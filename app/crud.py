from sqlalchemy.orm import Session
from . import models, schemas
from .scoring import calculate_score
from datetime import datetime, timedelta
import uuid


def create_rule(db: Session, rule: schemas.ScoringRuleCreate):
    db_rule = models.ScoringRule(**rule.model_dump())
    db.add(db_rule)
    db.commit()
    db.refresh(db_rule)
    return db_rule


def get_rule(db: Session, rule_id: int):
    return db.query(models.ScoringRule).filter(models.ScoringRule.id == rule_id).first()


def get_active_rule(db: Session):
    return db.query(models.ScoringRule).filter(models.ScoringRule.is_active == True).first()


def list_rules(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.ScoringRule).offset(skip).limit(limit).all()


def update_rule(db: Session, rule_id: int, rule_update: schemas.ScoringRuleUpdate):
    db_rule = get_rule(db, rule_id)
    if not db_rule:
        return None
    update_data = rule_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_rule, key, value)
    db_rule.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_rule)
    return db_rule


def import_batch(db: Session, batch_data: schemas.BatchImportRequest):
    errors = []
    for i, supplier in enumerate(batch_data.suppliers):
        if not supplier.supplier_code or supplier.supplier_code.strip() == "":
            errors.append(f"第{i+1}条供应商数据缺少供应商编号")

    if errors:
        raise ValueError("; ".join(errors))

    db_batch = models.SupplierBatch(
        batch_name=batch_data.batch_name,
        rule_id=batch_data.rule_id,
        status="imported",
        imported_by=batch_data.imported_by,
        supplier_count=len(batch_data.suppliers),
        remark=batch_data.remark or "",
    )
    db.add(db_batch)
    db.flush()

    for supplier in batch_data.suppliers:
        db_supplier = models.Supplier(
            batch_id=db_batch.id,
            supplier_code=supplier.supplier_code,
            supplier_name=supplier.supplier_name,
            metrics=supplier.metrics,
        )
        db.add(db_supplier)

    cleared_candidate = None
    change_log = None
    plan_superseded_ids = []
    current_candidate = get_current_candidate(db)
    if current_candidate and current_candidate.rule_id == batch_data.rule_id:
        current_candidate.is_current = False
        cleared_candidate = current_candidate
        change_log = models.CandidateChangeLog(
            old_candidate_id=current_candidate.id,
            new_candidate_id=None,
            change_reason=f"导入同规则(rule_id={batch_data.rule_id})新批次{db_batch.id}，旧候选批次{current_candidate.batch_id}自动失效",
            operated_by=batch_data.imported_by,
        )
        db.add(change_log)
        cancel_scheduled_releases_for_candidate(
            db, current_candidate.id,
            reason=f"导入同规则(rule_id={batch_data.rule_id})新批次{db_batch.id}，关联预约自动取消",
            operated_by=batch_data.imported_by,
            flush=False,
        )
        try:
            plan_superseded_ids = handle_import_conflict(
                db,
                rule_id=batch_data.rule_id,
                new_batch_id=db_batch.id,
                imported_by=batch_data.imported_by,
                new_source_detail=f"导入批次: {batch_data.batch_name}",
            )
        except Exception:
            pass

    db.commit()
    db.refresh(db_batch)
    return db_batch, cleared_candidate, change_log


def get_batch(db: Session, batch_id: int):
    return db.query(models.SupplierBatch).filter(models.SupplierBatch.id == batch_id).first()


def list_batches(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.SupplierBatch).order_by(models.SupplierBatch.imported_at.desc()).offset(skip).limit(limit).all()


def get_suppliers_by_batch(db: Session, batch_id: int):
    return db.query(models.Supplier).filter(models.Supplier.batch_id == batch_id).all()


def calculate_draft_scores(db: Session, batch_id: int):
    db_batch = get_batch(db, batch_id)
    if not db_batch:
        return None

    db_rule = get_rule(db, db_batch.rule_id)
    if not db_rule:
        raise ValueError("评分规则不存在")

    db.query(models.DraftScore).filter(models.DraftScore.batch_id == batch_id).delete()

    suppliers = get_suppliers_by_batch(db, batch_id)
    draft_scores = []

    for supplier in suppliers:
        total_score, score_details = calculate_score(supplier.metrics, db_rule.weight_config)
        db_draft = models.DraftScore(
            batch_id=batch_id,
            supplier_id=supplier.id,
            supplier_code=supplier.supplier_code,
            supplier_name=supplier.supplier_name,
            total_score=total_score,
            score_details=score_details,
            grade=score_details["grade"],
            calculated_at=datetime.utcnow(),
        )
        db.add(db_draft)
        draft_scores.append(db_draft)

    db_batch.status = "calculated"
    db.commit()
    db.refresh(db_batch)

    return draft_scores


def get_draft_scores(db: Session, batch_id: int):
    return db.query(models.DraftScore).filter(models.DraftScore.batch_id == batch_id).all()


def approve_and_release(db: Session, batch_id: int, approve_data: schemas.ApproveRequest, release_source: str = "manual", scheduled_release_id: int = None):
    db_batch = get_batch(db, batch_id)
    if not db_batch:
        return None

    existing_release = db.query(models.ReleaseVersion).filter(
        models.ReleaseVersion.batch_id == batch_id
    ).first()
    if existing_release:
        raise ValueError("该批次已发布过，不能重复发布")

    if db_batch.status != "calculated":
        raise ValueError("批次尚未完成计算，不能发布")

    draft_scores = get_draft_scores(db, batch_id)
    if not draft_scores:
        raise ValueError("没有草稿分数数据")

    version = f"v{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"

    db_release = models.ReleaseVersion(
        version=version,
        batch_id=batch_id,
        rule_id=db_batch.rule_id,
        is_active=True,
        release_note=approve_data.release_note or "",
        approval_remark=approve_data.approval_remark or "",
        approved_by=approve_data.approved_by,
        supplier_count=len(draft_scores),
        release_source=release_source,
    )
    db.add(db_release)
    db.flush()

    for draft in draft_scores:
        db_released = models.ReleasedScore(
            release_id=db_release.id,
            supplier_code=draft.supplier_code,
            supplier_name=draft.supplier_name,
            total_score=draft.total_score,
            score_details=draft.score_details,
            grade=draft.grade,
        )
        db.add(db_released)

    db.query(models.ReleaseVersion).filter(
        models.ReleaseVersion.id != db_release.id
    ).update({"is_active": False})

    db_batch.status = "released"

    current_candidate = get_current_candidate(db)
    if current_candidate:
        current_candidate.is_current = False
        change_log = models.CandidateChangeLog(
            old_candidate_id=current_candidate.id,
            new_candidate_id=None,
            change_reason=f"批次{batch_id}正式发布(v{db_release.version})，候选自动清空",
            operated_by=approve_data.approved_by,
        )
        db.add(change_log)
        cancel_scheduled_releases_for_candidate(
            db, current_candidate.id,
            reason=f"批次{batch_id}正式发布(v{db_release.version})，关联预约自动取消",
            operated_by=approve_data.approved_by,
            flush=False,
        )

    if scheduled_release_id:
        sched = db.query(models.ScheduledRelease).filter(models.ScheduledRelease.id == scheduled_release_id).first()
        if sched:
            sched.status = "executed"
            sched.release_version_id = db_release.id
            sched.executed_at = datetime.utcnow()
        try:
            handle_scheduler_execute_plan(
                db,
                scheduled_release_id=scheduled_release_id,
                release_version_id=db_release.id,
                success=True,
                operator=approve_data.approved_by,
                detail=f"预约发布成功，版本={db_release.version}",
            )
        except Exception:
            pass
    else:
        try:
            handle_manual_release_plan(
                db,
                rule_id=db_batch.rule_id,
                batch_id=batch_id,
                released_by=approve_data.approved_by,
                release_version_id=db_release.id,
                release_note=approve_data.release_note or "",
            )
        except Exception:
            pass

    db.commit()
    db.refresh(db_release)

    return db_release


def create_scheduled_release(db: Session, req: schemas.ScheduleReleaseRequest):
    db_batch = get_batch(db, req.batch_id)
    if not db_batch:
        raise ValueError("批次不存在")
    if db_batch.status != "calculated":
        raise ValueError("批次尚未完成计算，不能预约发布")

    if req.scheduled_time <= datetime.utcnow():
        raise ValueError("预约生效时间必须晚于当前时间")

    old_candidate = get_current_candidate(db)
    old_candidate_id = old_candidate.id if old_candidate else None

    if old_candidate and old_candidate.batch_id != req.batch_id:
        old_candidate.is_current = False
        cancel_scheduled_releases_for_candidate(
            db, old_candidate.id,
            reason=f"候选被批次{req.batch_id}顶替，关联预约自动取消",
            operated_by=req.set_by,
            flush=False,
        )

    existing_pending = None
    if old_candidate and old_candidate.batch_id == req.batch_id:
        existing_pending = db.query(models.ScheduledRelease).filter(
            models.ScheduledRelease.candidate_id == old_candidate.id,
            models.ScheduledRelease.status == "pending",
        ).first()
        if existing_pending:
            existing_pending.status = "cancelled"
            existing_pending.cancel_reason = f"同一批次重新预约，旧预约取消"
            existing_pending.cancelled_at = datetime.utcnow()
            existing_pending.cancelled_by = req.set_by
            try:
                handle_cancel_scheduled_release_plan(
                    db,
                    scheduled_release_id=existing_pending.id,
                    cancelled_by=req.set_by,
                    reason=f"同一批次重新预约，旧预约取消",
                )
            except Exception:
                pass

    new_candidate = models.ReleaseCandidate(
        batch_id=req.batch_id,
        rule_id=db_batch.rule_id,
        change_description=req.change_description,
        expected_effective_time=req.scheduled_time,
        operation_remark=req.operation_remark or "",
        set_by=req.set_by,
        is_current=True,
    )
    db.add(new_candidate)
    db.flush()

    change_reason = f"设置批次{req.batch_id}为预约发布候选，计划生效时间: {req.scheduled_time.isoformat()}"
    if old_candidate and old_candidate.batch_id != req.batch_id:
        change_reason = f"替换候选: 旧候选批次{old_candidate.batch_id}被新候选批次{req.batch_id}顶替(预约发布)"

    change_log = models.CandidateChangeLog(
        old_candidate_id=old_candidate_id,
        new_candidate_id=new_candidate.id,
        change_reason=change_reason,
        operated_by=req.set_by,
    )
    db.add(change_log)

    sched = models.ScheduledRelease(
        candidate_id=new_candidate.id,
        batch_id=req.batch_id,
        rule_id=db_batch.rule_id,
        scheduled_time=req.scheduled_time,
        status="pending",
        created_by=req.set_by,
    )
    db.add(sched)
    db.flush()

    try:
        handle_scheduled_release_plan(
            db,
            rule_id=db_batch.rule_id,
            batch_id=req.batch_id,
            candidate_id=new_candidate.id,
            scheduled_release_id=sched.id,
            scheduled_time=req.scheduled_time,
            set_by=req.set_by,
        )
    except Exception:
        pass

    db.commit()
    db.refresh(new_candidate)
    db.refresh(sched)
    return sched, new_candidate, change_log


def cancel_scheduled_releases_for_candidate(db: Session, candidate_id: int, reason: str, operated_by: str, flush: bool = True):
    rows = db.query(models.ScheduledRelease).filter(
        models.ScheduledRelease.candidate_id == candidate_id,
        models.ScheduledRelease.status == "pending",
    ).update({
        "status": "cancelled",
        "cancel_reason": reason,
        "cancelled_at": datetime.utcnow(),
        "cancelled_by": operated_by,
    }, synchronize_session=False)
    if flush:
        db.flush()
    return rows


def cancel_scheduled_release(db: Session, sched_id: int, reason: str, operated_by: str):
    sched = db.query(models.ScheduledRelease).filter(models.ScheduledRelease.id == sched_id).first()
    if not sched:
        raise ValueError("预约记录不存在")
    if sched.status != "pending":
        raise ValueError(f"预约状态为{sched.status}，不能取消")
    sched.status = "cancelled"
    sched.cancel_reason = reason or "手动取消预约"
    sched.cancelled_at = datetime.utcnow()
    sched.cancelled_by = operated_by

    candidate = db.query(models.ReleaseCandidate).filter(models.ReleaseCandidate.id == sched.candidate_id).first()
    change_log = None
    if candidate and candidate.is_current:
        candidate.is_current = False
        change_log = models.CandidateChangeLog(
            old_candidate_id=candidate.id,
            new_candidate_id=None,
            change_reason=f"预约发布被取消: {reason or '手动取消预约'}",
            operated_by=operated_by,
        )
        db.add(change_log)

    try:
        handle_cancel_scheduled_release_plan(
            db,
            scheduled_release_id=sched_id,
            cancelled_by=operated_by,
            reason=reason or "手动取消预约",
        )
    except Exception:
        pass

    db.commit()
    db.refresh(sched)
    return sched, change_log


def list_scheduled_releases(db: Session, status: str = None, rule_id: int = None, skip: int = 0, limit: int = 100):
    q = db.query(models.ScheduledRelease)
    if status:
        q = q.filter(models.ScheduledRelease.status == status)
    if rule_id:
        q = q.filter(models.ScheduledRelease.rule_id == rule_id)
    return q.order_by(models.ScheduledRelease.created_at.desc()).offset(skip).limit(limit).all()


def get_scheduled_release(db: Session, sched_id: int):
    return db.query(models.ScheduledRelease).filter(models.ScheduledRelease.id == sched_id).first()


def get_pending_scheduled_releases(db: Session, before_time: datetime = None, early_window_seconds: int = 0):
    q = db.query(models.ScheduledRelease).filter(models.ScheduledRelease.status == "pending")
    if before_time:
        effective_before = before_time + timedelta(seconds=early_window_seconds)
        q = q.filter(models.ScheduledRelease.scheduled_time <= effective_before)
    return q.order_by(models.ScheduledRelease.scheduled_time.asc()).all()


def get_latest_schedule_for_rule(db: Session, rule_id: int):
    return db.query(models.ScheduledRelease).filter(
        models.ScheduledRelease.rule_id == rule_id,
    ).order_by(models.ScheduledRelease.created_at.desc()).first()


def get_active_release(db: Session):
    return db.query(models.ReleaseVersion).filter(models.ReleaseVersion.is_active == True).first()


def get_release(db: Session, release_id: int):
    return db.query(models.ReleaseVersion).filter(models.ReleaseVersion.id == release_id).first()


def get_release_by_version(db: Session, version: str):
    return db.query(models.ReleaseVersion).filter(models.ReleaseVersion.version == version).first()


def list_releases(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.ReleaseVersion).order_by(models.ReleaseVersion.released_at.desc()).offset(skip).limit(limit).all()


def get_released_scores(db: Session, release_id: int):
    return db.query(models.ReleasedScore).filter(models.ReleasedScore.release_id == release_id).all()


def rollback_to_version(db: Session, rollback_data: schemas.RollbackRequest):
    current_active = get_active_release(db)
    if not current_active:
        raise ValueError("当前没有活动版本")

    target_release = get_release_by_version(db, rollback_data.target_version)
    if not target_release:
        raise ValueError("目标版本不存在")

    if current_active.version == target_release.version:
        raise ValueError("目标版本已是当前活动版本")

    current_active.is_active = False
    target_release.is_active = True

    rollback_record = models.RollbackRecord(
        from_version=current_active.version,
        to_version=target_release.version,
        reason=rollback_data.reason,
        operated_by=rollback_data.operated_by,
    )
    db.add(rollback_record)

    current_candidate = get_current_candidate(db)
    if current_candidate:
        current_candidate.is_current = False
        candidate_change_log = models.CandidateChangeLog(
            old_candidate_id=current_candidate.id,
            new_candidate_id=None,
            change_reason=f"版本回滚(从{current_active.version}到{target_release.version})，候选自动清空",
            operated_by=rollback_data.operated_by,
        )
        db.add(candidate_change_log)
        cancel_scheduled_releases_for_candidate(
            db, current_candidate.id,
            reason=f"版本回滚(从{current_active.version}到{target_release.version})，关联预约自动取消",
            operated_by=rollback_data.operated_by,
            flush=False,
        )

    try:
        handle_rollback_plan(
            db,
            rule_id=target_release.rule_id,
            target_version=target_release.version,
            operated_by=rollback_data.operated_by,
            reason=rollback_data.reason,
            from_version=current_active.version,
            release_version_id=target_release.id,
        )
    except Exception as e:
        import logging
        logging.getLogger("rollback_plan").error(f"handle_rollback_plan failed: {e}", exc_info=True)

    db.commit()
    db.refresh(target_release)
    db.refresh(rollback_record)

    return target_release, rollback_record


def list_rollback_records(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.RollbackRecord).order_by(models.RollbackRecord.operated_at.desc()).offset(skip).limit(limit).all()


def export_active_scores(db: Session):
    active_release = get_active_release(db)
    if not active_release:
        return None

    scores = get_released_scores(db, active_release.id)
    candidate = get_current_candidate(db)
    candidate_batch_id = candidate.batch_id if candidate else None
    candidate_matches_active = (candidate.batch_id == active_release.batch_id) if candidate and active_release else None

    release_source = active_release.release_source or "manual"
    source_plan_detail = ""
    plan_status_for_export = None

    try:
        active_plans = db.query(models.ReleasePlan).filter(
            models.ReleasePlan.release_version_id == active_release.id,
        ).all()
        if active_plans:
            plan = active_plans[0]
            if plan.source_type and release_source == "manual" and plan.source_type == "scheduled":
                pass
            elif plan.source_type and (release_source is None or release_source == ""):
                release_source = plan.source_type
            if plan.source_detail:
                source_plan_detail = plan.source_detail
            plan_status_for_export = plan.status
        elif not source_plan_detail and release_source == "scheduled":
            sched = db.query(models.ScheduledRelease).filter(
                models.ScheduledRelease.release_version_id == active_release.id,
            ).first()
            if sched:
                source_plan_detail = f"scheduled_release_id={sched.id}, planned_at={sched.scheduled_time.isoformat() if sched.scheduled_time else ''}"
    except Exception:
        pass

    return (active_release, scores, candidate_batch_id, candidate_matches_active,
            release_source, source_plan_detail, plan_status_for_export)


def create_user(db: Session, username: str, role: str):
    db_user = models.User(username=username, role=role)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def get_user(db: Session, username: str):
    return db.query(models.User).filter(models.User.username == username).first()


def list_users(db: Session):
    return db.query(models.User).all()


def write_audit_log(db: Session, action: str, operator: str, target_type: str, target_id: str, result: str, detail: str = ""):
    log = models.AuditLog(
        action=action,
        operator=operator,
        target_type=target_type,
        target_id=target_id,
        result=result,
        detail=detail,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def set_release_candidate(db: Session, candidate_data: schemas.SetCandidateRequest):
    db_batch = get_batch(db, candidate_data.batch_id)
    if not db_batch:
        raise ValueError("批次不存在")
    if db_batch.status != "calculated":
        raise ValueError("批次尚未完成计算，不能设为候选")

    old_candidate = get_current_candidate(db)
    old_candidate_id = old_candidate.id if old_candidate else None

    if old_candidate:
        old_candidate.is_current = False
        cancel_scheduled_releases_for_candidate(
            db, old_candidate.id,
            reason=f"候选被批次{candidate_data.batch_id}手动顶替，关联预约自动取消",
            operated_by=candidate_data.set_by,
            flush=False,
        )

    new_candidate = models.ReleaseCandidate(
        batch_id=candidate_data.batch_id,
        rule_id=db_batch.rule_id,
        change_description=candidate_data.change_description,
        expected_effective_time=candidate_data.expected_effective_time,
        operation_remark=candidate_data.operation_remark or "",
        set_by=candidate_data.set_by,
        is_current=True,
    )
    db.add(new_candidate)
    db.flush()

    change_reason = f"设置批次{candidate_data.batch_id}为候选发布"
    if old_candidate:
        change_reason = f"替换候选: 旧候选批次{old_candidate.batch_id}被新候选批次{candidate_data.batch_id}顶替"

    change_log = models.CandidateChangeLog(
        old_candidate_id=old_candidate_id,
        new_candidate_id=new_candidate.id,
        change_reason=change_reason,
        operated_by=candidate_data.set_by,
    )
    db.add(change_log)

    try:
        handle_set_candidate_plan(
            db,
            rule_id=db_batch.rule_id,
            batch_id=candidate_data.batch_id,
            candidate_id=new_candidate.id,
            set_by=candidate_data.set_by,
            expected_effective_time=candidate_data.expected_effective_time,
        )
    except Exception:
        pass

    db.commit()
    db.refresh(new_candidate)
    return new_candidate, change_log


def clear_candidate(db: Session, reason: str, operated_by: str):
    current = get_current_candidate(db)
    if not current:
        return None

    current.is_current = False
    cancel_scheduled_releases_for_candidate(
        db, current.id,
        reason=f"候选被手动取消，关联预约自动取消: {reason}",
        operated_by=operated_by,
        flush=False,
    )
    change_log = models.CandidateChangeLog(
        old_candidate_id=current.id,
        new_candidate_id=None,
        change_reason=reason,
        operated_by=operated_by,
    )
    db.add(change_log)

    try:
        handle_cancel_candidate_plan(
            db,
            rule_id=current.rule_id,
            cancelled_by=operated_by,
            reason=reason,
        )
    except Exception:
        pass

    db.commit()
    db.refresh(current)
    return current, change_log


def get_current_candidate(db: Session):
    return db.query(models.ReleaseCandidate).filter(
        models.ReleaseCandidate.is_current == True
    ).first()


def get_latest_candidate_change_log(db: Session):
    return db.query(models.CandidateChangeLog).order_by(
        models.CandidateChangeLog.operated_at.desc()
    ).first()


def list_candidate_change_logs(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.CandidateChangeLog).order_by(
        models.CandidateChangeLog.operated_at.desc()
    ).offset(skip).limit(limit).all()


def has_duplicate_rejected_audit(db: Session, batch_id: str):
    return db.query(models.AuditLog).filter(
        models.AuditLog.action == "release",
        models.AuditLog.result == "duplicate_rejected",
        models.AuditLog.target_id == batch_id,
    ).first() is not None


def list_audit_logs(db: Session, action: str = None, operator: str = None, target_type: str = None, skip: int = 0, limit: int = 100):
    q = db.query(models.AuditLog)
    if action:
        q = q.filter(models.AuditLog.action == action)
    if operator:
        q = q.filter(models.AuditLog.operator == operator)
    if target_type:
        q = q.filter(models.AuditLog.target_type == target_type)
    return q.order_by(models.AuditLog.created_at.desc()).offset(skip).limit(limit).all()


_PLAN_CONFIG_SCHEMA = {
    "allow_early_window_seconds": {
        "type": int,
        "min": 0,
        "max": 86400 * 30,
        "error": "必须是0-2592000之间的整数（秒）",
    },
    "allow_late_window_seconds": {
        "type": int,
        "min": 60,
        "max": 86400 * 30,
        "error": "必须是60-2592000之间的整数（秒，最小1分钟）",
    },
    "default_expire_hours": {
        "type": int,
        "min": 1,
        "max": 24 * 365,
        "error": "必须是1-8760之间的整数（小时，最小1小时）",
    },
    "max_queued_per_rule": {
        "type": int,
        "min": 1,
        "max": 100,
        "error": "必须是1-100之间的整数",
    },
}


def validate_plan_config(config_key: str, config_value: str) -> tuple[bool, str, str | None]:
    if config_key not in _PLAN_CONFIG_SCHEMA:
        allowed = ", ".join(sorted(_PLAN_CONFIG_SCHEMA.keys()))
        return False, f"未知配置项: {config_key}。允许的配置项: {allowed}", None

    schema = _PLAN_CONFIG_SCHEMA[config_key]
    try:
        parsed = schema["type"](config_value)
    except (ValueError, TypeError):
        return False, f"配置项 {config_key} {schema['error']}", None

    if "min" in schema and parsed < schema["min"]:
        return False, f"配置项 {config_key} 不能小于 {schema['min']}", None
    if "max" in schema and parsed > schema["max"]:
        return False, f"配置项 {config_key} 不能大于 {schema['max']}", None

    return True, "", str(parsed)


def get_plan_config(db: Session, config_key: str, rule_id: int | None = None) -> str | None:
    if rule_id is not None:
        cfg = db.query(models.ReleasePlanConfig).filter(
            models.ReleasePlanConfig.rule_id == rule_id,
            models.ReleasePlanConfig.config_key == config_key,
        ).first()
        if cfg:
            return cfg.config_value

    cfg = db.query(models.ReleasePlanConfig).filter(
        models.ReleasePlanConfig.rule_id.is_(None),
        models.ReleasePlanConfig.config_key == config_key,
    ).first()
    return cfg.config_value if cfg else None


def get_plan_config_int(db: Session, config_key: str, rule_id: int | None = None, default: int = 0) -> int:
    raw = get_plan_config(db, config_key, rule_id)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def get_max_early_window_seconds(db: Session, default: int = 120) -> int:
    try:
        rows = db.query(models.ReleasePlanConfig.config_value).filter(
            models.ReleasePlanConfig.config_key == "allow_early_window_seconds"
        ).all()
        max_val = default
        for row in rows:
            try:
                val = int(row.config_value)
                if val > max_val:
                    max_val = val
            except (ValueError, TypeError):
                pass
        return max_val
    except Exception:
        return default


def list_plan_configs(db: Session, rule_id: int | None = None) -> list[models.ReleasePlanConfig]:
    q = db.query(models.ReleasePlanConfig)
    if rule_id is None:
        q = q.filter(models.ReleasePlanConfig.rule_id.is_(None))
    else:
        q = q.filter((models.ReleasePlanConfig.rule_id == rule_id) | (models.ReleasePlanConfig.rule_id.is_(None)))
    return q.order_by(models.ReleasePlanConfig.rule_id.is_(None).desc(), models.ReleasePlanConfig.config_key.asc()).all()


def set_plan_config(db: Session, config_key: str, config_value: str, updated_by: str, rule_id: int | None = None, description: str = "") -> models.ReleasePlanConfig:
    valid, error, normalized = validate_plan_config(config_key, config_value)
    if not valid:
        raise ValueError(error)

    cfg = db.query(models.ReleasePlanConfig).filter(
        (models.ReleasePlanConfig.rule_id == rule_id) if rule_id is not None else models.ReleasePlanConfig.rule_id.is_(None),
        models.ReleasePlanConfig.config_key == config_key,
    ).first()

    if cfg:
        cfg.config_value = normalized or config_value
        cfg.description = description or cfg.description
        cfg.updated_by = updated_by
        cfg.updated_at = datetime.utcnow()
    else:
        cfg = models.ReleasePlanConfig(
            rule_id=rule_id,
            config_key=config_key,
            config_value=normalized or config_value,
            description=description,
            updated_by=updated_by,
        )
        db.add(cfg)

    db.commit()
    db.refresh(cfg)
    return cfg


def _add_plan_event(db: Session, plan_id: int, event_type: str, from_status: str | None, to_status: str | None,
                    operator: str, reason: str = "", detail: dict | None = None):
    event = models.ReleasePlanEvent(
        plan_id=plan_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        operator=operator,
        reason=reason,
        detail=detail or {},
    )
    db.add(event)


def _create_plan_record(db: Session, rule_id: int, status: str, source_type: str, plan_type: str,
                        created_by: str, batch_id: int | None = None, candidate_id: int | None = None,
                        scheduled_release_id: int | None = None, release_version_id: int | None = None,
                        planned_time: datetime | None = None, conflict_reason: str = "",
                        source_detail: str = "",
                        executed_at: datetime | None = None, expired_at: datetime | None = None,
                        cancelled_at: datetime | None = None, superseded_at: datetime | None = None,
                        superseded_by_plan_id: int | None = None) -> models.ReleasePlan:
    now = datetime.utcnow()
    plan = models.ReleasePlan(
        rule_id=rule_id,
        batch_id=batch_id,
        candidate_id=candidate_id,
        scheduled_release_id=scheduled_release_id,
        release_version_id=release_version_id,
        status=status,
        source_type=source_type,
        plan_type=plan_type,
        planned_time=planned_time,
        executed_at=executed_at,
        expired_at=expired_at,
        cancelled_at=cancelled_at,
        superseded_at=superseded_at,
        superseded_by_plan_id=superseded_by_plan_id,
        created_by=created_by,
        created_at=now,
        updated_at=now,
        conflict_reason=conflict_reason,
        source_detail=source_detail,
    )
    db.add(plan)
    db.flush()
    _add_plan_event(db, plan.id, "created", None, status, created_by, "创建发布计划", {
        "source_type": source_type,
        "plan_type": plan_type,
    })
    return plan


def _supersede_plan(db: Session, plan: models.ReleasePlan, superseder_id: int, operator: str, reason: str):
    old_status = plan.status
    now = datetime.utcnow()
    plan.status = models.PLAN_STATUS_SUPERSEDED
    plan.superseded_at = now
    plan.superseded_by_plan_id = superseder_id
    plan.conflict_reason = reason
    plan.updated_at = now
    plan.executed_at = None
    plan.cancelled_at = None
    plan.expired_at = None
    _add_plan_event(db, plan.id, "superseded", old_status, models.PLAN_STATUS_SUPERSEDED,
                    operator, reason, {"superseded_by_plan_id": superseder_id})


def check_conflict_for_import(db: Session, rule_id: int, new_batch_id: int, imported_by: str) -> schemas.ReleasePlanConflictInfo:
    active_plans = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.rule_id == rule_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
    ).all()

    if not active_plans:
        return schemas.ReleasePlanConflictInfo(has_conflict=False)

    oldest = min(active_plans, key=lambda p: p.created_at)
    return schemas.ReleasePlanConflictInfo(
        has_conflict=True,
        conflict_type="import_conflict",
        conflict_plan_id=oldest.id,
        conflict_reason=f"导入同规则(rule_id={rule_id})新批次{new_batch_id}，排队/预约计划将被顶掉",
        suggestion=f"计划#{oldest.id}将被标记为superseded（被导入新批次顶掉）",
    )


def handle_import_conflict(db: Session, rule_id: int, new_batch_id: int, imported_by: str,
                           new_source_detail: str = "") -> list[int]:
    active_plans = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.rule_id == rule_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
    ).all()

    superseded_ids = []
    new_plan = _create_plan_record(
        db, rule_id=rule_id,
        status=models.PLAN_STATUS_QUEUED,
        source_type=models.PLAN_SOURCE_IMPORT_CONFLICT,
        plan_type=models.PLAN_TYPE_CANDIDATE,
        created_by=imported_by,
        batch_id=new_batch_id,
        conflict_reason="导入新批次占位",
        source_detail=new_source_detail,
    )
    db.flush()

    for plan in active_plans:
        reason = f"导入同规则(rule_id={rule_id})新批次{new_batch_id}，原计划被顶掉"
        _supersede_plan(db, plan, new_plan.id, imported_by, reason)
        superseded_ids.append(plan.id)

    return superseded_ids


def check_conflict_for_manual_release(db: Session, rule_id: int, batch_id: int, released_by: str) -> schemas.ReleasePlanConflictInfo:
    active_plans = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.rule_id == rule_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
    ).all()

    if not active_plans:
        return schemas.ReleasePlanConflictInfo(has_conflict=False)

    matching = [p for p in active_plans if p.batch_id == batch_id]
    if matching:
        conflict = matching[0]
        return schemas.ReleasePlanConflictInfo(
            has_conflict=True,
            conflict_type="manual_release_same_batch",
            conflict_plan_id=conflict.id,
            conflict_reason=f"手动发布批次{batch_id}，该批次存在排队/预约计划#{conflict.id}，将被标记为已执行",
            suggestion="计划将被提升为executed（手动提前发布）",
        )
    else:
        oldest = min(active_plans, key=lambda p: p.created_at)
        return schemas.ReleasePlanConflictInfo(
            has_conflict=True,
            conflict_type="manual_release_different_batch",
            conflict_plan_id=oldest.id,
            conflict_reason=f"手动发布批次{batch_id}，与排队/预约计划#{oldest.id}(批次{oldest.batch_id})不一致，旧计划将被顶掉",
            suggestion=f"计划#{oldest.id}将被标记为superseded（被手动发布顶掉）",
        )


def handle_manual_release_plan(db: Session, rule_id: int, batch_id: int, released_by: str,
                               release_version_id: int, release_note: str = "") -> models.ReleasePlan:
    existing = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.rule_id == rule_id,
        models.ReleasePlan.batch_id == batch_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
    ).first()

    if existing:
        old_status = existing.status
        now = datetime.utcnow()
        existing.status = models.PLAN_STATUS_EXECUTED
        existing.executed_at = now
        existing.release_version_id = release_version_id
        existing.updated_at = now
        existing.conflict_reason = f"手动提前发布: {release_note}"
        existing.source_detail = existing.source_detail or f"手动提前发布: {release_note}"
        existing.cancelled_at = None
        existing.expired_at = None
        existing.superseded_at = None
        existing.superseded_by_plan_id = None
        _add_plan_event(db, existing.id, "manual_release", old_status, models.PLAN_STATUS_EXECUTED,
                        released_by, f"手动提前发布，版本ID={release_version_id}",
                        {"release_version_id": release_version_id, "release_note": release_note})
        result = existing
    else:
        result = _create_plan_record(
            db, rule_id=rule_id,
            status=models.PLAN_STATUS_EXECUTED,
            source_type=models.PLAN_SOURCE_MANUAL_RELEASE,
            plan_type=models.PLAN_TYPE_RELEASE,
            created_by=released_by,
            batch_id=batch_id,
            release_version_id=release_version_id,
            executed_at=datetime.utcnow(),
            planned_time=datetime.utcnow(),
            conflict_reason=f"手动发布: {release_note}",
            source_detail=release_note,
        )

    other_active = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.rule_id == rule_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
        models.ReleasePlan.id != result.id,
    ).all()

    for plan in other_active:
        reason = f"手动发布批次{batch_id}(版本ID={release_version_id})，不同批次的排队/预约计划被顶掉"
        _supersede_plan(db, plan, result.id, released_by, reason)

    return result


def check_conflict_for_cancel_candidate(db: Session, rule_id: int, cancelled_by: str) -> schemas.ReleasePlanConflictInfo:
    active_plans = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.rule_id == rule_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
    ).all()

    if not active_plans:
        return schemas.ReleasePlanConflictInfo(has_conflict=False)

    active = active_plans[0]
    return schemas.ReleasePlanConflictInfo(
        has_conflict=True,
        conflict_type="cancel_candidate",
        conflict_plan_id=active.id,
        conflict_reason=f"手动取消候选，计划#{active.id}将被标记为cancelled",
        suggestion="计划状态将变更为cancelled",
    )


def handle_cancel_candidate_plan(db: Session, rule_id: int, cancelled_by: str, reason: str) -> list[int]:
    active_plans = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.rule_id == rule_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
    ).all()

    cancelled_ids = []
    now = datetime.utcnow()
    for plan in active_plans:
        old_status = plan.status
        plan.status = models.PLAN_STATUS_CANCELLED
        plan.cancelled_at = now
        plan.conflict_reason = reason
        plan.updated_at = now
        plan.source_detail = plan.source_detail or f"手动取消候选: {reason}"
        plan.executed_at = None
        plan.expired_at = None
        plan.superseded_at = None
        plan.superseded_by_plan_id = None
        _add_plan_event(db, plan.id, "cancelled", old_status, models.PLAN_STATUS_CANCELLED,
                        cancelled_by, reason, {"cancel_reason": reason})
        cancelled_ids.append(plan.id)

    return cancelled_ids


def check_conflict_for_rollback(db: Session, rule_id: int, target_version: str, operated_by: str) -> schemas.ReleasePlanConflictInfo:
    active_plans = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.rule_id == rule_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
    ).all()

    if not active_plans:
        return schemas.ReleasePlanConflictInfo(has_conflict=False)

    oldest = min(active_plans, key=lambda p: p.created_at)
    return schemas.ReleasePlanConflictInfo(
        has_conflict=True,
        conflict_type="rollback_conflict",
        conflict_plan_id=oldest.id,
        conflict_reason=f"版本回滚到{target_version}，排队/预约计划将被顶掉",
        suggestion=f"计划#{oldest.id}将被标记为superseded（被回滚操作顶掉）",
    )


def handle_rollback_plan(db: Session, rule_id: int, target_version: str, operated_by: str,
                         reason: str, from_version: str, release_version_id: int | None = None) -> tuple[models.ReleasePlan, list[int]]:
    rollback_plan = _create_plan_record(
        db, rule_id=rule_id,
        status=models.PLAN_STATUS_EXECUTED,
        source_type=models.PLAN_SOURCE_ROLLBACK,
        plan_type=models.PLAN_TYPE_ROLLBACK,
        created_by=operated_by,
        release_version_id=release_version_id,
        executed_at=datetime.utcnow(),
        planned_time=datetime.utcnow(),
        conflict_reason=f"从{from_version}回滚到{target_version}: {reason}",
        source_detail=reason,
    )
    db.flush()

    superseded_ids = []
    active_plans = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.rule_id == rule_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
        models.ReleasePlan.id != rollback_plan.id,
    ).all()

    for plan in active_plans:
        sup_reason = f"版本回滚(从{from_version}到{target_version})，排队/预约计划被顶掉"
        _supersede_plan(db, plan, rollback_plan.id, operated_by, sup_reason)
        superseded_ids.append(plan.id)

    return rollback_plan, superseded_ids


def handle_set_candidate_plan(db: Session, rule_id: int, batch_id: int, candidate_id: int,
                              set_by: str, planned_time: datetime | None = None,
                              expected_effective_time: datetime | None = None) -> tuple[models.ReleasePlan, list[int]]:
    superseded_ids = []
    old_active = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.rule_id == rule_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
    ).all()

    new_plan = _create_plan_record(
        db, rule_id=rule_id,
        status=models.PLAN_STATUS_QUEUED,
        source_type=models.PLAN_SOURCE_MANUAL_CANDIDATE,
        plan_type=models.PLAN_TYPE_CANDIDATE,
        created_by=set_by,
        batch_id=batch_id,
        candidate_id=candidate_id,
        planned_time=expected_effective_time or planned_time,
        source_detail="手动设置候选",
    )
    db.flush()

    for plan in old_active:
        if plan.batch_id == batch_id and plan.candidate_id == candidate_id:
            continue
        reason = f"手动设置批次{batch_id}为候选，旧候选批次{plan.batch_id}被顶替"
        _supersede_plan(db, plan, new_plan.id, set_by, reason)
        superseded_ids.append(plan.id)

    return new_plan, superseded_ids


def handle_scheduled_release_plan(db: Session, rule_id: int, batch_id: int, candidate_id: int,
                                  scheduled_release_id: int, scheduled_time: datetime,
                                  set_by: str) -> tuple[models.ReleasePlan, list[int]]:
    superseded_ids = []
    old_active = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.rule_id == rule_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
    ).all()

    new_plan = _create_plan_record(
        db, rule_id=rule_id,
        status=models.PLAN_STATUS_SCHEDULED,
        source_type=models.PLAN_SOURCE_SCHEDULED,
        plan_type=models.PLAN_TYPE_RELEASE,
        created_by=set_by,
        batch_id=batch_id,
        candidate_id=candidate_id,
        scheduled_release_id=scheduled_release_id,
        planned_time=scheduled_time,
        source_detail=f"预约到{scheduled_time.isoformat()}",
    )
    db.flush()

    for plan in old_active:
        if plan.scheduled_release_id == scheduled_release_id:
            continue
        reason = f"设置批次{batch_id}预约发布({scheduled_time.isoformat()})，旧计划批次{plan.batch_id}被顶替"
        _supersede_plan(db, plan, new_plan.id, set_by, reason)
        superseded_ids.append(plan.id)

    return new_plan, superseded_ids


def handle_cancel_scheduled_release_plan(db: Session, scheduled_release_id: int, cancelled_by: str,
                                         reason: str) -> int | None:
    plan = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.scheduled_release_id == scheduled_release_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
    ).first()

    if not plan:
        return None

    old_status = plan.status
    now = datetime.utcnow()
    plan.status = models.PLAN_STATUS_CANCELLED
    plan.cancelled_at = now
    plan.conflict_reason = reason
    plan.updated_at = now
    plan.source_detail = plan.source_detail or f"预约发布取消: {reason}"
    plan.executed_at = None
    plan.expired_at = None
    plan.superseded_at = None
    plan.superseded_by_plan_id = None
    _add_plan_event(db, plan.id, "cancelled", old_status, models.PLAN_STATUS_CANCELLED,
                    cancelled_by, reason, {"cancel_reason": reason})
    return plan.id


def handle_scheduler_execute_plan(db: Session, scheduled_release_id: int, release_version_id: int,
                                  success: bool, operator: str, detail: str = "") -> models.ReleasePlan | None:
    plan = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.scheduled_release_id == scheduled_release_id,
    ).first()

    if not plan:
        return None

    old_status = plan.status
    now = datetime.utcnow()
    if success:
        plan.status = models.PLAN_STATUS_EXECUTED
        plan.executed_at = now
        plan.release_version_id = release_version_id
        plan.cancelled_at = None
        plan.expired_at = None
        plan.superseded_at = None
        plan.superseded_by_plan_id = None
    else:
        plan.status = models.PLAN_STATUS_FAILED
        plan.conflict_reason = f"执行失败: {detail}"
        plan.source_detail = plan.source_detail or f"执行失败: {detail}"
    plan.updated_at = now
    _add_plan_event(
        db, plan.id,
        "scheduler_execute" if success else "scheduler_failed",
        old_status, plan.status, operator, detail,
        {"release_version_id": release_version_id, "detail": detail, "success": success},
    )
    return plan


def handle_scheduler_conflict_cancel(db: Session, scheduled_release_id: int, operator: str,
                                     reason: str) -> models.ReleasePlan | None:
    plan = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.scheduled_release_id == scheduled_release_id,
        models.ReleasePlan.status.in_([models.PLAN_STATUS_QUEUED, models.PLAN_STATUS_SCHEDULED]),
    ).first()

    if not plan:
        return None

    old_status = plan.status
    now = datetime.utcnow()
    plan.status = models.PLAN_STATUS_SUPERSEDED
    plan.superseded_at = now
    plan.conflict_reason = reason
    plan.updated_at = now
    plan.source_detail = plan.source_detail or f"调度冲突取消: {reason}"
    plan.executed_at = None
    plan.cancelled_at = None
    plan.expired_at = None
    _add_plan_event(db, plan.id, "scheduler_conflict", old_status, models.PLAN_STATUS_SUPERSEDED,
                    operator, reason, {"conflict_reason": reason})
    return plan


def expire_stale_plans(db: Session) -> list[int]:
    now = datetime.utcnow()
    stale_ids = []

    queued_plans = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.status == models.PLAN_STATUS_QUEUED,
    ).all()

    for plan in queued_plans:
        rule_id_val = plan.rule_id if plan.rule_id and plan.rule_id > 0 else None
        expire_hours = get_plan_config_int(db, "default_expire_hours", rule_id_val, default=72)
        cutoff = plan.created_at + timedelta(hours=expire_hours)
        if now >= cutoff:
            old_status = plan.status
            plan.status = models.PLAN_STATUS_EXPIRED
            plan.expired_at = now
            plan.conflict_reason = f"排队超过{expire_hours}小时自动失效"
            plan.updated_at = now
            plan.executed_at = None
            plan.cancelled_at = None
            plan.superseded_at = None
            plan.superseded_by_plan_id = None
            _add_plan_event(db, plan.id, "auto_expired", old_status, models.PLAN_STATUS_EXPIRED,
                            "__scheduler__", f"排队超过{expire_hours}小时自动失效",
                            {"expire_hours": expire_hours})
            stale_ids.append(plan.id)

    scheduled_plans = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.status == models.PLAN_STATUS_SCHEDULED,
    ).all()

    for plan in scheduled_plans:
        if not plan.planned_time:
            continue
        rule_id_val = plan.rule_id if plan.rule_id and plan.rule_id > 0 else None
        late_seconds = get_plan_config_int(db, "allow_late_window_seconds", rule_id_val, default=86400)
        cutoff = plan.planned_time + timedelta(seconds=late_seconds)
        if now >= cutoff:
            old_status = plan.status
            plan.status = models.PLAN_STATUS_EXPIRED
            plan.expired_at = now
            plan.conflict_reason = f"超过预约时间{late_seconds}秒未执行自动失效"
            plan.updated_at = now
            plan.executed_at = None
            plan.cancelled_at = None
            plan.superseded_at = None
            plan.superseded_by_plan_id = None
            _add_plan_event(db, plan.id, "auto_expired", old_status, models.PLAN_STATUS_EXPIRED,
                            "__scheduler__", f"超过预约窗口{late_seconds}秒自动失效",
                            {"allow_late_window_seconds": late_seconds})
            stale_ids.append(plan.id)

    if stale_ids:
        db.commit()
    return stale_ids


def get_plan_by_id(db: Session, plan_id: int) -> models.ReleasePlan | None:
    return db.query(models.ReleasePlan).filter(models.ReleasePlan.id == plan_id).first()


def list_plans(db: Session, rule_id: int | None = None, status: str | None = None,
               source_type: str | None = None, plan_type: str | None = None,
               batch_id: int | None = None, skip: int = 0, limit: int = 100) -> list[models.ReleasePlan]:
    q = db.query(models.ReleasePlan)
    if rule_id is not None:
        q = q.filter(models.ReleasePlan.rule_id == rule_id)
    if status:
        q = q.filter(models.ReleasePlan.status == status)
    if source_type:
        q = q.filter(models.ReleasePlan.source_type == source_type)
    if plan_type:
        q = q.filter(models.ReleasePlan.plan_type == plan_type)
    if batch_id is not None:
        q = q.filter(models.ReleasePlan.batch_id == batch_id)
    return q.order_by(models.ReleasePlan.created_at.desc()).offset(skip).limit(limit).all()


def get_plan_stats(db: Session, rule_id: int | None = None) -> schemas.ReleasePlanStatsResponse:
    q = db.query(models.ReleasePlan)
    if rule_id is not None:
        q = q.filter(models.ReleasePlan.rule_id == rule_id)
    all_plans = q.all()

    stats = schemas.ReleasePlanStatsResponse(rule_id=rule_id, total_count=len(all_plans))
    for p in all_plans:
        key = f"{p.status}_count"
        if hasattr(stats, key):
            setattr(stats, key, getattr(stats, key) + 1)
    return stats


def get_plan_events(db: Session, plan_id: int, skip: int = 0, limit: int = 200) -> list[models.ReleasePlanEvent]:
    return db.query(models.ReleasePlanEvent).filter(
        models.ReleasePlanEvent.plan_id == plan_id,
    ).order_by(models.ReleasePlanEvent.created_at.desc()).offset(skip).limit(limit).all()


def recover_plans_on_restart(db: Session) -> dict:
    stats = {"recovered_queued": 0, "recovered_scheduled": 0, "auto_expired": 0,
             "reconciled_executed": 0, "reconciled_cancelled": 0, "reconciled_superseded": 0}

    _repair_contradictory_plans(db)

    expire_stale_plans(db)

    scheduled_plans = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.status == models.PLAN_STATUS_SCHEDULED,
        models.ReleasePlan.scheduled_release_id.isnot(None),
    ).all()
    for plan in scheduled_plans:
        sched = db.query(models.ScheduledRelease).filter(
            models.ScheduledRelease.id == plan.scheduled_release_id,
        ).first()
        if not sched:
            now = datetime.utcnow()
            old_status = plan.status
            plan.status = models.PLAN_STATUS_EXPIRED
            plan.expired_at = now
            plan.conflict_reason = "预约记录丢失，自动失效"
            plan.updated_at = now
            plan.executed_at = None
            plan.cancelled_at = None
            plan.superseded_at = None
            plan.superseded_by_plan_id = None
            _add_plan_event(db, plan.id, "reconciled_expired", old_status, models.PLAN_STATUS_EXPIRED,
                            "__system__", "预约记录丢失，自动失效", {})
            stats["auto_expired"] += 1
            continue
        if sched.status == "executed":
            now = datetime.utcnow()
            old_status = plan.status
            plan.status = models.PLAN_STATUS_EXECUTED
            plan.executed_at = sched.executed_at or now
            plan.release_version_id = sched.release_version_id
            plan.updated_at = now
            plan.cancelled_at = None
            plan.expired_at = None
            plan.superseded_at = None
            plan.superseded_by_plan_id = None
            plan.source_detail = plan.source_detail or f"重启后对齐: 预约已执行"
            _add_plan_event(db, plan.id, "reconciled_executed", old_status, models.PLAN_STATUS_EXECUTED,
                            "__system__", "重启后发现预约已执行，对齐计划状态",
                            {"scheduled_release_id": sched.id})
            stats["reconciled_executed"] += 1
        elif sched.status == "cancelled":
            now = datetime.utcnow()
            old_status = plan.status
            plan.status = models.PLAN_STATUS_CANCELLED
            plan.cancelled_at = sched.cancelled_at or now
            plan.conflict_reason = sched.cancel_reason or "预约已取消，重启后对齐"
            plan.updated_at = now
            plan.executed_at = None
            plan.expired_at = None
            plan.superseded_at = None
            plan.superseded_by_plan_id = None
            plan.source_detail = plan.source_detail or f"重启后对齐: 预约已取消"
            _add_plan_event(db, plan.id, "reconciled_cancelled", old_status, models.PLAN_STATUS_CANCELLED,
                            "__system__", "重启后发现预约已取消，对齐计划状态",
                            {"scheduled_release_id": sched.id, "cancel_reason": sched.cancel_reason})
            stats["reconciled_cancelled"] += 1

    if stats["reconciled_executed"] + stats["reconciled_cancelled"] + stats["auto_expired"] > 0:
        db.commit()

    queued = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.status == models.PLAN_STATUS_QUEUED,
    ).all()
    stats["recovered_queued"] = len(queued)

    remaining_scheduled = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.status == models.PLAN_STATUS_SCHEDULED,
    ).all()
    stats["recovered_scheduled"] = len(remaining_scheduled)

    return stats


def _repair_contradictory_plans(db: Session):
    repaired = 0
    executed_with_cancel = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.status == models.PLAN_STATUS_EXECUTED,
        models.ReleasePlan.cancelled_at.isnot(None),
    ).all()
    for plan in executed_with_cancel:
        plan.cancelled_at = None
        repaired += 1

    cancelled_with_exec = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.status == models.PLAN_STATUS_CANCELLED,
        models.ReleasePlan.executed_at.isnot(None),
    ).all()
    for plan in cancelled_with_exec:
        plan.executed_at = None
        repaired += 1

    superseded_with_exec = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.status == models.PLAN_STATUS_SUPERSEDED,
        models.ReleasePlan.executed_at.isnot(None),
    ).all()
    for plan in superseded_with_exec:
        plan.executed_at = None
        repaired += 1

    expired_with_exec = db.query(models.ReleasePlan).filter(
        models.ReleasePlan.status == models.PLAN_STATUS_EXPIRED,
        models.ReleasePlan.executed_at.isnot(None),
    ).all()
    for plan in expired_with_exec:
        plan.executed_at = None
        repaired += 1

    if repaired > 0:
        db.commit()
    return repaired
