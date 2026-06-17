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
    plan_status: Optional[str] = None


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


VALID_PLAN_STATUSES = {"queued", "scheduled", "executing", "executed", "expired", "superseded", "cancelled", "failed"}
VALID_PLAN_SOURCE_TYPES = {"manual_candidate", "scheduled", "manual_release", "rollback", "import_conflict"}
VALID_PLAN_TYPES = {"candidate", "release", "rollback"}


class ReleasePlanConfigBase(BaseModel):
    rule_id: Optional[int] = None
    config_key: str
    config_value: str
    description: Optional[str] = ""


class ReleasePlanConfigCreate(ReleasePlanConfigBase):
    pass


class ReleasePlanConfigUpdate(BaseModel):
    config_value: Optional[str] = None
    description: Optional[str] = None


class ReleasePlanConfigResponse(ReleasePlanConfigBase):
    id: int
    updated_by: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ReleasePlanEventResponse(BaseModel):
    id: int
    plan_id: int
    event_type: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    operator: str
    reason: str
    detail: Dict[str, Any]
    created_at: datetime

    class Config:
        from_attributes = True


class ReleasePlanBase(BaseModel):
    rule_id: int
    batch_id: Optional[int] = None
    candidate_id: Optional[int] = None
    scheduled_release_id: Optional[int] = None
    release_version_id: Optional[int] = None
    status: str
    source_type: str
    plan_type: str
    planned_time: Optional[datetime] = None
    conflict_reason: Optional[str] = ""
    source_detail: Optional[str] = ""


class ReleasePlanResponse(ReleasePlanBase):
    id: int
    executed_at: Optional[datetime] = None
    expired_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    superseded_at: Optional[datetime] = None
    superseded_by_plan_id: Optional[int] = None
    created_by: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ReleasePlanDetailResponse(ReleasePlanResponse):
    batch: Optional[SupplierBatchResponse] = None
    candidate: Optional[ReleaseCandidateResponse] = None
    scheduled_release: Optional[ScheduledReleaseResponse] = None
    release_version: Optional[ReleaseVersionResponse] = None
    events: List[ReleasePlanEventResponse] = []


class ReleasePlanListQuery(BaseModel):
    rule_id: Optional[int] = None
    status: Optional[str] = None
    source_type: Optional[str] = None
    plan_type: Optional[str] = None
    batch_id: Optional[int] = None
    skip: int = 0
    limit: int = 100


class ReleasePlanStatsResponse(BaseModel):
    rule_id: Optional[int] = None
    queued_count: int = 0
    scheduled_count: int = 0
    executing_count: int = 0
    executed_count: int = 0
    expired_count: int = 0
    superseded_count: int = 0
    cancelled_count: int = 0
    failed_count: int = 0
    total_count: int = 0


class ReleasePlanConflictInfo(BaseModel):
    has_conflict: bool
    conflict_type: Optional[str] = None
    conflict_plan_id: Optional[int] = None
    conflict_reason: Optional[str] = None
    suggestion: Optional[str] = None


class PlanConfigValidateRequest(BaseModel):
    config_key: str
    config_value: str


class PlanConfigValidateResponse(BaseModel):
    valid: bool
    config_key: str
    config_value: str
    error_message: Optional[str] = None
    normalized_value: Optional[str] = None


VALID_ARCHIVE_STATUSES = {"pending", "executing", "executed", "cancelled", "superseded", "failed"}
VALID_ARCHIVE_CONFLICTS = {"none", "import_conflict", "manual_release_conflict", "rollback_conflict", "candidate_conflict"}
VALID_ARCHIVE_EXEC_STRATEGIES = {"auto", "manual", "force"}


class ProcessingLogEntry(BaseModel):
    timestamp: str
    event: str
    operator: str
    detail: Optional[str] = ""


class ReleaseArchiveSnapshot(BaseModel):
    release_note: str
    approval_remark: str
    triggered_by: str
    source_batch_id: int
    target_version: Optional[str] = None
    execution_strategy: str = "auto"
    scheduled_release_id: int


class ReleaseArchiveBase(BaseModel):
    scheduled_release_id: int
    release_plan_id: Optional[int] = None
    release_version_id: Optional[int] = None
    release_note: str = ""
    approval_remark: str = ""
    triggered_by: str
    source_batch_id: int
    target_version: Optional[str] = None
    execution_strategy: str = "auto"
    status: str = "pending"
    conflict_result: str = "none"
    conflict_detail: Optional[str] = ""


class ReleaseArchiveResponse(ReleaseArchiveBase):
    id: int
    snapshot_hash: str
    is_immutable: bool
    created_at: datetime
    archived_at: datetime
    last_processed_at: Optional[datetime] = None
    recovered_after_restart: bool
    processing_log: List[ProcessingLogEntry] = []
    reference_count: int

    class Config:
        from_attributes = True


class ReleaseArchiveReferenceResponse(BaseModel):
    id: int
    archive_id: int
    reference_type: str
    reference_id: str
    operation: str
    operator: str
    detail: Optional[str] = ""
    created_at: datetime

    class Config:
        from_attributes = True


class ReleaseArchiveDetailResponse(ReleaseArchiveResponse):
    scheduled_release: Optional[ScheduledReleaseResponse] = None
    release_plan: Optional[ReleasePlanResponse] = None
    release_version: Optional[ReleaseVersionResponse] = None
    references: List[ReleaseArchiveReferenceResponse] = []


class ReleaseArchiveListQuery(BaseModel):
    scheduled_release_id: Optional[int] = None
    release_plan_id: Optional[int] = None
    release_version_id: Optional[int] = None
    source_batch_id: Optional[int] = None
    status: Optional[str] = None
    conflict_result: Optional[str] = None
    triggered_by: Optional[str] = None
    skip: int = 0
    limit: int = 100


class ReleaseArchiveExportItem(BaseModel):
    field: str
    value: str
    is_snapshot: bool
    description: str


class ReleaseArchiveExportResponse(BaseModel):
    archive_id: int
    snapshot_hash: str
    export_time: datetime
    exported_by: str
    items: List[ReleaseArchiveExportItem]
    scheduled_release: Optional[ScheduledReleaseResponse] = None
    release_version: Optional[ReleaseVersionResponse] = None


class ReleaseArchiveVerifyRequest(BaseModel):
    archive_id: int
    expected_fields: Dict[str, Any]


class ReleaseArchiveVerifyResponse(BaseModel):
    archive_id: int
    verified: bool
    matched_fields: List[str]
    mismatched_fields: List[str]
    details: Dict[str, Any]


try:
    ReleaseArchiveDetailResponse.model_rebuild()
except Exception:
    pass
