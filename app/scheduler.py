import threading
import time
from datetime import datetime
from sqlalchemy.orm import Session
from .database import SessionLocal
from . import models, schemas, crud
import logging

logger = logging.getLogger("scheduled_release_scheduler")


class ScheduledReleaseScheduler:
    def __init__(self, interval_sec: int = 5):
        self._interval = interval_sec
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False

    def start(self):
        with self._lock:
            if self._running:
                return
            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            logger.info("ScheduledReleaseScheduler started")

    def stop(self):
        with self._lock:
            if not self._running:
                return
            self._stop_event.set()
            self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        logger.info("ScheduledReleaseScheduler stopped")

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self._run_tick()
            except Exception as e:
                logger.exception(f"Scheduler tick error: {e}")
            self._stop_event.wait(self._interval)

    def _run_tick(self):
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            pending = crud.get_pending_scheduled_releases(db, before_time=now)
            for sched in pending:
                if self._stop_event.is_set():
                    break
                self._process_one(db, sched)
                db.commit()
        except Exception as e:
            logger.exception(f"Unexpected scheduler error: {e}")
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()

    def _process_one(self, db: Session, sched: models.ScheduledRelease):
        db.refresh(sched)
        if sched.status != "pending":
            return

        candidate = db.query(models.ReleaseCandidate).filter(
            models.ReleaseCandidate.id == sched.candidate_id
        ).first()

        conflict_reason = None
        if not candidate or not candidate.is_current:
            conflict_reason = "候选已失效/被顶替/被取消，预约安全取消"
        elif candidate.batch_id != sched.batch_id:
            conflict_reason = "候选批次与预约批次不一致，可能被篡改，预约安全取消"
        else:
            batch = crud.get_batch(db, sched.batch_id)
            if not batch:
                conflict_reason = "批次不存在，预约安全取消"
            elif batch.status == "released":
                conflict_reason = "批次已被手动发布，预约安全取消"

        if conflict_reason:
            sched.status = "cancelled"
            sched.cancel_reason = conflict_reason
            sched.cancelled_at = datetime.utcnow()
            sched.cancelled_by = "__scheduler__"
            crud.write_audit_log(
                db,
                action="scheduled_release_conflict",
                operator="__scheduler__",
                target_type="scheduled_release",
                target_id=str(sched.id),
                result="cancelled",
                detail=conflict_reason,
            )
            return

        try:
            approve_data = schemas.ApproveRequest(
                approved_by=sched.created_by,
                approval_remark=candidate.operation_remark or f"预约自动发布，原计划时间 {sched.scheduled_time.isoformat()}",
                release_note=candidate.change_description or f"预约自动生效发布",
            )
            release = crud.approve_and_release(
                db,
                sched.batch_id,
                approve_data,
                release_source="scheduled",
                scheduled_release_id=sched.id,
            )
            crud.write_audit_log(
                db,
                action="scheduled_release",
                operator=sched.created_by,
                target_type="version",
                target_id=release.version,
                result="success",
                detail=f"预约自动生效发布成功，版本={release.version}，批次={sched.batch_id}",
            )
        except ValueError as ve:
            sched.status = "cancelled"
            sched.cancel_reason = f"发布时校验失败: {ve}"
            sched.cancelled_at = datetime.utcnow()
            sched.cancelled_by = "__scheduler__"
            crud.write_audit_log(
                db,
                action="scheduled_release_failed",
                operator="__scheduler__",
                target_type="scheduled_release",
                target_id=str(sched.id),
                result="failed",
                detail=str(ve),
            )


scheduler = ScheduledReleaseScheduler(interval_sec=5)
