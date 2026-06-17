import requests
import json
import sys
import time
from datetime import datetime, timedelta

BASE_URL = "http://127.0.0.1:8002"
PASS_COUNT = 0
FAIL_COUNT = 0


def check(label, condition, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"    [PASS] {label}")
    else:
        FAIL_COUNT += 1
        msg = f"    [FAIL] {label}"
        if detail:
            msg += f" -- {detail}"
        print(msg)


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def step(n, desc):
    print(f"\n  [{n}] {desc}")
    print(f"  {'-'*50}")


def import_and_calculate(batch_name, rule_id, supplier_code, supplier_name, headers):
    batch = {
        "batch_name": batch_name,
        "rule_id": rule_id,
        "imported_by": headers["X-Username"],
        "suppliers": [
            {"supplier_code": supplier_code, "supplier_name": supplier_name,
             "metrics": {"pass_rate": 0.96, "defect_rate": 0.015, "on_time_rate": 0.94,
                         "lead_time_days": 13, "price_competitiveness": 86, "payment_terms_score": 73}}
        ]
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch, headers=headers)
    if resp.status_code != 200:
        return None, None
    batch_id = resp.json()["id"]
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/calculate", headers=headers)
    return batch_id, resp


def iso_future(seconds_from_now):
    dt = datetime.utcnow() + timedelta(seconds=seconds_from_now)
    return dt.replace(microsecond=0).isoformat()


def wait_until(condition_fn, timeout_sec=30, poll_sec=1, label=""):
    end = time.time() + timeout_sec
    while time.time() < end:
        if condition_fn():
            return True
        time.sleep(poll_sec)
    return False


def get_archive_for_scheduled(sched_id, headers):
    resp = requests.get(f"{BASE_URL}/api/release-archives",
                        params={"scheduled_release_id": sched_id},
                        headers=headers)
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return None


def main():
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    approver_h = {"X-Username": "approver1", "Content-Type": "application/json"}
    user_h = {"X-Username": "user1", "Content-Type": "application/json"}

    section("1. Schema Version & Migration Verification")

    step("1.1", "Check schema version >= 4")
    resp = requests.get(f"{BASE_URL}/api/_meta/schema-version", headers=admin_h)
    check("schema-version接口200", resp.status_code == 200, f"actual={resp.status_code}")
    schema_ver = None
    if resp.status_code == 200:
        body = resp.json()
        schema_ver = body.get("schema_version") or body.get("target_version")
        check("schema version >= 4", schema_ver is not None and schema_ver >= 4,
              f"actual={schema_ver}")
        print(f"    schema_version = {schema_ver}")

    step("1.2", "Verify archive tables exist via stats endpoint")
    resp = requests.get(f"{BASE_URL}/api/release-archives-stats", headers=admin_h)
    check("archive stats接口200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
    if resp.status_code == 200:
        stats = resp.json()
        check("stats contains total field", "total" in stats, f"keys={list(stats.keys())}")
        print(f"    archive stats = {json.dumps(stats, ensure_ascii=False)}")

    section("2. Archive Creation & Immutable Snapshot Test")

    step("2.1", "Get rule_id")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    assert resp.status_code == 200, f"无法获取规则: {resp.status_code} {resp.text}"
    rule_id = resp.json()[0]["id"]
    print(f"    rule_id = {rule_id}")

    step("2.2", "Import and calculate batch ARCHIVE-TEST-1")
    batch_1_id, _ = import_and_calculate(
        "档案测试-批次ARCHIVE-TEST-1", rule_id, "ARCHIVE-TEST-1", "档案测试供应商1", admin_h
    )
    check("批次ARCHIVE-TEST-1导入计算成功", batch_1_id is not None)

    step("2.3", "Create scheduled release with release_note and approval_remark")
    sched_time_1 = iso_future(120)
    release_note_test = "测试发布说明-档案完整性验证"
    approval_remark_test = "测试审批备注-需要合规审计"
    sched_req_1 = {
        "batch_id": batch_1_id,
        "scheduled_time": sched_time_1,
        "change_description": release_note_test,
        "operation_remark": approval_remark_test,
        "release_note": release_note_test,
        "approval_remark": approval_remark_test,
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_1, headers=admin_h)
    check("创建预约返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
    sched_1_id = None
    if resp.status_code == 200:
        body = resp.json()
        sched_1_id = body["scheduled_release"]["id"]
        check("预约初始状态pending", body["scheduled_release"]["status"] == "pending")
    print(f"    sched_1_id = {sched_1_id}, scheduled_time = {sched_time_1}")

    step("2.4", "Verify archive created automatically with correct snapshot fields")
    archive_1 = None
    if sched_1_id:
        def archive_exists():
            return get_archive_for_scheduled(sched_1_id, admin_h) is not None
        ok = wait_until(archive_exists, timeout_sec=10, poll_sec=1, label="等待档案创建")
        check("档案已自动创建", ok)
        archive_1 = get_archive_for_scheduled(sched_1_id, admin_h)
        if archive_1:
            print(f"    archive_1_id = {archive_1['id']}")
            check("archive scheduled_release_id正确", archive_1["scheduled_release_id"] == sched_1_id)
            check("archive source_batch_id正确", archive_1["source_batch_id"] == batch_1_id)
            check("archive triggered_by正确", archive_1["triggered_by"] == "admin")
            check("archive release_note正确", archive_1["release_note"] == release_note_test,
                  f"expected='{release_note_test}', actual='{archive_1['release_note']}'")
            check("archive approval_remark正确", archive_1["approval_remark"] == approval_remark_test,
                  f"expected='{approval_remark_test}', actual='{archive_1['approval_remark']}'")
            check("archive execution_strategy正确", archive_1["execution_strategy"] == "auto")
            check("archive is_immutable=True", archive_1["is_immutable"] == True)
            check("archive status=pending", archive_1["status"] == "pending")
            check("archive snapshot_hash非空", archive_1["snapshot_hash"] is not None and len(archive_1["snapshot_hash"]) == 64)
            check("archive conflict_result=none", archive_1["conflict_result"] == "none")

    step("2.5", "Verify archive idempotency - create same scheduled release again")
    if sched_1_id:
        resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_1, headers=admin_h)
        archives_after = requests.get(f"{BASE_URL}/api/release-archives",
                                       params={"scheduled_release_id": sched_1_id},
                                       headers=admin_h).json()
        check("幂等性验证-档案数量不变", len(archives_after) == 1)

    step("2.6", "Verify snapshot integrity via verify endpoint")
    if archive_1:
        resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_1['id']}/verify", headers=admin_h)
        check("verify接口200", resp.status_code == 200)
        if resp.status_code == 200:
            verify_result = resp.json()
            check("快照哈希验证通过", verify_result["verified"] == True,
                  f"expected={verify_result.get('expected_hash')[:16]}..., actual={verify_result.get('actual_hash')[:16]}...")
            check("is_immutable标记正确", verify_result["is_immutable"] == True)

    step("2.7", "Verify fields via verify-fields endpoint")
    if archive_1:
        verify_fields_req = {
            "archive_id": archive_1["id"],
            "expected_fields": {
                "release_note": release_note_test,
                "approval_remark": approval_remark_test,
                "triggered_by": "admin",
                "source_batch_id": str(batch_1_id),
                "execution_strategy": "auto",
            }
        }
        resp = requests.post(f"{BASE_URL}/api/release-archives/verify-fields",
                             json=verify_fields_req, headers=admin_h)
        check("verify-fields接口200", resp.status_code == 200)
        if resp.status_code == 200:
            result = resp.json()
            check("所有字段匹配", result["verified"] == True,
                  f"mismatched={result['mismatched_fields']}")
            check("匹配字段数=5", len(result["matched_fields"]) == 5,
                  f"matched={result['matched_fields']}")

    section("3. Permission Boundary Test")

    step("3.1", "user1 (role=user) should NOT be able to export archive")
    if archive_1:
        resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_1['id']}/export", headers=user_h)
        check("普通用户导出被拒(403)", resp.status_code == 403, f"actual={resp.status_code}")

    step("3.2", "user1 should NOT be able to view audit-trail")
    if archive_1:
        resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_1['id']}/audit-trail", headers=user_h)
        check("普通用户查看审计链路被拒(403)", resp.status_code == 403, f"actual={resp.status_code}")

    step("3.3", "user1 should NOT be able to cancel archive")
    if archive_1:
        resp = requests.post(f"{BASE_URL}/api/release-archives/{archive_1['id']}/cancel",
                              params={"reason": "测试越权取消"}, headers=user_h)
        check("普通用户取消被拒(403)", resp.status_code == 403, f"actual={resp.status_code}")

    step("3.4", "user1 should BE able to view archive list (ALLOW_ARCHIVE_VIEW_ROLES includes user)")
    resp = requests.get(f"{BASE_URL}/api/release-archives", headers=user_h)
    check("普通用户可查看档案列表(200)", resp.status_code == 200, f"actual={resp.status_code}")

    step("3.5", "approver1 should BE able to export archive")
    if archive_1:
        resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_1['id']}/export", headers=approver_h)
        check("审批人可导出档案(200)", resp.status_code == 200, f"actual={resp.status_code}")

    step("3.6", "approver1 should NOT be able to cancel archive (only admin)")
    if archive_1:
        resp = requests.post(f"{BASE_URL}/api/release-archives/{archive_1['id']}/cancel",
                              params={"reason": "测试审批人越权取消"}, headers=approver_h)
        check("审批人取消被拒(403)", resp.status_code == 403, f"actual={resp.status_code}")

    step("3.7", "Verify permission denied was logged in audit logs")
    resp = requests.get(f"{BASE_URL}/api/audit-logs",
                         params={"action": "permission_denied", "limit": 10},
                         headers=admin_h)
    if resp.status_code == 200:
        logs = resp.json()
        permission_logs = [l for l in logs if l.get("action") == "permission_denied"]
        check("权限拒绝已记录审计日志", len(permission_logs) > 0)

    step("3.8", "Verify archive-specific audit logs exist")
    resp = requests.get(f"{BASE_URL}/api/audit-logs",
                         params={"target_type": "release_archive", "limit": 20},
                         headers=admin_h)
    check("档案操作审计日志存在", resp.status_code == 200)
    if resp.status_code == 200:
        logs = resp.json()
        check("有档案相关审计记录", len(logs) > 0)
        for log in logs[:5]:
            print(f"    - {log['action']}: {log['result']} by {log['operator']}")

    section("4. Export Verification Test")

    step("4.1", "Export archive and verify all snapshot fields")
    if archive_1:
        resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_1['id']}/export", headers=admin_h)
        check("导出接口200", resp.status_code == 200)
        if resp.status_code == 200:
            export_data = resp.json()
            check("导出包含正确的archive_id", export_data["archive_id"] == archive_1["id"])
            check("导出包含正确的snapshot_hash", export_data["snapshot_hash"] == archive_1["snapshot_hash"])
            check("导出包含exported_by", export_data["exported_by"] == "admin")

            items = export_data["items"]
            snapshot_items = [item for item in items if item["is_snapshot"]]
            runtime_items = [item for item in items if not item["is_snapshot"]]

            check("导出包含8个快照字段", len(snapshot_items) == 8,
                  f"actual snapshot fields: {[i['field'] for i in snapshot_items]}")
            check("导出包含9个运行时字段", len(runtime_items) == 9,
                  f"actual runtime fields: {[i['field'] for i in runtime_items]}")

            field_values = {item["field"]: item["value"] for item in items}
            check("导出release_note正确", field_values["release_note"] == release_note_test)
            check("导出approval_remark正确", field_values["approval_remark"] == approval_remark_test)
            check("导出triggered_by正确", field_values["triggered_by"] == "admin")
            check("导出source_batch_id正确", field_values["source_batch_id"] == str(batch_1_id))
            check("导出snapshot_hash正确", field_values["snapshot_hash"] == archive_1["snapshot_hash"])
            check("导出is_immutable=True", field_values["is_immutable"] == "True")

            print(f"    导出快照字段: {[i['field'] for i in snapshot_items]}")
            print(f"    导出运行时字段: {[i['field'] for i in runtime_items]}")

    step("4.2", "Verify reference_count increased after export")
    if archive_1:
        resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_1['id']}", headers=admin_h)
        if resp.status_code == 200:
            archive_detail = resp.json()
            check("引用次数>=1(导出+创建引用)", archive_detail["reference_count"] >= 1,
                  f"actual={archive_detail['reference_count']}")

    section("5. Import Conflict Test")

    step("5.1", "Create pending scheduled release for batch CONFLICT-1")
    batch_conflict_id, _ = import_and_calculate(
        "冲突测试-批次CONFLICT-1", rule_id, "CONFLICT-1", "冲突测试供应商1", admin_h
    )
    check("批次CONFLICT-1导入计算成功", batch_conflict_id is not None)

    sched_time_conflict = iso_future(180)
    sched_req_conflict = {
        "batch_id": batch_conflict_id,
        "scheduled_time": sched_time_conflict,
        "change_description": "冲突测试-将被导入新批次顶掉",
        "operation_remark": "冲突测试审批备注",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_conflict, headers=admin_h)
    check("创建冲突测试预约返回200", resp.status_code == 200)
    sched_conflict_id = resp.json()["scheduled_release"]["id"] if resp.status_code == 200 else None

    step("5.2", "Get archive for conflict test scheduled release")
    archive_conflict = None
    if sched_conflict_id:
        archive_conflict = get_archive_for_scheduled(sched_conflict_id, admin_h)
        check("冲突测试档案存在", archive_conflict is not None)
        if archive_conflict:
            check("冲突测试档案初始状态=pending", archive_conflict["status"] == "pending")
            check("冲突测试档案初始conflict_result=none", archive_conflict["conflict_result"] == "none")

    step("5.3", "Import new batch for same rule to trigger conflict")
    batch_new_id, _ = import_and_calculate(
        "冲突测试-新批次NEW-1", rule_id, "CONFLICT-NEW-1", "新批次供应商", admin_h
    )
    check("新批次NEW-1导入计算成功", batch_new_id is not None)

    step("5.4", "Verify old archive status updated to superseded with import_conflict")
    if archive_conflict:
        def archive_superseded():
            a = get_archive_for_scheduled(sched_conflict_id, admin_h)
            return a and a["status"] == "superseded"
        ok = wait_until(archive_superseded, timeout_sec=10, poll_sec=1, label="等待档案状态更新")
        check("导入冲突后旧档案状态=superseded", ok)

        archive_updated = get_archive_for_scheduled(sched_conflict_id, admin_h)
        if archive_updated:
            check("冲突结果=import_conflict", archive_updated["conflict_result"] == "import_conflict",
                  f"actual={archive_updated['conflict_result']}")
            check("冲突详情包含导入信息", "导入" in archive_updated["conflict_detail"] or "批次" in archive_updated["conflict_detail"],
                  f"actual={archive_updated['conflict_detail']}")
            check("快照字段未改变", archive_updated["release_note"] == "冲突测试-将被导入新批次顶掉")
            check("快照哈希未改变", archive_updated["snapshot_hash"] == archive_conflict["snapshot_hash"])

            print(f"    冲突后档案状态: {archive_updated['status']}")
            print(f"    冲突结果: {archive_updated['conflict_result']}")
            print(f"    快照哈希验证: {archive_updated['snapshot_hash'][:16]}... (未变)")

    step("5.5", "Verify audit-trail shows complete link for conflict archive")
    if archive_conflict:
        resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_conflict['id']}/audit-trail", headers=admin_h)
        check("审计链路接口200", resp.status_code == 200)
        if resp.status_code == 200:
            trail = resp.json()
            check("审计链路包含事件", len(trail["events"]) > 0)
            check("审计链路包含快照验证", "snapshot_verified" in trail)
            event_types = [e.get("event", "") for e in trail["events"]]
            check("链路包含created事件", any("created" in e for e in event_types))
            check("链路包含status_pending_to_superseded事件",
                  any("status_pending_to_superseded" in e for e in event_types))
            check("链路包含reference:release_plan事件",
                  any("reference:release_plan" in e for e in event_types))
            print(f"    审计链路事件数: {len(trail['events'])}")
            print(f"    事件类型: {event_types[:10]}")

    section("6. Cross-Restart Persistence Snapshot")

    step("6.1", "Create scheduled release for restart test")
    batch_restart_id, _ = import_and_calculate(
        "重启测试-批次RESTART-1", rule_id, "RESTART-1", "重启测试供应商", admin_h
    )
    check("批次RESTART-1导入计算成功", batch_restart_id is not None)

    sched_time_restart = iso_future(30)
    release_note_restart = "重启测试发布说明-验证跨重启不变"
    approval_remark_restart = "重启测试审批备注-跨重启完整性"
    sched_req_restart = {
        "batch_id": batch_restart_id,
        "scheduled_time": sched_time_restart,
        "change_description": release_note_restart,
        "operation_remark": approval_remark_restart,
        "release_note": release_note_restart,
        "approval_remark": approval_remark_restart,
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_restart, headers=admin_h)
    check("重启测试预约创建200", resp.status_code == 200, f"body={resp.text}")
    sched_restart_id = None
    if resp.status_code == 200:
        sched_restart_id = resp.json()["scheduled_release"]["id"]

    step("6.2", "Get archive and take snapshot of all fields")
    archive_restart = None
    if sched_restart_id:
        def archive_created():
            return get_archive_for_scheduled(sched_restart_id, admin_h) is not None
        wait_until(archive_created, timeout_sec=10, poll_sec=1)
        archive_restart = get_archive_for_scheduled(sched_restart_id, admin_h)
        check("重启测试档案存在", archive_restart is not None)

    snapshot = {}
    if archive_restart:
        snapshot = {
            "archive_id": archive_restart["id"],
            "sched_restart_id": sched_restart_id,
            "batch_restart_id": batch_restart_id,
            "release_note": archive_restart["release_note"],
            "approval_remark": archive_restart["approval_remark"],
            "triggered_by": archive_restart["triggered_by"],
            "source_batch_id": archive_restart["source_batch_id"],
            "execution_strategy": archive_restart["execution_strategy"],
            "snapshot_hash": archive_restart["snapshot_hash"],
            "is_immutable": archive_restart["is_immutable"],
            "status_before": archive_restart["status"],
            "scheduled_time": sched_time_restart,
            "rule_id": rule_id,
        }
        print(f"    重启前快照 = {json.dumps(snapshot, ensure_ascii=False, indent=2)}")

        resp = requests.get(f"{BASE_URL}/api/release-archives/{snapshot['archive_id']}/verify", headers=admin_h)
        if resp.status_code == 200:
            snapshot["verify_before"] = resp.json()
            check("重启前快照验证通过", snapshot["verify_before"]["verified"] == True)

    step("6.3", "Trigger manual recovery to simulate restart")
    resp = requests.post(f"{BASE_URL}/api/release-plans/recover", headers=admin_h)
    check("手动触发恢复接口200", resp.status_code == 200)
    if resp.status_code == 200:
        recovery_stats = resp.json()
        print(f"    恢复统计 = {json.dumps(recovery_stats, ensure_ascii=False)}")
        if "archives" in recovery_stats:
            arch_stats = recovery_stats["archives"]
            check("档案恢复统计包含verified_intact", "verified_intact" in arch_stats)
            print(f"    档案恢复 verified_intact={arch_stats.get('verified_intact')}")

    step("6.4", "Verify archive fields unchanged after simulated restart")
    if snapshot.get("archive_id"):
        resp = requests.get(f"{BASE_URL}/api/release-archives/{snapshot['archive_id']}", headers=admin_h)
        check("重启后档案查询200", resp.status_code == 200)
        if resp.status_code == 200:
            archive_after = resp.json()
            check("重启后release_note不变", archive_after["release_note"] == snapshot["release_note"])
            check("重启后approval_remark不变", archive_after["approval_remark"] == snapshot["approval_remark"])
            check("重启后triggered_by不变", archive_after["triggered_by"] == snapshot["triggered_by"])
            check("重启后source_batch_id不变", archive_after["source_batch_id"] == snapshot["source_batch_id"])
            check("重启后execution_strategy不变", archive_after["execution_strategy"] == snapshot["execution_strategy"])
            check("重启后snapshot_hash不变", archive_after["snapshot_hash"] == snapshot["snapshot_hash"])
            check("重启后is_immutable不变", archive_after["is_immutable"] == snapshot["is_immutable"])
            check("重启后recovered_after_restart=True", archive_after["recovered_after_restart"] == True)

    step("6.5", "Verify snapshot integrity after restart")
    if snapshot.get("archive_id"):
        resp = requests.get(f"{BASE_URL}/api/release-archives/{snapshot['archive_id']}/verify", headers=admin_h)
        check("重启后验证接口200", resp.status_code == 200)
        if resp.status_code == 200:
            verify_after = resp.json()
            check("重启后快照验证通过", verify_after["verified"] == True,
                  f"expected={verify_after.get('expected_hash')[:16]}..., actual={verify_after.get('actual_hash')[:16]}...")
            check("重启后哈希与重启前一致", verify_after["actual_hash"] == snapshot["snapshot_hash"])

    step("6.6", "Verify all fields via verify-fields after restart")
    if snapshot.get("archive_id"):
        verify_fields_req = {
            "archive_id": snapshot["archive_id"],
            "expected_fields": {
                "release_note": snapshot["release_note"],
                "approval_remark": snapshot["approval_remark"],
                "triggered_by": snapshot["triggered_by"],
                "source_batch_id": str(snapshot["source_batch_id"]),
                "execution_strategy": snapshot["execution_strategy"],
                "scheduled_release_id": str(snapshot["sched_restart_id"]),
            }
        }
        resp = requests.post(f"{BASE_URL}/api/release-archives/verify-fields",
                             json=verify_fields_req, headers=admin_h)
        check("重启后字段验证接口200", resp.status_code == 200)
        if resp.status_code == 200:
            result = resp.json()
            check("重启后所有字段匹配", result["verified"] == True,
                  f"mismatched={result['mismatched_fields']}")
            check("重启后匹配字段数=6", len(result["matched_fields"]) == 6,
                  f"matched={result['matched_fields']}")

    step("6.7", "Print restart command for actual restart verification")
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    print()
    print("    >>> 如需真实重启测试，请在此时手动重启服务(8002端口)后运行: <<<")
    print(f"    命令: python {sys.argv[0]} --verify-restart \"{snapshot_json}\"")
    print("    注意: 真实重启后档案字段应保持完全一致，snapshot_hash不变。")

    section("测试结果汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


def verify_restart(snapshot_json):
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    snapshot = json.loads(snapshot_json)
    archive_id = snapshot["archive_id"]

    section("7. Post-Restart Verification (真实重启后)")

    step("7.1", "Verify archive still exists after real restart")
    resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_id}", headers=admin_h)
    check("重启后档案查询200", resp.status_code == 200)

    archive_after = None
    if resp.status_code == 200:
        archive_after = resp.json()

    step("7.2", "Verify ALL snapshot fields unchanged after real restart")
    if archive_after:
        check("真实重启后release_note不变", archive_after["release_note"] == snapshot["release_note"],
              f"expected='{snapshot['release_note']}', actual='{archive_after['release_note']}'")
        check("真实重启后approval_remark不变", archive_after["approval_remark"] == snapshot["approval_remark"],
              f"expected='{snapshot['approval_remark']}', actual='{archive_after['approval_remark']}'")
        check("真实重启后triggered_by不变", archive_after["triggered_by"] == snapshot["triggered_by"],
              f"expected='{snapshot['triggered_by']}', actual='{archive_after['triggered_by']}'")
        check("真实重启后source_batch_id不变", archive_after["source_batch_id"] == snapshot["source_batch_id"],
              f"expected={snapshot['source_batch_id']}, actual={archive_after['source_batch_id']}")
        check("真实重启后execution_strategy不变", archive_after["execution_strategy"] == snapshot["execution_strategy"],
              f"expected='{snapshot['execution_strategy']}', actual='{archive_after['execution_strategy']}'")
        check("真实重启后snapshot_hash不变", archive_after["snapshot_hash"] == snapshot["snapshot_hash"],
              f"expected={snapshot['snapshot_hash'][:16]}..., actual={archive_after['snapshot_hash'][:16]}...")
        check("真实重启后is_immutable=True", archive_after["is_immutable"] == True)

    step("7.3", "Verify snapshot integrity via verify endpoint")
    resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_id}/verify", headers=admin_h)
    check("验证接口200", resp.status_code == 200)
    if resp.status_code == 200:
        verify_result = resp.json()
        check("快照哈希验证通过", verify_result["verified"] == True)
        check("哈希与重启前一致", verify_result["actual_hash"] == snapshot["snapshot_hash"])

    step("7.4", "Verify all 6 fields via verify-fields")
    verify_fields_req = {
        "archive_id": archive_id,
        "expected_fields": {
            "release_note": snapshot["release_note"],
            "approval_remark": snapshot["approval_remark"],
            "triggered_by": snapshot["triggered_by"],
            "source_batch_id": str(snapshot["source_batch_id"]),
            "execution_strategy": snapshot["execution_strategy"],
            "scheduled_release_id": str(snapshot["sched_restart_id"]),
        }
    }
    resp = requests.post(f"{BASE_URL}/api/release-archives/verify-fields",
                         json=verify_fields_req, headers=admin_h)
    check("字段验证接口200", resp.status_code == 200)
    if resp.status_code == 200:
        result = resp.json()
        check("所有6个字段匹配", result["verified"] == True,
              f"mismatched={result['mismatched_fields']}")
        check("matched_fields=6", len(result["matched_fields"]) == 6,
              f"matched={result['matched_fields']}")

    step("7.5", "Verify recovered_after_restart flag is True")
    if archive_after:
        check("recovered_after_restart=True", archive_after["recovered_after_restart"] == True,
              f"actual={archive_after['recovered_after_restart']}")

    step("7.6", "Verify archive export works after restart")
    resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_id}/export", headers=admin_h)
    check("导出接口200", resp.status_code == 200)
    if resp.status_code == 200:
        export_data = resp.json()
        field_values = {item["field"]: item["value"] for item in export_data["items"]}
        check("导出release_note不变", field_values["release_note"] == snapshot["release_note"])
        check("导出approval_remark不变", field_values["approval_remark"] == snapshot["approval_remark"])
        check("导出snapshot_hash不变", field_values["snapshot_hash"] == snapshot["snapshot_hash"])

    step("7.7", "Verify audit-trail is complete after restart")
    resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_id}/audit-trail", headers=admin_h)
    check("审计链路接口200", resp.status_code == 200)
    if resp.status_code == 200:
        trail = resp.json()
        check("审计链路包含事件", len(trail["events"]) > 0)
        event_types = [e.get("event", "") for e in trail["events"]]
        check("链路包含restart_recovered事件",
              any("restart_recovered" in e for e in event_types),
              f"events={event_types}")

    section("真实重启验证汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--verify-restart":
        verify_restart(sys.argv[2])
    else:
        main()
