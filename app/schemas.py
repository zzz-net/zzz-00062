from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime


class ScoringRuleBase(BaseModel):
    name: str
    description: Optional[str] = ""
    weight_config: Dict[str, Any]
    is_active: Optional[bool] = True


class ScoringRuleCreate(ScoringRuleBase):
    pass


class ScoringRuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    weight_config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class ScoringRuleResponse(ScoringRuleBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SupplierImportItem(BaseModel):
    supplier_code: str
    supplier_name: str
    metrics: Dict[str, Any]


class BatchImportRequest(BaseModel):
    batch_name: str
    rule_id: int
    imported_by: str
    suppliers: List[SupplierImportItem]
    remark: Optional[str] = ""


class SupplierResponse(BaseModel):
    id: int
    supplier_code: str
    supplier_name: str
    metrics: Dict[str, Any]

    class Config:
        from_attributes = True


class SupplierBatchResponse(BaseModel):
    id: int
    batch_name: str
    rule_id: int
    status: str
    imported_by: str
    imported_at: datetime
    supplier_count: int
    remark: str

    class Config:
        from_attributes = True


class DraftScoreResponse(BaseModel):
    id: int
    batch_id: int
    supplier_code: str
    supplier_name: str
    total_score: float
    score_details: Dict[str, Any]
    grade: str
    calculated_at: datetime

    class Config:
        from_attributes = True


class CalculateRequest(BaseModel):
    calculated_by: str


class ApproveRequest(BaseModel):
    approved_by: str
    approval_remark: Optional[str] = ""
    release_note: Optional[str] = ""


class ReleaseVersionResponse(BaseModel):
    id: int
    version: str
    batch_id: int
    rule_id: int
    is_active: bool
    release_note: str
    approval_remark: str
    approved_by: str
    released_at: datetime
    supplier_count: int
    release_source: str = "manual"

    class Config:
        from_attributes = True


class ReleasedScoreResponse(BaseModel):
    id: int
    release_id: int
    supplier_code: str
    supplier_name: str
    total_score: float
    score_details: Dict[str, Any]
    grade: str

    class Config:
        from_attributes = True


class RollbackRequest(BaseModel):
    target_version: str
    reason: str
    operated_by: str


class RollbackRecordResponse(BaseModel):
    id: int
    from_version: str
    to_version: str
    reason: str
    operated_by: str
    operated_at: datetime

    class Config:
        from_attributes = True


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    created_at: datetime

    class Config:
        from_attributes = True


class AuditLogResponse(BaseModel):
    id: int
    action: str
    operator: str
    target_type: str
    target_id: str
    result: str
    detail: str
    created_at: datetime

    class Config:
        from_attributes = True


class ExportResultItem(BaseModel):
    supplier_code: str
    supplier_name: str
    total_score: float
    grade: str
    score_details: Dict[str, Any]


class SetCandidateRequest(BaseModel):
    batch_id: int
    change_description: str
    expected_effective_time: Optional[datetime] = None
    operation_remark: Optional[str] = ""
    set_by: str


class ReleaseCandidateResponse(BaseModel):
    id: int
    batch_id: int
    rule_id: int
    change_description: str
    expected_effective_time: Optional[datetime] = None
    operation_remark: str
    set_by: str
    set_at: datetime
    is_current: bool

    class Config:
        from_attributes = True


class CandidateChangeLogResponse(BaseModel):
    id: int
    old_candidate_id: Optional[int] = None
    new_candidate_id: Optional[int] = None
    change_reason: str
    operated_by: str
    operated_at: datetime

    class Config:
        from_attributes = True


class ExportResponse(BaseModel):
    version: str
    released_at: datetime
    approved_by: str
    release_note: str
    supplier_count: int
    scores: List[ExportResultItem]
    candidate_batch_id: Optional[int] = None
    candidate_matches_active: Optional[bool] = None
    release_source: str = "manual"


class ScheduleReleaseRequest(BaseModel):
    batch_id: int
    scheduled_time: datetime
    change_description: str = ""
    operation_remark: Optional[str] = ""
    release_note: Optional[str] = ""
    approval_remark: Optional[str] = ""
    set_by: str


class ScheduledReleaseResponse(BaseModel):
    id: int
    candidate_id: int
    batch_id: int
    rule_id: int
    scheduled_time: datetime
    status: str
    cancel_reason: str
    release_version_id: Optional[int] = None
    created_by: str
    created_at: datetime
    executed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancelled_by: Optional[str] = None

    class Config:
        from_attributes = True


class ScheduledReleaseDetailResponse(ScheduledReleaseResponse):
    candidate: Optional[ReleaseCandidateResponse] = None
    release_version: Optional[ReleaseVersionResponse] = None
