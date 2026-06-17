from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from .database import engine, Base, get_db
from . import models, schemas, crud, auth

Base.metadata.create_all(bind=engine)

app = FastAPI(title="供应商评分重算服务", version="1.0.0")


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
        return crud.import_batch(db, batch_data)
    except ValueError as e:
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
    _=Depends(auth.require_role([auth.ROLE_ADMIN, auth.ROLE_APPROVER]))
):
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    try:
        return crud.calculate_draft_scores(db, batch_id)
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
        return crud.approve_and_release(db, batch_id, approve_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
        return {
            "active_release": schemas.ReleaseVersionResponse.model_validate(release).model_dump(),
            "rollback_record": schemas.RollbackRecordResponse.model_validate(record).model_dump()
        }
    except ValueError as e:
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

    release, scores = result
    score_items = [
        schemas.ExportResultItem(
            supplier_code=s.supplier_code,
            supplier_name=s.supplier_name,
            total_score=s.total_score,
            grade=s.grade,
            score_details=s.score_details,
        ) for s in scores
    ]

    return schemas.ExportResponse(
        version=release.version,
        released_at=release.released_at,
        approved_by=release.approved_by,
        release_note=release.release_note,
        supplier_count=release.supplier_count,
        scores=score_items,
    )


@app.get("/api/users", response_model=List[schemas.UserResponse])
def list_users(
    db: Session = Depends(get_db),
    _=Depends(auth.require_role([auth.ROLE_ADMIN]))
):
    return crud.list_users(db)
