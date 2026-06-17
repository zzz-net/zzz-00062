from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


class ScoringRule(Base):
    __tablename__ = "scoring_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    weight_config = Column(JSON, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SupplierBatch(Base):
    __tablename__ = "supplier_batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_name = Column(String(200), nullable=False)
    rule_id = Column(Integer, ForeignKey("scoring_rules.id"))
    status = Column(String(50), default="imported")
    imported_by = Column(String(100), nullable=False)
    imported_at = Column(DateTime, default=datetime.utcnow)
    supplier_count = Column(Integer, default=0)
    remark = Column(Text, default="")

    rule = relationship("ScoringRule")
    suppliers = relationship("Supplier", back_populates="batch", cascade="all, delete-orphan")
    draft_scores = relationship("DraftScore", back_populates="batch", cascade="all, delete-orphan")


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("supplier_batches.id"), nullable=False)
    supplier_code = Column(String(100), nullable=False, index=True)
    supplier_name = Column(String(200), nullable=False)
    metrics = Column(JSON, nullable=False)

    batch = relationship("SupplierBatch", back_populates="suppliers")


class DraftScore(Base):
    __tablename__ = "draft_scores"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("supplier_batches.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    supplier_code = Column(String(100), nullable=False)
    supplier_name = Column(String(200), nullable=False)
    total_score = Column(Float, default=0.0)
    score_details = Column(JSON, nullable=False)
    grade = Column(String(20), default="")
    calculated_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("SupplierBatch", back_populates="draft_scores")
    supplier = relationship("Supplier")


class ReleaseVersion(Base):
    __tablename__ = "release_versions"

    id = Column(Integer, primary_key=True, index=True)
    version = Column(String(50), nullable=False, unique=True)
    batch_id = Column(Integer, ForeignKey("supplier_batches.id"), nullable=False)
    rule_id = Column(Integer, ForeignKey("scoring_rules.id"), nullable=False)
    is_active = Column(Boolean, default=False)
    release_note = Column(Text, default="")
    approval_remark = Column(Text, default="")
    approved_by = Column(String(100), nullable=False)
    released_at = Column(DateTime, default=datetime.utcnow)
    supplier_count = Column(Integer, default=0)

    batch = relationship("SupplierBatch")
    rule = relationship("ScoringRule")
    released_scores = relationship("ReleasedScore", back_populates="release", cascade="all, delete-orphan")


class ReleasedScore(Base):
    __tablename__ = "released_scores"

    id = Column(Integer, primary_key=True, index=True)
    release_id = Column(Integer, ForeignKey("release_versions.id"), nullable=False)
    supplier_code = Column(String(100), nullable=False, index=True)
    supplier_name = Column(String(200), nullable=False)
    total_score = Column(Float, default=0.0)
    score_details = Column(JSON, nullable=False)
    grade = Column(String(20), default="")

    release = relationship("ReleaseVersion", back_populates="released_scores")


class RollbackRecord(Base):
    __tablename__ = "rollback_records"

    id = Column(Integer, primary_key=True, index=True)
    from_version = Column(String(50), nullable=False)
    to_version = Column(String(50), nullable=False)
    reason = Column(Text, default="")
    operated_by = Column(String(100), nullable=False)
    operated_at = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    action = Column(String(50), nullable=False, index=True)
    operator = Column(String(100), nullable=False)
    target_type = Column(String(50), nullable=False)
    target_id = Column(String(100), nullable=False)
    result = Column(String(20), nullable=False)
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ReleaseCandidate(Base):
    __tablename__ = "release_candidates"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("supplier_batches.id"), nullable=False)
    rule_id = Column(Integer, ForeignKey("scoring_rules.id"), nullable=False)
    change_description = Column(Text, default="")
    expected_effective_time = Column(DateTime, nullable=True)
    operation_remark = Column(Text, default="")
    set_by = Column(String(100), nullable=False)
    set_at = Column(DateTime, default=datetime.utcnow)
    is_current = Column(Boolean, default=True)

    batch = relationship("SupplierBatch")
    rule = relationship("ScoringRule")


class CandidateChangeLog(Base):
    __tablename__ = "candidate_change_logs"

    id = Column(Integer, primary_key=True, index=True)
    old_candidate_id = Column(Integer, nullable=True)
    new_candidate_id = Column(Integer, nullable=True)
    change_reason = Column(Text, default="")
    operated_by = Column(String(100), nullable=False)
    operated_at = Column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, nullable=False)
    role = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
