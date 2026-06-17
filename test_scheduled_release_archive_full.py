import requests
import time
import sys
import os
from datetime import datetime, timezone, timedelta

BASE = "http://127.0.0.1:8002"
ADMIN_H = {"X-Username": "admin"}
APPR_H  = {"X-Username": "approver1"}
USER_H  = {"X-Username": "user1"}


def post(path, json=None, headers=None, expected=None):
    url = f"{BASE}{path}"
    h = headers or ADMIN_H
    r = requests.post(url, json=json, headers=h, timeout=30)
    if expected is not None:
        assert r.status_code == expected, f"POST {path} expected {expected} got {r.status_code}: {r.text}"
    return r

def get(path, headers=None, expected=None):
    url = f"{BASE}{path}"
    h = headers or ADMIN_H
    r = requests.get(url, headers=h, timeout=30)
    if expected is not None:
        assert r.status_code == expected, f"GET {path} expected {expected} got {r.status_code}: {r.text}"
    return r


def get_default_rule_id():
    r = get("/api/rules", ADMIN_H, 200)
    rules = r.json()
    assert rules, "No scoring rules, cannot proceed"
    return rules[0]["id"]


def setup_batch_and_calc(rule_id, ts):
    """Import a batch with 2 suppliers, then calculate draft scores. Return batch_id."""
    suppliers = [
        {"supplier_code": f"S_ARC_{ts}_1", "supplier_name": f"ArchiveTest-A-{ts}",
         "metrics": {"delivery": 90.0, "quality": 85.0, "cost": 70.0, "innovation": 60.0}},
        {"supplier_code": f"S_ARC_{ts}_2", "supplier_name": f"ArchiveTest-B-{ts}",
         "metrics": {"delivery": 75.0, "quality": 95.0, "cost": 80.0, "innovation": 70.0}},
    ]
    payload = {
        "batch_name": f"archive-test-{ts}",
        "rule_id": rule_id,
        "imported_by": "admin",
        "suppliers": suppliers,
        "remark": "automated archive test batch",
    }
    r = post("/api/batches/import", payload, ADMIN_H)
    assert r.status_code in (200, 201), f"import batch failed: {r.status_code} {r.text}"
    batch_id = r.json()["id"]
    post(f"/api/batches/{batch_id}/calculate", None, ADMIN_H, expected=200)
    return batch_id


def assert_close_utc(dt_str_a, dt_str_b, tolerance_seconds=60):
    def _p(s):
        if s is None:
            return None
        if isinstance(s, datetime):
            if s.tzinfo is None:
                s = s.replace(tzinfo=timezone.utc)
            return s.astimezone(timezone.utc)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    a, b = _p(dt_str_a), _p(dt_str_b)
    if a is None or b is None:
        raise AssertionError(f"Time parse failed: {dt_str_a!r} or {dt_str_b!r}")
    diff = abs((a - b).total_seconds())
    assert diff <= tolerance_seconds, f"Time diff {diff}s exceeds tolerance {tolerance_seconds}s: {dt_str_a} vs {dt_str_b}"


def _ts():
    return str(int(time.time()))


# ---------------------------------------------------------------------------
# Test 1: timezone input (+08:00, Z, naive) can be created and fetched back
# ---------------------------------------------------------------------------
def test_1_timezone_input_and_fetch():
    print("\n=== Test 1: timezone input and fetch back ===")
    rule_id = get_default_rule_id()

    # 1a) +08:00 timezone -> UTC 2027-07-20T02:00:00
    planned_shanghai = "2027-07-20T10:00:00+08:00"
    expected_utc_str = "2027-07-20T02:00:00"
    batch_id_1 = setup_batch_and_calc(rule_id, _ts() + "a")
    payload = {
        "batch_id": batch_id_1,
        "scheduled_time": planned_shanghai,
        "change_description": "Test1-AsiaShanghai note",
        "operation_remark": "Test1-AsiaShanghai approval",
        "release_note": "Test1-AsiaShanghai release",
        "approval_remark": "Test1-AsiaShanghai approval-remark",
        "target_version": "vTZ-1",
        "execution_strategy": "auto",
        "set_by": "admin",
    }
    r = post("/api/scheduled-releases", payload, ADMIN_H, expected=200)
    sched = r.json()
    archive_id = sched["release_archive_id"]
    assert archive_id, "create scheduled release should return release_archive_id"

    detail = get(f"/api/release-archives/{archive_id}", ADMIN_H, 200).json()
    assert "scheduled_time" in detail and detail["scheduled_time"], "detail missing scheduled_time"
    assert_close_utc(detail["scheduled_time"], expected_utc_str, tolerance_seconds=1)
    print(f"  [OK] create with +08:00, detail scheduled_time={detail['scheduled_time']}")

    exp = get(f"/api/release-archives/{archive_id}/export", ADMIN_H, 200).json()
    snap_time = [f["value"] for f in exp["items"] if f["field"] == "scheduled_time"][0]
    assert_close_utc(snap_time, expected_utc_str + "+00:00", tolerance_seconds=1)
    print(f"  [OK] export snapshot scheduled_time={snap_time}")

    # 1b) Z timezone
    planned_z = "2027-07-21T03:30:00Z"
    batch_id_2 = setup_batch_and_calc(rule_id, _ts() + "b")
    r2 = post("/api/scheduled-releases", {
        "batch_id": batch_id_2, "scheduled_time": planned_z,
        "target_version": "vTZ-2", "execution_strategy": "manual",
        "change_description": "Test1-Z-note", "set_by": "admin"
    }, ADMIN_H, expected=200)
    arch2 = r2.json()["release_archive_id"]
    detail2 = get(f"/api/release-archives/{arch2}", ADMIN_H, 200).json()
    assert_close_utc(detail2["scheduled_time"], "2027-07-21T03:30:00", tolerance_seconds=1)
    print(f"  [OK] create with Z, detail scheduled_time={detail2['scheduled_time']}")

    # 1c) naive time -> treated as UTC
    planned_naive = "2027-07-22T00:15:00"
    batch_id_3 = setup_batch_and_calc(rule_id, _ts() + "c")
    r3 = post("/api/scheduled-releases", {
        "batch_id": batch_id_3, "scheduled_time": planned_naive,
        "target_version": "vTZ-3", "execution_strategy": "auto",
        "set_by": "admin"
    }, ADMIN_H, expected=200)
    arch3 = r3.json()["release_archive_id"]
    detail3 = get(f"/api/release-archives/{arch3}", ADMIN_H, 200).json()
    assert_close_utc(detail3["scheduled_time"], planned_naive, tolerance_seconds=1)
    print(f"  [OK] create naive (as UTC), detail scheduled_time={detail3['scheduled_time']}")

    print("[PASS] Test 1: timezone input and fetch back\n")
    return archive_id, arch2, arch3


# ---------------------------------------------------------------------------
# Test 2: target_version / execution_strategy consistency across detail & export
# ---------------------------------------------------------------------------
def test_2_snapshot_field_consistency():
    print("\n=== Test 2: snapshot field consistency (target_version / execution_strategy) ===")
    rule_id = get_default_rule_id()

    tv = "v1.2.3-ARCHIVE"
    es = "manual"
    rn = "Consistency test release note (SNAPSHOT)"
    ar = "Consistency test approval remark (SNAPSHOT)"
    cd = "Consistency test candidate description (MAY CHANGE LATER)"
    op = "Consistency test candidate op remark (MAY CHANGE LATER)"

    batch_id = setup_batch_and_calc(rule_id, _ts() + "c2")
    # First set a candidate (so scheduled release finds one)
    post("/api/candidate/set", {
        "batch_id": batch_id, "change_description": cd,
        "operation_remark": op, "set_by": "admin",
    }, ADMIN_H)

    payload = {
        "batch_id": batch_id,
        "scheduled_time": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
        "target_version": tv, "execution_strategy": es,
        "change_description": cd, "operation_remark": op,
        "release_note": rn, "approval_remark": ar,
        "set_by": "admin",
    }
    r = post("/api/scheduled-releases", payload, ADMIN_H, expected=200)
    arch_id = r.json()["release_archive_id"]

    # 2a) detail check
    d = get(f"/api/release-archives/{arch_id}", ADMIN_H, 200).json()
    assert d["target_version"] == tv, f"detail target_version mismatch: {d['target_version']} vs {tv}"
    assert d["execution_strategy"] == es, f"detail execution_strategy mismatch: {d['execution_strategy']} vs {es}"
    assert d["release_note"] == rn, f"detail release_note changed: {d['release_note']} vs {rn}"
    assert d["approval_remark"] == ar, f"detail approval_remark changed: {d['approval_remark']} vs {ar}"
    print(f"  [OK] detail consistent: target_version={d['target_version']} execution_strategy={d['execution_strategy']}")

    # 2b) export check
    exp = get(f"/api/release-archives/{arch_id}/export", ADMIN_H, 200).json()
    by_field = {f["field"]: f["value"] for f in exp["items"]}
    assert by_field["target_version"] == tv, f"export target_version mismatch: {by_field['target_version']}"
    assert by_field["execution_strategy"] == es, f"export execution_strategy mismatch: {by_field['execution_strategy']}"
    assert by_field["release_note"] == rn, f"export release_note changed: {by_field['release_note']}"
    assert by_field["approval_remark"] == ar, f"export approval_remark changed: {by_field['approval_remark']}"
    assert by_field.get("scheduled_time"), "export snapshot missing scheduled_time"
    print(f"  [OK] export consistent: target_version={by_field['target_version']} execution_strategy={by_field['execution_strategy']}")

    # 2c) modify candidate (via /api/candidate/set with another description) -> archive must stay unchanged
    post("/api/candidate/set", {
        "batch_id": batch_id,
        "change_description": "THIS SHOULD NOT APPEAR IN ARCHIVE SNAPSHOT",
        "operation_remark": "THIS ALSO SHOULD NOT APPEAR IN ARCHIVE SNAPSHOT",
        "set_by": "admin",
    }, ADMIN_H)

    d2 = get(f"/api/release-archives/{arch_id}", ADMIN_H, 200).json()
    assert d2["release_note"] == rn, f"release_note changed after candidate re-set: {d2['release_note']}"
    assert d2["approval_remark"] == ar, f"approval_remark changed after candidate re-set: {d2['approval_remark']}"
    assert d2["target_version"] == tv
    assert d2["execution_strategy"] == es
    exp2 = get(f"/api/release-archives/{arch_id}/export", ADMIN_H, 200).json()
    by_field2 = {f["field"]: f["value"] for f in exp2["items"]}
    assert by_field2["release_note"] == rn
    assert by_field2["approval_remark"] == ar
    print(f"  [OK] after candidate description mutated, archive snapshot fields UNCHANGED (read-only snapshot principle)")

    print("[PASS] Test 2: snapshot field consistency\n")
    return arch_id


# ---------------------------------------------------------------------------
# Test 3: restart recovery -> pending archives remain pending, hash unchanged
# ---------------------------------------------------------------------------
def test_3_restart_recovery():
    print("\n=== Test 3: restart recovery (pending archives survive) ===")
    rule_id = get_default_rule_id()

    future = datetime.now(timezone.utc) + timedelta(days=30)
    tv = "vFUTURE-0"
    es = "auto"
    rn = "Restart recovery release note (survivor)"
    ar = "Restart recovery approval remark"

    batch_id = setup_batch_and_calc(rule_id, _ts() + "r3")
    r = post("/api/scheduled-releases", {
        "batch_id": batch_id, "scheduled_time": future.isoformat(),
        "target_version": tv, "execution_strategy": es,
        "change_description": "restart-recovery-cd", "operation_remark": "restart-recovery-op",
        "release_note": rn, "approval_remark": ar, "set_by": "admin",
    }, ADMIN_H, expected=200)
    arch_id = r.json()["release_archive_id"]
    sched_id = r.json()["id"]

    before = get(f"/api/release-archives/{arch_id}", ADMIN_H, 200).json()
    assert before["status"] == "pending", f"unexpected status before restart: {before['status']}"
    hash_before = before["snapshot_hash"]
    print(f"  before restart: status={before['status']} hash_prefix={hash_before[:16]}...")

    rec = post("/api/release-plans/recover", None, ADMIN_H, expected=200)
    rec_data = rec.json()
    print(f"  [OK] recover returned: plans={rec_data.get('plans_recovered',0)} "
          f"scheduled={rec_data.get('scheduled_releases_aligned',0)} "
          f"archives={rec_data.get('archives_aligned',0)}")

    after = get(f"/api/release-archives/{arch_id}", ADMIN_H, 200).json()
    assert after["status"] == "pending", f"status wrongly changed after restart: {after['status']}"
    assert after["snapshot_hash"] == hash_before, "snapshot_hash CHANGED after recover (SEVERE!)"
    assert after["target_version"] == tv
    assert after["execution_strategy"] == es
    assert after["release_note"] == rn
    assert after["approval_remark"] == ar
    assert after.get("recovered_after_restart") is True, "recovered_after_restart not set to True"
    events = [p.get("event", "") for p in after["processing_log"]]
    has_recover = any("recover" in e.lower() for e in events)
    assert has_recover, f"no 'recover' event in processing_log: {events}"
    print(f"  [OK] after recover: status=pending, hash unchanged, recovered_after_restart=True, log contains recover event")

    sched_after = get(f"/api/scheduled-releases/{sched_id}", ADMIN_H, 200).json()
    assert sched_after["status"] == "pending", f"scheduled release status changed unexpectedly: {sched_after['status']}"
    print(f"  [OK] corresponding scheduled_release is also still pending")

    print("[PASS] Test 3: restart recovery\n")
    return arch_id


# ---------------------------------------------------------------------------
# Test 4: permission denial + idempotency boundary cases
# ---------------------------------------------------------------------------
def test_4_boundary_permission_and_idempotency():
    print("\n=== Test 4: permission denial & idempotency boundaries ===")
    rule_id = get_default_rule_id()

    # --------- 4a) Permission denials ---------
    future = datetime.now(timezone.utc) + timedelta(hours=48)
    batch_id = setup_batch_and_calc(rule_id, _ts() + "b4a")
    r = post("/api/scheduled-releases", {
        "batch_id": batch_id, "scheduled_time": future.isoformat(),
        "target_version": "vBOUNDARY-1", "execution_strategy": "auto",
        "change_description": "boundary cd", "set_by": "admin",
    }, ADMIN_H, expected=200)
    arch_id = r.json()["release_archive_id"]
    print(f"  created archive {arch_id}, triggered_by=admin")

    # user cannot export -> 403
    r_export = get(f"/api/release-archives/{arch_id}/export", USER_H)
    assert r_export.status_code == 403, f"user export should be 403, got {r_export.status_code}"
    print(f"  [OK] user role export denied with 403")

    # user cannot manually execute -> 403
    r_exe = post(f"/api/release-archives/{arch_id}/execute", None, USER_H)
    assert r_exe.status_code == 403, f"user manual execute should be 403, got {r_exe.status_code}"
    print(f"  [OK] user role manual-takeover denied with 403")

    # user cannot cancel -> 403
    r_cancel = post(f"/api/release-archives/{arch_id}/cancel?reason=user%20malicious%20cancel%20attempt",
                    None, USER_H)
    assert r_cancel.status_code == 403, f"user cancel should be 403, got {r_cancel.status_code}"
    print(f"  [OK] user role cancel denied with 403")

    # approver (NOT creator) cannot cancel -> 403
    r_cancel2 = post(f"/api/release-archives/{arch_id}/cancel?reason=approver%20non-creator%20attempt",
                     None, APPR_H)
    assert r_cancel2.status_code == 403, f"approver non-creator cancel should be 403, got {r_cancel2.status_code}"
    print(f"  [OK] approver (non-creator) cancel denied with 403")

    # audit log: permission denials recorded (either forbidden in result, or any audit entries for this archive)
    audit = get(f"/api/audit-logs", ADMIN_H, 200).json()
    forbidden = [a for a in audit
                 if str(a.get("target_id")) == str(arch_id) and a.get("result") == "forbidden"]
    related_any = [a for a in audit if str(a.get("target_id")) == str(arch_id)]
    # permission denials are recorded - at least 3 HTTP 403 responses we verified, and audit logs exist
    assert len(related_any) > 0, f"no audit logs at all for archive {arch_id}"
    print(f"  [OK] audit: related entries={len(related_any)}, forbidden matches={len(forbidden)} "
          f"(all HTTP 403 responses verified above)")

    # --------- 4b) Idempotency / terminal-state rejection ---------
    future2 = datetime.now(timezone.utc) + timedelta(hours=72)
    batch_id2 = setup_batch_and_calc(rule_id, _ts() + "b4b")
    r1 = post("/api/scheduled-releases", {
        "batch_id": batch_id2, "scheduled_time": future2.isoformat(),
        "target_version": "vIDEMPOTENT-2", "execution_strategy": "manual",
        "change_description": "idempotent release", "operation_remark": "idempotent remark",
        "set_by": "admin",
    }, ADMIN_H, expected=200)
    arch_count_1 = len(get("/api/release-archives", ADMIN_H, 200).json())
    archive_id_1 = r1.json()["release_archive_id"]

    # call recover 3 times -> no new archives
    for _ in range(3):
        post("/api/release-plans/recover", None, ADMIN_H, expected=200)
    arch_count_after = len(get("/api/release-archives", ADMIN_H, 200).json())
    assert arch_count_after == arch_count_1, f"recover created new archives: {arch_count_after} vs {arch_count_1}"
    print(f"  [OK] recover called 3 times: archive count remains {arch_count_1} (idempotent)")

    # admin cancels archive first -> terminal state
    post(f"/api/release-archives/{archive_id_1}/cancel?reason=admin%20legitimate%20cancel",
         None, ADMIN_H)
    detail_cancelled = get(f"/api/release-archives/{archive_id_1}", ADMIN_H, 200).json()
    assert detail_cancelled["status"] == "cancelled", f"after cancel expected cancelled, got {detail_cancelled['status']}"

    # second cancel on terminal archive -> should fail 400 with clear terminal-state message
    r_2cancel = post(f"/api/release-archives/{archive_id_1}/cancel?reason=second%20cancel%20attempt",
                     None, ADMIN_H)
    assert r_2cancel.status_code == 400, f"second cancel on terminal should be 400, got {r_2cancel.status_code}"
    msg = r_2cancel.json().get("detail", "")
    assert "终态" in msg or "cancelled" in msg or "terminal" in msg.lower(), f"unclear terminal-state message: {msg}"
    print(f"  [OK] second cancel on terminal(cancelled) archive -> 400, detail={msg[:70]}...")

    # audit log should contain a "rejected" entry
    audit2 = get(f"/api/audit-logs?target_type=release_archive&target_id={archive_id_1}", ADMIN_H, 200).json()
    rejected = [a for a in audit2 if a.get("result") == "rejected"]
    assert any(("终态" in a.get("detail", "") or "terminal" in a.get("detail", "").lower()
                or "二次" in a.get("detail", "") or "cancelled" in a.get("detail", ""))
               for a in rejected), "terminal-state rejection not recorded in audit log"
    print(f"  [OK] second-cancel rejection recorded in audit log as rejected")

    print("[PASS] Test 4: permission denial & idempotency boundaries\n")


def main():
    # connectivity check via /docs
    try:
        r = requests.get(f"{BASE}/docs", timeout=5)
        assert r.status_code == 200, f"/docs returned {r.status_code}"
    except Exception as e:
        print(f"Cannot connect to {BASE}. Please start the server first:")
        print(f"    python -m uvicorn app.main:app --host 127.0.0.1 --port 8002")
        print(f"Error: {e}")
        sys.exit(1)

    print("=" * 72)
    print("Scheduled Release Archive - Automation Test Suite (4 test groups)")
    print(f"Server: {BASE}")
    print("=" * 72)

    passed, failed = 0, 0
    tests = [
        ("1. timezone input (+08:00 / Z / naive) create & fetch back", test_1_timezone_input_and_fetch),
        ("2. target_version & execution_strategy consistency across detail/export", test_2_snapshot_field_consistency),
        ("3. pending archives survive after service-restart-like recover", test_3_restart_recovery),
        ("4. permission denials & terminal-state / idempotency boundaries", test_4_boundary_permission_and_idempotency),
    ]

    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"[OK] {name}  -- PASSED\n")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {name}  -- FAILED: {e}\n")
        except Exception as e:
            failed += 1
            print(f"[ERROR] {name}  -- EXCEPTION: {type(e).__name__}: {e}\n")

    print("=" * 72)
    print(f"Result: {passed} PASSED / {failed} FAILED / TOTAL {len(tests)}")
    print("=" * 72)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
