from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session
from typing import List, Optional
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import logging
import json
from .database import engine, Base, get_db, SessionLocal
from . import models, schemas, crud, auth
from .scheduler import scheduler
from .migrations import run_migrations, get_current_version
from . import migrations as plan_migrations

logger = logging.getLogger("release_plan_main")

Base.metadata.create_all(bind=engine)


def _run_startup_tasks():
    db = SessionLocal()
    try:
        run_migrations(db)
        recover_stats = crud.recover_plans_on_restart(db)
        logger.info(f"Plan recovery on restart: {recover_stats}")
        schema_ver = get_current_version(db)
        logger.info(f"Schema version after migration: {schema_ver}")
    except Exception as e:
        logger.exception(f"Startup migration/recovery error: {e}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _run_startup_tasks()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="供应商评分重算服务", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def audit_permission_denial(request: Request, call_next):
    response = await call_next(request)
    if response.status_code == 403:
        username = request.headers.get("x-username", "anonymous")
        db = SessionLocal()
        try:
            crud.write_audit_log(
                db,
                action="permission_denied",
                operator=username,
                target_type="api_endpoint",
                target_id=request.url.path,
                result="denied",
                detail=f"访问被拒: {request.method} {request.url.path}",
            )
        finally:
            db.close()
    return response


@app.exception_handler(RequestValidationError)
async def scheduled_time_validation_handler(request: Request, exc: RequestValidationError):
    for err in exc.errors():
        if "scheduled_time" in str(err.get("loc", [])):
            msg = err.get("msg", "")
            if "无效的时间格式" in msg or "预约时间" in msg:
                username = request.headers.get("x-username", "anonymous")
                db = SessionLocal()
                try:
                    crud.write_audit_log(
                        db,
                        action="create_scheduled_release",
                        operator=username,
                        target_type="scheduled_release",
                        target_id="",
                        result="rejected",
                        detail=f"时间格式解析失败: {msg}",
                    )
                finally:
                    db.close()
                return JSONResponse(status_code=400, content={"detail": msg})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


def init_default_data(db: Session):
    if not crud.list_users(db):
        crud.create_user(db, "admin", auth.ROLE_ADMIN)
        crud.create_user(db, "approver1", auth.ROLE_APPROVER)
        crud.create_user(db, "user1", auth.ROLE_USER)

    if not crud.list_rules(db):
        default_rule = schemas.ScoringRuleCreate(
            name="默认综合评分规则",
            description="包含质量、交付、成本三个维度的默认评分规则",
            weight_config={
                "dimensions": {
                    "quality": {
                        "weight": 40,
                        "metrics": {
                            "pass_rate": {"weight": 60, "type": "higher_is_better", "baseline": 0.95, "full_score": 100},
                            "defect_rate": {"weight": 40, "type": "lower_is_better", "baseline": 0.02, "full_score": 100},
                        }
                    },
                    "delivery": {
                        "weight": 30,
                        "metrics": {
                            "on_time_rate": {"weight": 70, "type": "higher_is_better", "baseline": 0.95, "full_score": 100},
                            "lead_time_days": {"weight": 30, "type": "lower_is_better", "baseline": 15, "full_score": 100},
                        }
                    },
                    "cost": {
                        "weight": 30,
                        "metrics": {
                            "price_competitiveness": {"weight": 60, "type": "higher_is_better", "baseline": 85, "full_score": 100},
                            "payment_terms_score": {"weight": 40, "type": "higher_is_better", "baseline": 70, "full_score": 100},
                        }
                    }
                }
            }
        )
        crud.create_rule(db, default_rule)


from .database import SessionLocal

with Session(engine) as db:
    init_default_data(db)


@app.get("/")
def root():
    return {"message": "供应商评分重算服务", "version": "1.0.0"}


@app.post("/api/rules", response_model=schemas.ScoringRuleResponse)
def create_rule(
    rule: schemas.ScoringRuleCreate,
    db: Session = Depends(get_db),
    _=Depends(auth.require_role([auth.ROLE_ADMIN]))
):
    return crud.create_rule(db, rule)


@app.get("/api/rules", response_model=List[schemas.ScoringRuleResponse])
def list_rules(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    return crud.list_rules(db, skip, limit)


@app.get("/api/rules/{rule_id}", response_model=schemas.ScoringRuleResponse)
def get_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    rule = crud.get_rule(db, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="评分规则不存在")
    return rule


@app.put("/api/rules/{rule_id}", response_model=schemas.ScoringRuleResponse)
def update_rule(
    rule_id: int,
    rule_update: schemas.ScoringRuleUpdate,
    db: Session = Depends(get_db),
    _=Depends(auth.require_role([auth.ROLE_ADMIN]))
):
    rule = crud.update_rule(db, rule_id, rule_update)
    if not rule:
        raise HTTPException(status_code=404, detail="评分规则不存在")
    return rule


@app.post("/api/batches/import", response_model=schemas.SupplierBatchResponse)
def import_batch(
    batch_data: schemas.BatchImportRequest,
    db: Session = Depends(get_db),
    _=Depends(auth.require_role(auth.ALLOW_IMPORT_ROLES))
):
    try:
        result, cleared_candidate, change_log = crud.import_batch(db, batch_data)
        conflict_info = crud.check_conflict_for_import(db, batch_data.rule_id, result.id, batch_data.imported_by)
        crud.write_audit_log(
            db,
            action="import_batch",
            operator=batch_data.imported_by,
            target_type="batch",
            target_id=str(result.id),
            result="success",
            detail=f"导入批次: {batch_data.batch_name}, 供应商数: {len(batch_data.suppliers)}"
                   + (f", 冲突: {conflict_info.conflict_reason}" if conflict_info.has_conflict else ""),
        )
        if cleared_candidate and change_log:
            crud.write_audit_log(
                db,
                action="candidate_cleared_on_import",
                operator=batch_data.imported_by,
                target_type="candidate",
                target_id=str(cleared_candidate.id),
                result="cleared",
                detail=change_log.change_reason,
            )
        return result
    except ValueError as e:
        crud.write_audit_log(
            db,
            action="import_batch",
            operator=batch_data.imported_by,
            target_type="batch",
            target_id="",
            result="rejected",
            detail=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/batches", response_model=List[schemas.SupplierBatchResponse])
def list_batches(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    return crud.list_batches(db, skip, limit)


@app.get("/api/batches/{batch_id}", response_model=schemas.SupplierBatchResponse)
def get_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


@app.get("/api/batches/{batch_id}/suppliers", response_model=List[schemas.SupplierResponse])
def get_batch_suppliers(
    batch_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return crud.get_suppliers_by_batch(db, batch_id)


@app.post("/api/batches/{batch_id}/calculate", response_model=List[schemas.DraftScoreResponse])
def calculate_scores(
    batch_id: int,
    db: Session = Depends(get_db),
    _=Depends(auth.require_role(auth.ALLOW_CALCULATE_ROLES))
):
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    try:
        result = crud.calculate_draft_scores(db, batch_id)
        crud.write_audit_log(
            db,
            action="calculate",
            operator=_.username,
            target_type="batch",
            target_id=str(batch_id),
            result="success",
            detail=f"计算批次 {batch.batch_name} 的草稿分数",
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/batches/{batch_id}/drafts", response_model=List[schemas.DraftScoreResponse])
def get_draft_scores(
    batch_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return crud.get_draft_scores(db, batch_id)


@app.post("/api/batches/{batch_id}/release", response_model=schemas.ReleaseVersionResponse)
def approve_and_release(
    batch_id: int,
    approve_data: schemas.ApproveRequest,
    db: Session = Depends(get_db),
    _=Depends(auth.require_role(auth.ALLOW_RELEASE_ROLES))
):
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    try:
        conflict_info = crud.check_conflict_for_manual_release(db, batch.rule_id, batch_id, approve_data.approved_by)
        result = crud.approve_and_release(db, batch_id, approve_data)
        crud.write_audit_log(
            db,
            action="release",
            operator=approve_data.approved_by,
            target_type="version",
            target_id=result.version,
            result="success",
            detail=f"发布批次 {batch_id} 为版本 {result.version}, 审批备注: {approve_data.approval_remark}"
                   + (f", 冲突: {conflict_info.conflict_reason}" if conflict_info.has_conflict else ""),
        )
        return result
    except ValueError as e:
        error_detail = str(e)
        is_duplicate = "已发布过" in error_detail
        if is_duplicate and crud.has_duplicate_rejected_audit(db, str(batch_id)):
            raise HTTPException(status_code=400, detail=error_detail)
        crud.write_audit_log(
            db,
            action="release",
            operator=approve_data.approved_by,
            target_type="batch",
            target_id=str(batch_id),
            result="rejected" if not is_duplicate else "duplicate_rejected",
            detail=error_detail,
        )
        raise HTTPException(status_code=400, detail=error_detail)


@app.get("/api/releases", response_model=List[schemas.ReleaseVersionResponse])
def list_releases(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    return crud.list_releases(db, skip, limit)


@app.get("/api/releases/active", response_model=schemas.ReleaseVersionResponse)
def get_active_release(
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    release = crud.get_active_release(db)
    if not release:
        raise HTTPException(status_code=404, detail="当前没有活动版本")
    return release


@app.get("/api/releases/{release_id}", response_model=schemas.ReleaseVersionResponse)
def get_release(
    release_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    release = crud.get_release(db, release_id)
    if not release:
        raise HTTPException(status_code=404, detail="发布版本不存在")
    return release


@app.get("/api/releases/{release_id}/scores", response_model=List[schemas.ReleasedScoreResponse])
def get_release_scores(
    release_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    release = crud.get_release(db, release_id)
    if not release:
        raise HTTPException(status_code=404, detail="发布版本不存在")
    return crud.get_released_scores(db, release_id)


@app.post("/api/rollback", response_model=dict)
def rollback_version(
    rollback_data: schemas.RollbackRequest,
    db: Session = Depends(get_db),
    _=Depends(auth.require_role(auth.ALLOW_ROLLBACK_ROLES))
):
    try:
        release, record = crud.rollback_to_version(db, rollback_data)
        crud.write_audit_log(
            db,
            action="rollback",
            operator=rollback_data.operated_by,
            target_type="version",
            target_id=release.version,
            result="success",
            detail=f"从 {record.from_version} 回滚到 {record.to_version}, 原因: {rollback_data.reason}",
        )
        return {
            "active_release": schemas.ReleaseVersionResponse.model_validate(release).model_dump(),
            "rollback_record": schemas.RollbackRecordResponse.model_validate(record).model_dump()
        }
    except ValueError as e:
        crud.write_audit_log(
            db,
            action="rollback",
            operator=rollback_data.operated_by,
            target_type="version",
            target_id=rollback_data.target_version,
            result="rejected",
            detail=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/rollback-records", response_model=List[schemas.RollbackRecordResponse])
def list_rollback_records(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    return crud.list_rollback_records(db, skip, limit)


@app.get("/api/export/active", response_model=schemas.ExportResponse)
def export_active(
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    result = crud.export_active_scores(db)
    if not result:
        raise HTTPException(status_code=404, detail="当前没有活动版本")

    if len(result) == 5:
        release, scores, candidate_batch_id, candidate_matches_active, release_source = result
        source_plan_detail = ""
        plan_status = None
    else:
        release, scores, candidate_batch_id, candidate_matches_active, release_source, source_plan_detail, plan_status = result

    score_items = [
        schemas.ExportResultItem(
            supplier_code=s.supplier_code,
            supplier_name=s.supplier_name,
            total_score=s.total_score,
            grade=s.grade,
            score_details=s.score_details,
        ) for s in scores
    ]

    release_source_display = release_source or "manual"
    if False and source_plan_detail and plan_status:  # 保留release_source纯净值，兼容性优先
        release_source_display = f"{release_source_display}|status={plan_status}"

    return schemas.ExportResponse(
        version=release.version,
        released_at=release.released_at,
        approved_by=release.approved_by,
        release_note=release.release_note,
        supplier_count=release.supplier_count,
        scores=score_items,
        candidate_batch_id=candidate_batch_id,
        candidate_matches_active=candidate_matches_active,
        release_source=release_source_display,
        plan_status=plan_status,
    )


@app.get("/api/audit-logs", response_model=List[schemas.AuditLogResponse])
def list_audit_logs(
    action: Optional[str] = None,
    operator: Optional[str] = None,
    target_type: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    return crud.list_audit_logs(db, action=action, operator=operator, target_type=target_type, skip=skip, limit=limit)


@app.get("/api/users", response_model=List[schemas.UserResponse])
def list_users(
    db: Session = Depends(get_db),
    _=Depends(auth.require_role([auth.ROLE_ADMIN]))
):
    return crud.list_users(db)


@app.post("/api/candidate/set", response_model=dict)
def set_candidate(
    candidate_data: schemas.SetCandidateRequest,
    db: Session = Depends(get_db),
    _=Depends(auth.require_role(auth.ALLOW_CANDIDATE_ROLES))
):
    try:
        candidate, change_log = crud.set_release_candidate(db, candidate_data)
        crud.write_audit_log(
            db,
            action="set_candidate",
            operator=candidate_data.set_by,
            target_type="candidate",
            target_id=str(candidate.id),
            result="success",
            detail=f"设置批次{candidate_data.batch_id}为候选发布, 变更说明: {candidate_data.change_description}",
        )
        if change_log.old_candidate_id:
            crud.write_audit_log(
                db,
                action="candidate_replaced",
                operator=candidate_data.set_by,
                target_type="candidate",
                target_id=str(change_log.old_candidate_id),
                result="replaced",
                detail=f"候选被顶替: 旧候选ID={change_log.old_candidate_id}, 新候选ID={change_log.new_candidate_id}, 原因: {change_log.change_reason}",
            )
        return {
            "candidate": schemas.ReleaseCandidateResponse.model_validate(candidate).model_dump(),
            "change_log": schemas.CandidateChangeLogResponse.model_validate(change_log).model_dump(),
        }
    except ValueError as e:
        crud.write_audit_log(
            db,
            action="set_candidate",
            operator=candidate_data.set_by,
            target_type="candidate",
            target_id=str(candidate_data.batch_id),
            result="rejected",
            detail=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/candidate/cancel", response_model=dict)
def cancel_candidate(
    operated_by: str,
    reason: str = "",
    db: Session = Depends(get_db),
    _=Depends(auth.require_role(auth.ALLOW_CANDIDATE_ROLES))
):
    current_before = crud.get_current_candidate(db)
    conflict_info_before = None
    rule_id_for_check = None
    if current_before:
        rule_id_for_check = current_before.rule_id
        conflict_info_before = crud.check_conflict_for_cancel_candidate(db, rule_id_for_check, operated_by)
    else:
        crud.write_audit_log(
            db,
            action="cancel_candidate",
            operator=operated_by,
            target_type="candidate",
            target_id="none",
            result="not_found",
            detail=f"尝试取消候选但当前无候选存在, 原因: {reason or '手动取消候选'}",
        )
        raise HTTPException(status_code=404, detail="当前没有候选发布")

    result = crud.clear_candidate(db, reason=reason or "手动取消候选", operated_by=operated_by)
    if not result:
        crud.write_audit_log(
            db,
            action="cancel_candidate",
            operator=operated_by,
            target_type="candidate",
            target_id="none",
            result="not_found",
            detail=f"尝试取消候选但当前无候选存在(race), 原因: {reason or '手动取消候选'}",
        )
        raise HTTPException(status_code=404, detail="当前没有候选发布")
    old_candidate, change_log, plan_handle_result = result
    conflict_info = conflict_info_before
    audit_detail_parts = [
        f"取消候选批次{old_candidate.batch_id}",
        f"原因: {reason or '手动取消候选'}",
    ]
    if conflict_info.has_conflict:
        audit_detail_parts.append(f"冲突命中: {conflict_info.conflict_reason}")
        audit_detail_parts.append(f"建议动作: {conflict_info.suggestion or '联动取消'}")
    if plan_handle_result:
        audit_detail_parts.append(
            f"计划联动处理: 总计={plan_handle_result.get('total_found', 0)}, "
            f"成功取消={len(plan_handle_result.get('cancelled_ids', []))}, "
            f"幂等跳过={len(plan_handle_result.get('skipped_ids', []))}, "
            f"触发人={plan_handle_result.get('triggered_by', operated_by)}, "
            f"动作类型={plan_handle_result.get('action', 'linked_cancel')}"
        )
        cancelled_ids = plan_handle_result.get("cancelled_ids", [])
        skipped_ids = plan_handle_result.get("skipped_ids", [])
        if cancelled_ids:
            audit_detail_parts.append(f"已取消计划ID: {cancelled_ids}")
        if skipped_ids:
            audit_detail_parts.append(f"幂等跳过计划ID: {skipped_ids}")
        for d in plan_handle_result.get("details", []):
            audit_detail_parts.append(
                f"  - plan#{d.get('plan_id')}: action={d.get('action')}, "
                f"type={d.get('plan_type')}, source={d.get('source_type')}"
                + (f", old_status={d.get('old_status')}->new_status={d.get('new_status')}" if d.get("old_status") else "")
            )
    crud.write_audit_log(
        db,
        action="cancel_candidate",
        operator=operated_by,
        target_type="candidate",
        target_id=str(old_candidate.id),
        result="success",
        detail=" | ".join(audit_detail_parts),
    )
    return {
        "candidate": schemas.ReleaseCandidateResponse.model_validate(old_candidate).model_dump(),
        "change_log": schemas.CandidateChangeLogResponse.model_validate(change_log).model_dump(),
        "conflict_info": {
            "has_conflict": conflict_info.has_conflict,
            "conflict_type": conflict_info.conflict_type,
            "conflict_plan_id": conflict_info.conflict_plan_id,
            "conflict_reason": conflict_info.conflict_reason,
            "suggestion": conflict_info.suggestion,
        } if conflict_info else {"has_conflict": False},
        "plan_linked_result": plan_handle_result or {
            "cancelled_ids": [],
            "skipped_ids": [],
            "total_found": 0,
            "action": "none",
            "details": [],
        },
    }


@app.get("/api/candidate/current", response_model=schemas.ReleaseCandidateResponse)
def get_current_candidate(
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    candidate = crud.get_current_candidate(db)
    if not candidate:
        raise HTTPException(status_code=404, detail="当前没有候选发布")
    return candidate


@app.get("/api/candidate/change-log/latest", response_model=schemas.CandidateChangeLogResponse)
def get_latest_candidate_change(
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    change_log = crud.get_latest_candidate_change_log(db)
    if not change_log:
        raise HTTPException(status_code=404, detail="没有候选变更记录")
    return change_log


@app.get("/api/candidate/change-logs", response_model=List[schemas.CandidateChangeLogResponse])
def list_candidate_change_logs(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user)
):
    return crud.list_candidate_change_logs(db, skip, limit)


@app.post("/api/scheduled-releases", response_model=dict)
def create_scheduled_release(
    req: schemas.ScheduleReleaseRequest,
    db: Session = Depends(get_db),
    _=Depends(auth.require_role(auth.ALLOW_CANDIDATE_ROLES)),
):
    try:
        sched, candidate, change_log = crud.create_scheduled_release(db, req)
        crud.write_audit_log(
            db,
            action="create_scheduled_release",
            operator=req.set_by,
            target_type="scheduled_release",
            target_id=str(sched.id),
            result="success",
            detail=f"创建预约发布: 批次={req.batch_id}, 计划生效时间={req.scheduled_time.isoformat()}",
        )
        crud.write_audit_log(
            db,
            action="set_candidate",
            operator=req.set_by,
            target_type="candidate",
            target_id=str(candidate.id),
            result="success",
            detail=f"设置批次{req.batch_id}为预约发布候选, 变更说明: {req.change_description}",
        )
        if change_log.old_candidate_id:
            crud.write_audit_log(
                db,
                action="candidate_replaced",
                operator=req.set_by,
                target_type="candidate",
                target_id=str(change_log.old_candidate_id),
                result="replaced",
                detail=f"候选被顶替(预约): 旧候选ID={change_log.old_candidate_id}, 新候选ID={change_log.new_candidate_id}",
            )
        archive = crud.get_archive_by_scheduled_release_id(db, sched.id)
        return {
            "id": sched.id,
            "release_archive_id": archive.id if archive else None,
            "scheduled_release": schemas.ScheduledReleaseResponse.model_validate(sched).model_dump(),
            "candidate": schemas.ReleaseCandidateResponse.model_validate(candidate).model_dump(),
            "change_log": schemas.CandidateChangeLogResponse.model_validate(change_log).model_dump(),
        }
    except ValueError as e:
        crud.write_audit_log(
            db,
            action="create_scheduled_release",
            operator=req.set_by,
            target_type="scheduled_release",
            target_id=str(req.batch_id),
            result="rejected",
            detail=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/scheduled-releases/{sched_id}/cancel", response_model=dict)
def cancel_scheduled_release(
    sched_id: int,
    operated_by: str,
    reason: str = "",
    db: Session = Depends(get_db),
    _=Depends(auth.require_role(auth.ALLOW_CANDIDATE_ROLES)),
):
    sched_before = crud.get_scheduled_release(db, sched_id)
    if not sched_before:
        crud.write_audit_log(
            db,
            action="cancel_scheduled_release",
            operator=operated_by,
            target_type="scheduled_release",
            target_id=str(sched_id),
            result="not_found",
            detail=f"尝试取消预约但记录不存在, sched_id={sched_id}, 原因: {reason or '手动取消预约'}",
        )
        raise HTTPException(status_code=404, detail="预约发布记录不存在")
    if sched_before.status not in ("pending",):
        crud.write_audit_log(
            db,
            action="cancel_scheduled_release",
            operator=operated_by,
            target_type="scheduled_release",
            target_id=str(sched_id),
            result="rejected",
            detail=f"取消预约被拒绝: 当前状态={sched_before.status}, 仅pending状态可取消, "
                   f"批次={sched_before.batch_id}, 原因: {reason or '手动取消预约'}",
        )
        raise HTTPException(
            status_code=400,
            detail=f"预约状态为{sched_before.status}，不能取消（仅pending状态可取消）"
        )
    try:
        sched, change_log, plan_result = crud.cancel_scheduled_release(
            db, sched_id, reason=reason or "手动取消预约", operated_by=operated_by
        )
        audit_parts = [
            f"取消预约发布: 批次={sched.batch_id}",
            f"sched_id={sched.id}",
            f"原因: {reason or '手动取消预约'}",
            f"状态: {sched_before.status}->{sched.status}",
        ]
        if plan_result:
            audit_parts.append(
                f"计划联动: action={plan_result.get('action_taken')}, "
                f"plan_id={plan_result.get('plan_id')}, "
                f"type={plan_result.get('plan_type')}, "
                f"source={plan_result.get('source_type')}"
            )
        crud.write_audit_log(
            db,
            action="cancel_scheduled_release",
            operator=operated_by,
            target_type="scheduled_release",
            target_id=str(sched.id),
            result="success",
            detail=" | ".join(audit_parts),
        )
        if change_log:
            crud.write_audit_log(
                db,
                action="cancel_candidate",
                operator=operated_by,
                target_type="candidate",
                target_id=str(change_log.old_candidate_id),
                result="success",
                detail=f"取消候选(随预约取消联动): sched_id={sched.id}, "
                       f"批次={sched.batch_id}, 原因={reason or '手动取消预约'}",
            )
        return {
            "scheduled_release": schemas.ScheduledReleaseResponse.model_validate(sched).model_dump(),
            "change_log": schemas.CandidateChangeLogResponse.model_validate(change_log).model_dump() if change_log else None,
            "plan_linked_result": plan_result or {"plan_id": None, "action_taken": "none"},
        }
    except ValueError as e:
        crud.write_audit_log(
            db,
            action="cancel_scheduled_release",
            operator=operated_by,
            target_type="scheduled_release",
            target_id=str(sched_id),
            result="rejected",
            detail=f"取消预约失败: {str(e)}, 原因: {reason or '手动取消预约'}",
        )
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/scheduled-releases", response_model=List[schemas.ScheduledReleaseResponse])
def list_scheduled_releases(
    status: Optional[str] = None,
    rule_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    return crud.list_scheduled_releases(db, status=status, rule_id=rule_id, skip=skip, limit=limit)


@app.get("/api/scheduled-releases/{sched_id}", response_model=schemas.ScheduledReleaseDetailResponse)
def get_scheduled_release(
    sched_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    sched = crud.get_scheduled_release(db, sched_id)
    if not sched:
        raise HTTPException(status_code=404, detail="预约发布记录不存在")
    cancel_reason_out = ""
    if sched.status == "cancelled":
        cancel_reason_out = sched.cancel_reason
    return schemas.ScheduledReleaseDetailResponse(
        id=sched.id,
        candidate_id=sched.candidate_id,
        batch_id=sched.batch_id,
        rule_id=sched.rule_id,
        scheduled_time=sched.scheduled_time,
        status=sched.status,
        cancel_reason=cancel_reason_out,
        release_version_id=sched.release_version_id,
        created_by=sched.created_by,
        created_at=sched.created_at,
        executed_at=sched.executed_at,
        cancelled_at=sched.cancelled_at if sched.status == "cancelled" else None,
        cancelled_by=sched.cancelled_by if sched.status == "cancelled" else None,
        candidate=schemas.ReleaseCandidateResponse.model_validate(sched.candidate) if sched.candidate else None,
        release_version=schemas.ReleaseVersionResponse.model_validate(sched.release_version) if sched.release_version else None,
    )


@app.get("/api/scheduled-releases/rule/{rule_id}/latest", response_model=schemas.ScheduledReleaseResponse)
def get_latest_schedule_for_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    sched = crud.get_latest_schedule_for_rule(db, rule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="该规则没有预约发布记录")
    return sched


@app.get("/api/_meta/schema-version")
def get_schema_version_endpoint(
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    return {
        "schema_version": plan_migrations.get_current_version(db),
        "target_version": plan_migrations.CURRENT_SCHEMA_VERSION,
    }


@app.get("/api/release-plans", response_model=List[schemas.ReleasePlanResponse])
def list_release_plans(
    rule_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    plan_type: Optional[str] = Query(None),
    batch_id: Optional[int] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    if status and status not in schemas.VALID_PLAN_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"无效状态: {status}。允许值: {', '.join(sorted(schemas.VALID_PLAN_STATUSES))}"
        )
    if source_type and source_type not in schemas.VALID_PLAN_SOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"无效来源类型: {source_type}。允许值: {', '.join(sorted(schemas.VALID_PLAN_SOURCE_TYPES))}"
        )
    if plan_type and plan_type not in schemas.VALID_PLAN_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"无效计划类型: {plan_type}。允许值: {', '.join(sorted(schemas.VALID_PLAN_TYPES))}"
        )
    return crud.list_plans(db, rule_id=rule_id, status=status, source_type=source_type,
                           plan_type=plan_type, batch_id=batch_id, skip=skip, limit=limit)


@app.get("/api/release-plans/{plan_id}", response_model=schemas.ReleasePlanDetailResponse)
def get_release_plan_detail(
    plan_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    plan = crud.get_plan_by_id(db, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="发布计划不存在")
    events = crud.get_plan_events(db, plan_id, limit=200)
    return schemas.ReleasePlanDetailResponse(
        id=plan.id,
        rule_id=plan.rule_id,
        batch_id=plan.batch_id,
        candidate_id=plan.candidate_id,
        scheduled_release_id=plan.scheduled_release_id,
        release_version_id=plan.release_version_id,
        status=plan.status,
        source_type=plan.source_type,
        plan_type=plan.plan_type,
        planned_time=plan.planned_time,
        executed_at=plan.executed_at,
        expired_at=plan.expired_at,
        cancelled_at=plan.cancelled_at,
        superseded_at=plan.superseded_at,
        superseded_by_plan_id=plan.superseded_by_plan_id,
        conflict_reason=plan.conflict_reason,
        source_detail=plan.source_detail,
        created_by=plan.created_by,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        batch=schemas.SupplierBatchResponse.model_validate(plan.batch) if hasattr(plan, 'batch') and plan.batch else None,
        candidate=schemas.ReleaseCandidateResponse.model_validate(plan.candidate) if hasattr(plan, 'candidate') and plan.candidate else None,
        scheduled_release=schemas.ScheduledReleaseResponse.model_validate(plan.scheduled_release) if hasattr(plan, 'scheduled_release') and plan.scheduled_release else None,
        release_version=schemas.ReleaseVersionResponse.model_validate(plan.release_version) if hasattr(plan, 'release_version') and plan.release_version else None,
        events=[schemas.ReleasePlanEventResponse.model_validate(e) for e in events],
    )


@app.get("/api/release-plans/{plan_id}/events", response_model=List[schemas.ReleasePlanEventResponse])
def get_release_plan_events(
    plan_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    plan = crud.get_plan_by_id(db, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="发布计划不存在")
    return crud.get_plan_events(db, plan_id, skip=skip, limit=limit)


@app.get("/api/release-plans-stats", response_model=List[schemas.ReleasePlanStatsResponse])
def get_release_plans_stats(
    rule_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    if rule_id is not None:
        return [crud.get_plan_stats(db, rule_id=rule_id)]
    stats_all = crud.get_plan_stats(db, rule_id=None)
    rule_ids = db.query(models.ReleasePlan.rule_id).distinct().all()
    result = [stats_all]
    for (rid,) in rule_ids:
        if rid:
            result.append(crud.get_plan_stats(db, rule_id=rid))
    return result


@app.get("/api/release-plan-configs", response_model=List[schemas.ReleasePlanConfigResponse])
def list_release_plan_configs(
    rule_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(auth.require_role([auth.ROLE_ADMIN])),
):
    return crud.list_plan_configs(db, rule_id=rule_id)


@app.post("/api/release-plan-configs/validate", response_model=schemas.PlanConfigValidateResponse)
def validate_release_plan_config(
    req: schemas.PlanConfigValidateRequest,
    _=Depends(auth.require_role([auth.ROLE_ADMIN])),
):
    valid, error, normalized = crud.validate_plan_config(req.config_key, req.config_value)
    return schemas.PlanConfigValidateResponse(
        valid=valid,
        config_key=req.config_key,
        config_value=req.config_value,
        error_message=error if not valid else None,
        normalized_value=normalized if valid else None,
    )


@app.put("/api/release-plan-configs", response_model=schemas.ReleasePlanConfigResponse)
def update_release_plan_config(
    config_key: str = Query(..., description="配置键名"),
    config_value: str = Query(..., description="配置值"),
    rule_id: Optional[int] = Query(None, description="规则ID，None表示全局配置"),
    description: Optional[str] = Query("", description="配置说明"),
    db: Session = Depends(get_db),
    user=Depends(auth.require_role([auth.ROLE_ADMIN])),
):
    try:
        cfg = crud.set_plan_config(
            db, config_key=config_key, config_value=config_value,
            updated_by=user.username, rule_id=rule_id, description=description,
        )
        crud.write_audit_log(
            db,
            action="update_plan_config",
            operator=user.username,
            target_type="release_plan_config",
            target_id=f"{rule_id}:{config_key}" if rule_id else f"global:{config_key}",
            result="success",
            detail=f"更新计划配置: {config_key}={config_value}" + (f" (rule_id={rule_id})" if rule_id else " (全局)"),
        )
        return cfg
    except ValueError as e:
        crud.write_audit_log(
            db,
            action="update_plan_config",
            operator=user.username,
            target_type="release_plan_config",
            target_id=f"{rule_id}:{config_key}" if rule_id else f"global:{config_key}",
            result="rejected",
            detail=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/release-plans/check-conflict/import")
def check_conflict_import(
    rule_id: int = Query(..., description="规则ID"),
    new_batch_id: int = Query(..., description="新批次ID"),
    imported_by: str = Query("admin", description="导入人"),
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    return crud.check_conflict_for_import(db, rule_id=rule_id, new_batch_id=new_batch_id, imported_by=imported_by)


@app.get("/api/release-plans/check-conflict/manual-release")
def check_conflict_manual_release(
    rule_id: int = Query(..., description="规则ID"),
    batch_id: int = Query(..., description="要发布的批次ID"),
    released_by: str = Query("admin", description="发布人"),
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    return crud.check_conflict_for_manual_release(db, rule_id=rule_id, batch_id=batch_id, released_by=released_by)


@app.get("/api/release-plans/check-conflict/cancel-candidate")
def check_conflict_cancel_candidate(
    rule_id: int = Query(..., description="规则ID"),
    cancelled_by: str = Query("admin", description="取消人"),
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    return crud.check_conflict_for_cancel_candidate(db, rule_id=rule_id, cancelled_by=cancelled_by)


@app.get("/api/release-plans/check-conflict/rollback")
def check_conflict_rollback(
    rule_id: int = Query(..., description="规则ID"),
    target_version: str = Query(..., description="目标版本"),
    operated_by: str = Query("admin", description="操作人"),
    db: Session = Depends(get_db),
    user=Depends(auth.get_current_user),
):
    return crud.check_conflict_for_rollback(db, rule_id=rule_id, target_version=target_version, operated_by=operated_by)


@app.post("/api/release-plans/trigger-expire", response_model=dict)
def trigger_expire_stale_plans(
    db: Session = Depends(get_db),
    _=Depends(auth.require_role([auth.ROLE_ADMIN])),
):
    expired = crud.expire_stale_plans(db)
    return {"expired_count": len(expired), "expired_plan_ids": expired}


@app.post("/api/release-plans/recover", response_model=dict)
def trigger_recover_plans(
    db: Session = Depends(get_db),
    _=Depends(auth.require_role([auth.ROLE_ADMIN])),
):
    stats = crud.recover_plans_on_restart(db)
    return stats


@app.get("/api/release-archives/check-conflict/import", response_model=schemas.ReleaseArchiveImportConflictInfo)
def check_archive_import_conflict(
    rule_id: int = Query(..., description="规则ID"),
    new_batch_id: int = Query(..., description="新批次ID"),
    imported_by: str = Query("admin", description="导入人"),
    db: Session = Depends(get_db),
    user=Depends(auth.require_role(auth.ALLOW_ARCHIVE_VIEW_ROLES)),
):
    return crud.check_archive_import_conflict(db, rule_id=rule_id, new_batch_id=new_batch_id, imported_by=imported_by)


@app.get("/api/release-archives", response_model=List[schemas.ReleaseArchiveResponse])
def list_release_archives(
    scheduled_release_id: Optional[int] = Query(None),
    release_plan_id: Optional[int] = Query(None),
    release_version_id: Optional[int] = Query(None),
    source_batch_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    conflict_result: Optional[str] = Query(None),
    triggered_by: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    user=Depends(auth.require_role(auth.ALLOW_ARCHIVE_VIEW_ROLES)),
):
    if status and status not in schemas.VALID_ARCHIVE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"无效状态: {status}。允许值: {', '.join(sorted(schemas.VALID_ARCHIVE_STATUSES))}"
        )
    if conflict_result and conflict_result not in schemas.VALID_ARCHIVE_CONFLICTS:
        raise HTTPException(
            status_code=400,
            detail=f"无效冲突结果: {conflict_result}。允许值: {', '.join(sorted(schemas.VALID_ARCHIVE_CONFLICTS))}"
        )
    archives = crud.list_archives(
        db,
        scheduled_release_id=scheduled_release_id,
        release_plan_id=release_plan_id,
        release_version_id=release_version_id,
        source_batch_id=source_batch_id,
        status=status,
        conflict_result=conflict_result,
        triggered_by=triggered_by,
        skip=skip,
        limit=limit,
        requesting_username=user.username,
        requesting_user_role=user.role,
    )
    return archives


@app.get("/api/release-archives/{archive_id}", response_model=schemas.ReleaseArchiveDetailResponse)
def get_release_archive_detail(
    archive_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.require_role(auth.ALLOW_ARCHIVE_VIEW_ROLES)),
):
    archive = crud.get_archive_by_id(db, archive_id)
    if not archive:
        crud.write_audit_log(
            db,
            action="archive_view",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="not_found",
            detail=f"查看档案失败: 档案不存在",
        )
        raise HTTPException(status_code=404, detail="档案不存在")

    permission_ok, permission_msg = crud.check_archive_permission(
        db, archive_id, user.username, auth.ALLOW_ARCHIVE_VIEW_ROLES, user.role, "view"
    )
    if not permission_ok:
        crud.write_audit_log(
            db,
            action="archive_view",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="forbidden",
            detail=f"查看档案被拒: {permission_msg}",
        )
        raise HTTPException(status_code=403, detail=permission_msg)

    references = crud.get_archive_references(db, archive_id, limit=200)
    processing_log = []
    if isinstance(archive.processing_log, list):
        processing_log = [schemas.ProcessingLogEntry(**log) for log in archive.processing_log]

    crud.write_audit_log(
        db,
        action="archive_view",
        operator=user.username,
        target_type="release_archive",
        target_id=str(archive_id),
        result="success",
        detail=f"查看档案详情，状态={archive.status}",
    )

    return schemas.ReleaseArchiveDetailResponse(
        id=archive.id,
        scheduled_release_id=archive.scheduled_release_id,
        release_plan_id=archive.release_plan_id,
        release_version_id=archive.release_version_id,
        release_note=archive.release_note,
        approval_remark=archive.approval_remark,
        triggered_by=archive.triggered_by,
        source_batch_id=archive.source_batch_id,
        target_version=archive.target_version,
        execution_strategy=archive.execution_strategy,
        scheduled_time=archive.scheduled_time,
        status=archive.status,
        conflict_result=archive.conflict_result,
        conflict_detail=archive.conflict_detail,
        context_snapshot=archive.context_snapshot if isinstance(archive.context_snapshot, dict) else {},
        snapshot_hash=archive.snapshot_hash,
        is_immutable=archive.is_immutable,
        created_at=archive.created_at,
        archived_at=archive.archived_at,
        last_processed_at=archive.last_processed_at,
        recovered_after_restart=archive.recovered_after_restart,
        processing_log=processing_log,
        reference_count=archive.reference_count,
        scheduled_release=schemas.ScheduledReleaseResponse.model_validate(archive.scheduled_release) if archive.scheduled_release else None,
        release_plan=schemas.ReleasePlanResponse.model_validate(archive.release_plan) if archive.release_plan else None,
        release_version=schemas.ReleaseVersionResponse.model_validate(archive.release_version) if archive.release_version else None,
        references=[schemas.ReleaseArchiveReferenceResponse.model_validate(ref) for ref in references],
    )


@app.get("/api/release-archives/{archive_id}/export", response_model=schemas.ReleaseArchiveExportResponse)
def export_release_archive(
    archive_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.require_role(auth.ALLOW_ARCHIVE_EXPORT_ROLES)),
):
    archive = crud.get_archive_by_id(db, archive_id)
    if not archive:
        crud.write_audit_log(
            db,
            action="archive_export",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="not_found",
            detail=f"导出档案失败: 档案不存在",
        )
        raise HTTPException(status_code=404, detail="档案不存在")

    permission_ok, permission_msg = crud.check_archive_permission(
        db, archive_id, user.username, auth.ALLOW_ARCHIVE_EXPORT_ROLES, user.role, "export"
    )
    if not permission_ok:
        crud.write_audit_log(
            db,
            action="archive_export",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="forbidden",
            detail=f"导出档案被拒: {permission_msg}",
        )
        raise HTTPException(status_code=403, detail=permission_msg)

    result = crud.export_archive(db, archive_id, user.username)
    if not result:
        raise HTTPException(status_code=500, detail="导出失败")

    crud.write_audit_log(
        db,
        action="archive_export",
        operator=user.username,
        target_type="release_archive",
        target_id=str(archive_id),
        result="success",
        detail=f"导出档案，hash={archive.snapshot_hash[:16]}...",
    )

    export_items = [schemas.ReleaseArchiveExportItem(**item) for item in result["items"]]
    processing_log_entries = [schemas.ProcessingLogEntry(**log) for log in result.get("processing_log", [])]
    return schemas.ReleaseArchiveExportResponse(
        archive_id=result["archive_id"],
        snapshot_hash=result["snapshot_hash"],
        export_time=result["export_time"],
        exported_by=result["exported_by"],
        items=export_items,
        processing_log=processing_log_entries,
        scheduled_release=schemas.ScheduledReleaseResponse.model_validate(result["scheduled_release"]) if result["scheduled_release"] else None,
        release_version=schemas.ReleaseVersionResponse.model_validate(result["release_version"]) if result["release_version"] else None,
    )


@app.get("/api/release-archives/{archive_id}/audit-trail")
def get_archive_audit_trail(
    archive_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.require_role(auth.ALLOW_ARCHIVE_AUDIT_ROLES)),
):
    archive = crud.get_archive_by_id(db, archive_id)
    if not archive:
        crud.write_audit_log(
            db,
            action="archive_audit",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="not_found",
            detail=f"审计档案失败: 档案不存在",
        )
        raise HTTPException(status_code=404, detail="档案不存在")

    permission_ok, permission_msg = crud.check_archive_permission(
        db, archive_id, user.username, auth.ALLOW_ARCHIVE_AUDIT_ROLES, user.role, "audit"
    )
    if not permission_ok:
        crud.write_audit_log(
            db,
            action="archive_audit",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="forbidden",
            detail=f"审计档案被拒: {permission_msg}",
        )
        raise HTTPException(status_code=403, detail=permission_msg)

    trail = crud.get_archive_audit_trail(db, archive_id)
    if not trail:
        raise HTTPException(status_code=500, detail="获取审计链路失败")

    verify_result = crud.verify_archive_snapshot(db, archive_id)
    trail["snapshot_verified"] = verify_result["verified"]
    trail["snapshot_hash_match"] = verify_result["verified"]

    crud.write_audit_log(
        db,
        action="archive_audit",
        operator=user.username,
        target_type="release_archive",
        target_id=str(archive_id),
        result="success",
        detail=f"查看审计链路，快照完整性={'验证通过' if verify_result['verified'] else '验证失败'}",
    )

    return trail


@app.get("/api/release-archives/{archive_id}/verify")
def verify_archive(
    archive_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.require_role(auth.ALLOW_ARCHIVE_AUDIT_ROLES)),
):
    archive = crud.get_archive_by_id(db, archive_id)
    if not archive:
        raise HTTPException(status_code=404, detail="档案不存在")

    result = crud.verify_archive_snapshot(db, archive_id)

    crud.write_audit_log(
        db,
        action="archive_verify",
        operator=user.username,
        target_type="release_archive",
        target_id=str(archive_id),
        result="success" if result["verified"] else "failed",
        detail=f"快照完整性校验，结果={'通过' if result['verified'] else '失败'}",
    )

    return result


@app.post("/api/release-archives/verify-fields", response_model=schemas.ReleaseArchiveVerifyResponse)
def verify_archive_fields(
    req: schemas.ReleaseArchiveVerifyRequest,
    db: Session = Depends(get_db),
    user=Depends(auth.require_role(auth.ALLOW_ARCHIVE_AUDIT_ROLES)),
):
    archive = crud.get_archive_by_id(db, req.archive_id)
    if not archive:
        raise HTTPException(status_code=404, detail="档案不存在")

    snapshot_result = crud.verify_archive_snapshot(db, req.archive_id)

    matched_fields = []
    mismatched_fields = []
    details = {
        "snapshot_verified": snapshot_result["verified"],
        "field_comparison": {},
    }

    snapshot_field_map = {
        "release_note": archive.release_note,
        "approval_remark": archive.approval_remark,
        "triggered_by": archive.triggered_by,
        "source_batch_id": archive.source_batch_id,
        "target_version": archive.target_version,
        "execution_strategy": archive.execution_strategy,
        "scheduled_release_id": archive.scheduled_release_id,
        "scheduled_time": archive.scheduled_time.isoformat() if archive.scheduled_time else None,
    }

    for field, expected_value in req.expected_fields.items():
        actual_value = snapshot_field_map.get(field)
        match = str(actual_value) == str(expected_value)
        if match:
            matched_fields.append(field)
        else:
            mismatched_fields.append(field)
        details["field_comparison"][field] = {
            "expected": expected_value,
            "actual": actual_value,
            "match": match,
            "is_snapshot_field": field in snapshot_field_map,
        }

    all_matched = len(mismatched_fields) == 0 and snapshot_result["verified"]

    crud.write_audit_log(
        db,
        action="archive_verify_fields",
        operator=user.username,
        target_type="release_archive",
        target_id=str(req.archive_id),
        result="success" if all_matched else "mismatch",
        detail=f"字段校验，匹配={len(matched_fields)}个，不匹配={len(mismatched_fields)}个",
    )

    return schemas.ReleaseArchiveVerifyResponse(
        archive_id=req.archive_id,
        verified=all_matched,
        matched_fields=matched_fields,
        mismatched_fields=mismatched_fields,
        details=details,
    )


@app.post("/api/release-archives/{archive_id}/cancel", response_model=dict)
def cancel_archive(
    archive_id: int,
    reason: str = Query(..., description="取消原因"),
    db: Session = Depends(get_db),
    user=Depends(auth.require_role(auth.ALLOW_ARCHIVE_CANCEL_ROLES)),
):
    archive = crud.get_archive_by_id(db, archive_id)
    if not archive:
        crud.write_audit_log(
            db,
            action="archive_cancel",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="not_found",
            detail=f"取消档案失败: 档案不存在",
        )
        raise HTTPException(status_code=404, detail="档案不存在")

    permission_ok, permission_msg = crud.check_archive_permission(
        db, archive_id, user.username, auth.ALLOW_ARCHIVE_CANCEL_ROLES, user.role, "cancel"
    )
    if not permission_ok:
        crud.write_audit_log(
            db,
            action="archive_cancel",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="forbidden",
            detail=f"取消档案被拒: {permission_msg}",
        )
        raise HTTPException(status_code=403, detail=permission_msg)

    terminal_statuses = {models.ARCHIVE_STATUS_EXECUTED, models.ARCHIVE_STATUS_CANCELLED,
                         models.ARCHIVE_STATUS_SUPERSEDED, models.ARCHIVE_STATUS_FAILED}
    if archive.status in terminal_statuses:
        crud.write_audit_log(
            db,
            action="archive_cancel",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="rejected",
            detail=f"取消档案被拒: 已处于终态{archive.status}",
        )
        raise HTTPException(
            status_code=400,
            detail=f"档案已处于终态{archive.status}，不能取消"
        )

    if archive.scheduled_release_id:
        sched = crud.get_scheduled_release(db, archive.scheduled_release_id)
        if sched and sched.status == "pending":
            crud.cancel_scheduled_release(db, archive.scheduled_release_id, reason, user.username)

    updated = crud.update_archive_status(
        db, archive_id, models.ARCHIVE_STATUS_CANCELLED, user.username,
        conflict_result=models.ARCHIVE_CONFLICT_NONE,
        conflict_detail=reason,
    )

    crud.write_audit_log(
        db,
        action="archive_cancel",
        operator=user.username,
        target_type="release_archive",
        target_id=str(archive_id),
        result="success",
        detail=f"取消档案成功，原因: {reason}",
    )

    return {
        "archive_id": archive_id,
        "status": updated.status if updated else "cancelled",
        "reason": reason,
    }


@app.get("/api/release-archives-stats")
def get_archive_stats(
    db: Session = Depends(get_db),
    user=Depends(auth.require_role(auth.ALLOW_ARCHIVE_AUDIT_ROLES)),
):
    stats = crud.get_archive_stats(db)
    return stats


@app.post("/api/release-archives/recover-on-restart", response_model=dict)
def recover_archives_after_restart(
    db: Session = Depends(get_db),
    user=Depends(auth.require_role(auth.ALLOW_ARCHIVE_AUDIT_ROLES)),
):
    crud.write_audit_log(
        db,
        action="archive_recover_restart",
        operator=user.username,
        target_type="release_archive",
        target_id="__all__",
        result="started",
        detail="触发服务重启后档案恢复流程",
    )
    result = crud.recover_archives_on_restart(db)
    crud.write_audit_log(
        db,
        action="archive_recover_restart",
        operator=user.username,
        target_type="release_archive",
        target_id="__all__",
        result="success",
        detail=f"档案恢复完成: {json.dumps(result, ensure_ascii=False)}",
    )
    db.commit()
    return result


@app.post("/api/release-archives/{archive_id}/execute", response_model=dict)
def manually_execute_archive(
    archive_id: int,
    db: Session = Depends(get_db),
    user=Depends(auth.require_role(auth.ALLOW_ARCHIVE_EXECUTE_ROLES)),
):
    archive = crud.get_archive_by_id(db, archive_id)
    if not archive:
        crud.write_audit_log(
            db,
            action="archive_manual_execute",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="not_found",
            detail=f"手动接管执行失败: 档案不存在",
        )
        raise HTTPException(status_code=404, detail="档案不存在")

    permission_ok, permission_msg = crud.check_archive_permission(
        db, archive_id, user.username, auth.ALLOW_ARCHIVE_EXECUTE_ROLES, user.role, "execute"
    )
    if not permission_ok:
        crud.write_audit_log(
            db,
            action="archive_manual_execute",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="forbidden",
            detail=f"手动接管执行被拒: {permission_msg}",
        )
        raise HTTPException(status_code=403, detail=permission_msg)

    terminal_statuses = {models.ARCHIVE_STATUS_EXECUTED, models.ARCHIVE_STATUS_CANCELLED,
                         models.ARCHIVE_STATUS_SUPERSEDED, models.ARCHIVE_STATUS_FAILED}
    if archive.status in terminal_statuses:
        crud.write_audit_log(
            db,
            action="archive_manual_execute",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="rejected",
            detail=f"手动接管执行被拒: 已处于终态{archive.status}",
        )
        raise HTTPException(
            status_code=400,
            detail=f"档案已处于终态{archive.status}，不能手动接管执行（仅pending/executing状态可执行）"
        )

    try:
        result = crud.manually_execute_archive(db, archive_id, user.username)
        return result
    except ValueError as e:
        crud.write_audit_log(
            db,
            action="archive_manual_execute",
            operator=user.username,
            target_type="release_archive",
            target_id=str(archive_id),
            result="rejected",
            detail=f"手动接管执行被拒: {str(e)}",
        )
        raise HTTPException(status_code=400, detail=str(e))
