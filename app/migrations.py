from sqlalchemy import text, inspect, Column, Integer, String, DateTime, Text, ForeignKey, JSON, Boolean, Index
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import logging
from .database import engine, Base
from . import models

logger = logging.getLogger("release_plan_migrations")

MIGRATION_VERSION_TABLE = "_schema_migrations"
CURRENT_SCHEMA_VERSION = 3


def ensure_migration_table(db: Session):
    inspector = inspect(engine)
    if MIGRATION_VERSION_TABLE not in inspector.get_table_names():
        db.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {MIGRATION_VERSION_TABLE} (
                version INTEGER PRIMARY KEY,
                applied_at DATETIME NOT NULL,
                description TEXT DEFAULT ''
            )
        """))
        db.commit()


def get_current_version(db: Session) -> int:
    ensure_migration_table(db)
    try:
        result = db.execute(text(f"SELECT MAX(version) FROM {MIGRATION_VERSION_TABLE}"))
        row = result.fetchone()
        return row[0] if row and row[0] is not None else 0
    except Exception:
        return 0


def record_migration(db: Session, version: int, description: str = ""):
    db.execute(
        text(f"INSERT INTO {MIGRATION_VERSION_TABLE} (version, applied_at, description) VALUES (:v, :t, :d)"),
        {"v": version, "t": datetime.utcnow(), "d": description}
    )
    db.commit()


def column_exists(db: Session, table_name: str, column_name: str) -> bool:
    inspector = inspect(engine)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def table_exists(db: Session, table_name: str) -> bool:
    inspector = inspect(engine)
    return table_name in inspector.get_table_names()


def migrate_v1_to_v2(db: Session):
    logger.info("Starting migration v1 -> v2: Adding release_plans and release_plan_configs")

    if not table_exists(db, "release_plan_configs"):
        db.execute(text("""
            CREATE TABLE release_plan_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER,
                config_key VARCHAR(100) NOT NULL,
                config_value TEXT NOT NULL,
                description TEXT DEFAULT '',
                updated_by VARCHAR(100) DEFAULT '__system__',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        db.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_config_rule_key
            ON release_plan_configs(rule_id, config_key)
        """))
        logger.info("Created release_plan_configs table")

    if not table_exists(db, "release_plans"):
        db.execute(text("""
            CREATE TABLE release_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL,
                batch_id INTEGER,
                candidate_id INTEGER,
                scheduled_release_id INTEGER,
                release_version_id INTEGER,
                status VARCHAR(30) NOT NULL,
                source_type VARCHAR(30) NOT NULL,
                plan_type VARCHAR(30) NOT NULL,
                planned_time DATETIME,
                executed_at DATETIME,
                expired_at DATETIME,
                cancelled_at DATETIME,
                superseded_at DATETIME,
                superseded_by_plan_id INTEGER,
                conflict_reason TEXT DEFAULT '',
                source_detail TEXT DEFAULT '',
                created_by VARCHAR(100) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plan_rule_status ON release_plans(rule_id, status)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plan_status ON release_plans(status)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plan_batch ON release_plans(batch_id)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plan_candidate ON release_plans(candidate_id)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plan_sched ON release_plans(scheduled_release_id)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plan_version ON release_plans(release_version_id)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plan_created ON release_plans(created_at DESC)"))
        logger.info("Created release_plans table")

    if not table_exists(db, "release_plan_events"):
        db.execute(text("""
            CREATE TABLE release_plan_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                event_type VARCHAR(50) NOT NULL,
                from_status VARCHAR(30),
                to_status VARCHAR(30),
                operator VARCHAR(100) NOT NULL,
                reason TEXT DEFAULT '',
                detail JSON DEFAULT '{}',
                created_at DATETIME NOT NULL
            )
        """))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plan_event_plan ON release_plan_events(plan_id)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plan_event_type ON release_plan_events(event_type)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_plan_event_created ON release_plan_events(created_at DESC)"))
        logger.info("Created release_plan_events table")

    db.commit()
    backfill_release_plans(db)
    backfill_default_configs(db)
    record_migration(db, 2, "Add release_plans, release_plan_configs, release_plan_events tables with backfill")
    logger.info("Migration v1->v2 completed successfully")


def backfill_release_plans(db: Session):
    logger.info("Backfilling release_plans from existing data...")
    now = datetime.utcnow()
    plan_id_counter = 1

    bad_rows = db.execute(text("""
        SELECT COUNT(*) FROM release_plans
        WHERE created_at IS NOT NULL AND typeof(created_at) = 'text'
          AND substr(created_at, 1, 1) NOT IN ('0','1','2','3','4','5','6','7','8','9')
    """)).fetchone()[0]
    if bad_rows > 0:
        logger.warning(f"Found {bad_rows} malformed plan records. Truncating plan tables and re-backfilling.")
        db.execute(text("DELETE FROM release_plan_events"))
        db.execute(text("DELETE FROM release_plans"))
        db.commit()

    existing_plans = db.execute(text("SELECT COUNT(*) FROM release_plans")).fetchone()[0]
    if existing_plans > 0:
        logger.info(f"release_plans already has {existing_plans} records, skipping backfill")
        return

    if not column_exists(db, "release_versions", "release_source"):
        try:
            db.execute(text("ALTER TABLE release_versions ADD COLUMN release_source VARCHAR(20) DEFAULT 'manual'"))
            db.commit()
            logger.info("Added missing release_source column to release_versions")
        except Exception:
            db.rollback()

    rv_select_cols = ["rv.id", "rv.version", "rv.batch_id", "rv.rule_id", "rv.is_active",
                      "rv.released_at", "rv.approved_by"]
    if column_exists(db, "release_versions", "release_source"):
        rv_select_cols.append("rv.release_source")
    else:
        rv_select_cols.append("'manual' as release_source")

    release_versions = db.execute(text(f"""
        SELECT {', '.join(rv_select_cols)}
        FROM release_versions rv
        ORDER BY rv.released_at ASC
    """)).fetchall()

    version_plan_map = {}
    for rv in release_versions:
        rv_id, rv_version, rv_batch, rv_rule, rv_active, rv_released_at, rv_approved_by, rv_source = (
            rv[0], rv[1], rv[2], rv[3], rv[4], rv[5], rv[6], rv[7]
        )
        plan_id = plan_id_counter
        plan_id_counter += 1
        source_type = "scheduled" if rv_source == "scheduled" else "manual_release"
        created_by = rv_approved_by if isinstance(rv_approved_by, str) and rv_approved_by else "__system__"
        released_time = rv_released_at or now
        db.execute(text("""
            INSERT INTO release_plans (
                id, rule_id, batch_id, candidate_id, scheduled_release_id,
                release_version_id, status, source_type, plan_type,
                planned_time, executed_at, created_by, created_at, updated_at
            ) VALUES (
                :id, :rule_id, :batch_id, NULL, NULL,
                :version_id, 'executed', :source_type, 'release',
                :planned, :executed, :created_by, :created, :updated
            )
        """), {
            "id": plan_id,
            "rule_id": rv_rule,
            "batch_id": rv_batch,
            "version_id": rv_id,
            "source_type": source_type,
            "planned": released_time,
            "executed": released_time,
            "created_by": created_by,
            "created": released_time,
            "updated": released_time,
        })
        version_plan_map[rv_id] = plan_id
        active_str = "true" if rv_active else "false"
        db.execute(text("""
            INSERT INTO release_plan_events (
                plan_id, event_type, from_status, to_status, operator, reason, detail, created_at
            ) VALUES (:pid, 'created', NULL, 'executed', :op, '历史数据迁移回填', :detail, :t)
        """), {
            "pid": plan_id,
            "op": created_by,
            "detail": '{"version":"' + (rv_version or "") + '","is_active":' + active_str + '}',
            "t": released_time,
        })
    logger.info(f"Backfilled {len(release_versions)} executed release plans")

    sched_releases = db.execute(text("""
        SELECT sr.id, sr.candidate_id, sr.batch_id, sr.rule_id, sr.scheduled_time,
               sr.status, sr.cancel_reason, sr.release_version_id, sr.created_by,
               sr.created_at, sr.executed_at, sr.cancelled_at
        FROM scheduled_releases sr
        ORDER BY sr.created_at ASC
    """)).fetchall()

    sched_candidate_ids = set()
    for sr in sched_releases:
        sched_candidate_ids.add(sr[1])
        plan_id = plan_id_counter
        plan_id_counter += 1

        status_map = {
            "pending": "scheduled",
            "executed": "executed",
            "cancelled": "cancelled",
        }
        plan_status = status_map.get(sr[5], sr[5])

        superseded_at = None
        if sr[5] == "cancelled" and sr[6] and ("顶替" in str(sr[6]) or "导入" in str(sr[6]) or "发布" in str(sr[6])):
            plan_status = "superseded"
            superseded_at = sr[11] or sr[10] or now

        mapped_version_id = version_plan_map.get(sr[7]) if sr[7] else None

        db.execute(text("""
            INSERT INTO release_plans (
                id, rule_id, batch_id, candidate_id, scheduled_release_id,
                release_version_id, status, source_type, plan_type,
                planned_time, executed_at, cancelled_at, superseded_at,
                conflict_reason, created_by, created_at, updated_at
            ) VALUES (
                :id, :rule_id, :batch_id, :candidate_id, :sched_id,
                :version_id, :status, 'scheduled', 'release',
                :planned, :executed, :cancelled, :superseded,
                :conflict, :created_by, :created, :updated
            )
        """), {
            "id": plan_id,
            "rule_id": sr[3],
            "batch_id": sr[2],
            "candidate_id": sr[1],
            "sched_id": sr[0],
            "version_id": sr[7],
            "status": plan_status,
            "planned": sr[4],
            "executed": sr[10],
            "cancelled": sr[11] if plan_status == "cancelled" else None,
            "superseded": superseded_at,
            "conflict": sr[6] or "",
            "created_by": sr[8] or "__system__",
            "created": sr[9] or now,
            "updated": sr[9] or now,
        })
        db.execute(text("""
            INSERT INTO release_plan_events (
                plan_id, event_type, from_status, to_status, operator, reason, detail, created_at
            ) VALUES (:pid, 'created', NULL, :status, :op, '历史数据迁移回填(预约发布)', :detail, :t)
        """), {
            "pid": plan_id,
            "status": plan_status,
            "op": sr[8] or "__system__",
            "detail": '{"scheduled_release_id":' + str(sr[0]) + '}',
            "t": sr[9] or now,
        })
    logger.info(f"Backfilled {len(sched_releases)} scheduled release plans")

    candidates = db.execute(text("""
        SELECT rc.id, rc.batch_id, rc.rule_id, rc.expected_effective_time,
               rc.set_by, rc.set_at, rc.is_current
        FROM release_candidates rc
        ORDER BY rc.set_at ASC
    """)).fetchall()

    for rc in candidates:
        if rc[0] in sched_candidate_ids:
            continue

        plan_id = plan_id_counter
        plan_id_counter += 1

        if rc[6]:
            plan_status = "queued"
        else:
            plan_status = "expired"

        db.execute(text("""
            INSERT INTO release_plans (
                id, rule_id, batch_id, candidate_id, scheduled_release_id,
                release_version_id, status, source_type, plan_type,
                planned_time, expired_at, created_by, created_at, updated_at
            ) VALUES (
                :id, :rule_id, :batch_id, :candidate_id, NULL,
                NULL, :status, 'manual_candidate', 'candidate',
                :planned, :expired, :created_by, :created, :updated
            )
        """), {
            "id": plan_id,
            "rule_id": rc[2],
            "batch_id": rc[1],
            "candidate_id": rc[0],
            "status": plan_status,
            "planned": rc[3],
            "expired": now if not rc[6] else None,
            "created_by": rc[4] or "__system__",
            "created": rc[5] or now,
            "updated": rc[5] or now,
        })
        db.execute(text("""
            INSERT INTO release_plan_events (
                plan_id, event_type, from_status, to_status, operator, reason, detail, created_at
            ) VALUES (:pid, 'created', NULL, :status, :op, '历史数据迁移回填(手动候选)', :detail, :t)
        """), {
            "pid": plan_id,
            "status": plan_status,
            "op": rc[4] or "__system__",
            "detail": '{"candidate_id":' + str(rc[0]) + ',"is_current":' + ("true" if rc[6] else "false") + '}',
            "t": rc[5] or now,
        })
    logger.info(f"Backfilled {len(candidates)} manual candidate plans")

    rollbacks = db.execute(text("""
        SELECT rr.id, rr.from_version, rr.to_version, rr.reason,
               rr.operated_by, rr.operated_at
        FROM rollback_records rr
        ORDER BY rr.operated_at ASC
    """)).fetchall()

    version_to_id = {rv[1]: rv[0] for rv in release_versions}
    version_to_rule = {rv[1]: rv[3] for rv in release_versions}
    version_to_batch = {rv[1]: rv[2] for rv in release_versions}

    for rr in rollbacks:
        plan_id = plan_id_counter
        plan_id_counter += 1
        target_rule = version_to_rule.get(rr[2])
        target_batch = version_to_batch.get(rr[2])
        target_ver_id = version_to_id.get(rr[2])

        db.execute(text("""
            INSERT INTO release_plans (
                id, rule_id, batch_id, candidate_id, scheduled_release_id,
                release_version_id, status, source_type, plan_type,
                executed_at, conflict_reason, created_by, created_at, updated_at
            ) VALUES (
                :id, :rule_id, :batch_id, NULL, NULL,
                :version_id, 'executed', 'rollback', 'rollback',
                :executed, :reason, :created_by, :created, :updated
            )
        """), {
            "id": plan_id,
            "rule_id": target_rule or 0,
            "batch_id": target_batch,
            "version_id": target_ver_id,
            "executed": rr[5] or now,
            "reason": f"从{rr[1]}回滚到{rr[2]}: {rr[3] or ''}",
            "created_by": rr[4] or "__system__",
            "created": rr[5] or now,
            "updated": rr[5] or now,
        })
        db.execute(text("""
            INSERT INTO release_plan_events (
                plan_id, event_type, from_status, to_status, operator, reason, detail, created_at
            ) VALUES (:pid, 'created', NULL, 'executed', :op, :reason, :detail, :t)
        """), {
            "pid": plan_id,
            "op": rr[4] or "__system__",
            "reason": rr[3] or "历史数据迁移回填(回滚)",
            "detail": '{"from_version":"' + (rr[1] or "") + '","to_version":"' + (rr[2] or "") + '"}',
            "t": rr[5] or now,
        })
    logger.info(f"Backfilled {len(rollbacks)} rollback plans")

    db.commit()
    logger.info("Backfill completed")


def backfill_default_configs(db: Session):
    defaults = [
        (None, "allow_early_window_seconds", "120", "允许提前执行的时间窗口（秒），默认2分钟"),
        (None, "allow_late_window_seconds", "86400", "允许延后执行的时间窗口（秒），默认24小时，超出后自动失效"),
        (None, "default_expire_hours", "72", "排队候选的默认过期时间（小时），默认3天后失效"),
        (None, "max_queued_per_rule", "5", "同一规则最多排队的计划数量，超出自动顶掉最早的"),
    ]
    for rule_id, key, value, desc in defaults:
        existing = db.execute(text("""
            SELECT id FROM release_plan_configs
            WHERE (rule_id IS NULL AND :rid IS NULL OR rule_id = :rid2) AND config_key = :k
        """), {"rid": rule_id, "rid2": rule_id, "k": key}).fetchone()
        if not existing:
            db.execute(text("""
                INSERT INTO release_plan_configs (
                    rule_id, config_key, config_value, description,
                    updated_by, created_at, updated_at
                ) VALUES (:rid, :k, :v, :d, '__system__', :t, :t)
            """), {"rid": rule_id, "k": key, "v": value, "d": desc, "t": datetime.utcnow()})
    db.commit()
    logger.info("Default release plan configs ensured")


def migrate_v2_to_v3(db: Session):
    logger.info("Starting migration v2 -> v3: Update early window default, repair contradictory plan data")

    db.execute(text("""
        UPDATE release_plan_configs
        SET config_value = '120', description = '允许提前执行的时间窗口（秒），默认2分钟'
        WHERE config_key = 'allow_early_window_seconds'
          AND config_value = '300'
          AND rule_id IS NULL
          AND (description LIKE '%5分钟%' OR description LIKE '%300%')
    """))
    db.commit()
    logger.info("Updated allow_early_window_seconds default from 300 to 120")

    if table_exists(db, "release_plans"):
        repaired = 0

        db.execute(text("""
            UPDATE release_plans SET cancelled_at = NULL
            WHERE status = 'executed' AND cancelled_at IS NOT NULL
        """))
        repaired += db.execute(text("SELECT changes()")).fetchone()[0]

        db.execute(text("""
            UPDATE release_plans SET executed_at = NULL
            WHERE status = 'cancelled' AND executed_at IS NOT NULL
        """))
        repaired += db.execute(text("SELECT changes()")).fetchone()[0]

        db.execute(text("""
            UPDATE release_plans SET executed_at = NULL
            WHERE status = 'superseded' AND executed_at IS NOT NULL
        """))
        repaired += db.execute(text("SELECT changes()")).fetchone()[0]

        db.execute(text("""
            UPDATE release_plans SET executed_at = NULL
            WHERE status = 'expired' AND executed_at IS NOT NULL
        """))
        repaired += db.execute(text("SELECT changes()")).fetchone()[0]

        db.execute(text("""
            UPDATE release_plans SET cancelled_at = NULL
            WHERE status = 'superseded' AND cancelled_at IS NOT NULL
        """))
        repaired += db.execute(text("SELECT changes()")).fetchone()[0]

        db.execute(text("""
            UPDATE release_plans SET cancelled_at = NULL
            WHERE status = 'expired' AND cancelled_at IS NOT NULL
        """))
        repaired += db.execute(text("SELECT changes()")).fetchone()[0]

        db.commit()
        logger.info(f"Repaired {repaired} contradictory plan records")

    record_migration(db, 3, "Update early window default to 120s, repair contradictory plan timestamps")
    logger.info("Migration v2->v3 completed successfully")


def run_migrations(db: Session):
    current = get_current_version(db)
    logger.info(f"Current schema version: {current}, target: {CURRENT_SCHEMA_VERSION}")

    if current < 1:
        record_migration(db, 1, "Initial baseline - existing tables")
        current = 1

    if current < 2:
        migrate_v1_to_v2(db)
        current = 2

    if current < 3:
        migrate_v2_to_v3(db)
        current = 3

    try:
        repair_bad_plan_data(db)
    except Exception as e:
        logger.warning(f"repair_bad_plan_data skipped: {e}")

    logger.info(f"Migrations complete. Schema version: {get_current_version(db)}")


def repair_bad_plan_data(db: Session):
    if not table_exists(db, "release_plans"):
        return
    bad_rows = db.execute(text("""
        SELECT COUNT(*) FROM release_plans
        WHERE created_at IS NOT NULL AND typeof(created_at) = 'text'
          AND substr(created_at, 1, 1) NOT IN ('0','1','2','3','4','5','6','7','8','9')
    """)).fetchone()[0]
    if bad_rows > 0:
        logger.warning(f"Found {bad_rows} malformed plan records. Truncating plan tables and re-backfilling.")
        db.execute(text("DELETE FROM release_plan_events"))
        db.execute(text("DELETE FROM release_plans"))
        db.commit()
        backfill_release_plans(db)
        db.commit()
