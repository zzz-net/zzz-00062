from sqlalchemy.orm import Session
from . import models, schemas
from .scoring import calculate_score
from datetime import datetime
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

    db.commit()
    db.refresh(db_batch)
    return db_batch


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


def approve_and_release(db: Session, batch_id: int, approve_data: schemas.ApproveRequest):
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
    db.commit()
    db.refresh(db_release)

    return db_release


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
    return active_release, scores


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


def list_audit_logs(db: Session, action: str = None, operator: str = None, target_type: str = None, skip: int = 0, limit: int = 100):
    q = db.query(models.AuditLog)
    if action:
        q = q.filter(models.AuditLog.action == action)
    if operator:
        q = q.filter(models.AuditLog.operator == operator)
    if target_type:
        q = q.filter(models.AuditLog.target_type == target_type)
    return q.order_by(models.AuditLog.created_at.desc()).offset(skip).limit(limit).all()
