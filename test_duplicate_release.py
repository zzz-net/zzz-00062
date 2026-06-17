import requests
import json
import sys

BASE_URL = "http://127.0.0.1:8000"
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


def main():
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    approver_h = {"X-Username": "approver1", "Content-Type": "application/json"}
    user_h = {"X-Username": "user1", "Content-Type": "application/json"}

    section("1. 首次发布 -> 审计正常写入")

    step("1.1", "获取规则并导入批次")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    rule_id = resp.json()[0]["id"]

    batch = {
        "batch_name": "重复发布测试批次",
        "rule_id": rule_id,
        "imported_by": "admin",
        "suppliers": [
            {"supplier_code": "DUP-001", "supplier_name": "重复测试供应商",
             "metrics": {"pass_rate": 0.95, "defect_rate": 0.018, "on_time_rate": 0.93,
                         "lead_time_days": 14, "price_competitiveness": 86, "payment_terms_score": 72}}
        ]
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch, headers=admin_h)
    check("导入返回200", resp.status_code == 200)
    batch_id = resp.json()["id"]

    step("1.2", "计算草稿")
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/calculate", headers=approver_h)
    check("计算返回200", resp.status_code == 200)

    step("1.3", "首次发布")
    approve = {"approved_by": "approver1", "approval_remark": "首次发布备注", "release_note": "首次发布说明"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/release", json=approve, headers=approver_h)
    check("首次发布返回200", resp.status_code == 200, f"actual={resp.status_code}")
    version = resp.json()["version"] if resp.status_code == 200 else ""

    step("1.4", "验证 release/success 审计只有1条")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=release", headers=admin_h)
    logs = resp.json()
    success_logs = [l for l in logs if l["result"] == "success" and l["target_id"] == version]
    check("release/success 审计1条", len(success_logs) == 1, f"count={len(success_logs)}")

    step("1.5", "验证版本数量=1")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    release_count_after_first = len(resp.json())
    check("版本数量=1", release_count_after_first == 1, f"count={release_count_after_first}")

    section("2. 第2次重复发布请求 -> 只写1条 duplicate_rejected 审计")

    step("2.1", "第2次发布请求")
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/release", json=approve, headers=approver_h)
    check("第2次发布返回400", resp.status_code == 400, f"actual={resp.status_code}")
    check("错误信息含'已发布过'", "已发布过" in resp.json().get("detail", ""), f"detail={resp.json().get('detail','')}")

    step("2.2", "验证 duplicate_rejected 审计只有1条")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=release", headers=admin_h)
    logs = resp.json()
    dup_logs = [l for l in logs if l["result"] == "duplicate_rejected" and l["target_id"] == str(batch_id)]
    check("duplicate_rejected 审计1条", len(dup_logs) == 1, f"count={len(dup_logs)}")

    if dup_logs:
        l = dup_logs[0]
        check("操作类型=release", l["action"] == "release")
        check("操作者=approver1", l["operator"] == "approver1")
        check("目标类型=batch", l["target_type"] == "batch")
        check("目标ID=批次ID", l["target_id"] == str(batch_id))
        check("结果=duplicate_rejected", l["result"] == "duplicate_rejected")
        check("时间存在", l["created_at"] is not None)

    step("2.3", "验证版本数量未增加")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    check("版本数量仍=1", len(resp.json()) == release_count_after_first,
          f"count={len(resp.json())}")

    section("3. 第3次重复发布请求 -> 不再追加审计")

    step("3.1", "第3次发布请求")
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/release", json=approve, headers=approver_h)
    check("第3次发布返回400", resp.status_code == 400, f"actual={resp.status_code}")

    step("3.2", "验证 duplicate_rejected 审计仍只有1条（关键：审计不膨胀）")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=release", headers=admin_h)
    logs = resp.json()
    dup_logs = [l for l in logs if l["result"] == "duplicate_rejected" and l["target_id"] == str(batch_id)]
    check("duplicate_rejected 审计仍1条（不膨胀）", len(dup_logs) == 1, f"count={len(dup_logs)}")

    step("3.3", "验证 release/success 审计仍只有1条")
    success_logs = [l for l in logs if l["result"] == "success" and l["target_id"] == version]
    check("release/success 审计仍1条", len(success_logs) == 1, f"count={len(success_logs)}")

    step("3.4", "验证版本数量仍未增加")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    check("版本数量仍=1", len(resp.json()) == release_count_after_first,
          f"count={len(resp.json())}")

    section("4. 审计总数和字段验证")

    step("4.1", "该批次全部 release 审计只有2条（1 success + 1 duplicate_rejected）")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=release", headers=admin_h)
    logs = resp.json()
    batch_release_logs = [l for l in logs if l["target_id"] in [version, str(batch_id)]]
    check("该批次 release 审计总共2条", len(batch_release_logs) == 2,
          f"count={len(batch_release_logs)}")

    step("4.2", "审计记录操作者/结果/时间字段完整")
    all_complete = all(
        l["action"] and l["operator"] and l["target_type"] and l["result"] and l["created_at"]
        for l in batch_release_logs
    )
    check("审计记录核心字段完整", all_complete)

    section("5. 正常发布流程不受影响")

    step("5.1", "导入新批次并完成首次发布")
    batch2 = {
        "batch_name": "正常发布验证批次",
        "rule_id": rule_id,
        "imported_by": "admin",
        "suppliers": [
            {"supplier_code": "NORM-001", "supplier_name": "正常测试供应商",
             "metrics": {"pass_rate": 0.94, "defect_rate": 0.02, "on_time_rate": 0.92,
                         "lead_time_days": 15, "price_competitiveness": 84, "payment_terms_score": 70}}
        ]
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch2, headers=admin_h)
    batch2_id = resp.json()["id"]
    requests.post(f"{BASE_URL}/api/batches/{batch2_id}/calculate", headers=approver_h)
    approve2 = {"approved_by": "approver1", "approval_remark": "第二个批次正常发布"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch2_id}/release", json=approve2, headers=approver_h)
    check("第二个批次首次发布200", resp.status_code == 200)
    version2 = resp.json()["version"] if resp.status_code == 200 else ""

    step("5.2", "验证新版本发布成功，版本数+1")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    check("版本数量=2", len(resp.json()) == 2, f"count={len(resp.json())}")

    step("5.3", "验证活动版本正确")
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    check("活动版本=第二个批次版本", resp.json()["version"] == version2)

    step("5.4", "验证已有审计读取正常")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=release&operator=approver1", headers=admin_h)
    logs = resp.json()
    success_logs = [l for l in logs if l["result"] == "success"]
    check("release/success 审计>=2条", len(success_logs) >= 2, f"count={len(success_logs)}")

    step("5.5", "验证版本历史展示正常")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    releases = resp.json()
    check("版本历史2条", len(releases) == 2)
    versions = [r["version"] for r in releases]
    check("版本历史包含v1", version in versions)
    check("版本历史包含v2", version2 in versions)

    step("5.6", "导出结果与活动版本一致")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    export = resp.json()
    check("导出版本=活动版本", export["version"] == version2)
    check("导出审批人=approver1", export["approved_by"] == "approver1")

    section("6. 服务重启持久性快照")

    step("6.1", "记录快照")
    snapshot = {}
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    snapshot["active_version"] = resp.json()["version"]
    snapshot["approval_remark"] = resp.json()["approval_remark"]
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    snapshot["export_version"] = resp.json()["version"]
    snapshot["export_count"] = resp.json()["supplier_count"]
    resp = requests.get(f"{BASE_URL}/api/audit-logs", headers=admin_h)
    snapshot["audit_count"] = len(resp.json())
    dup_after = [l for l in resp.json() if l["result"] == "duplicate_rejected"]
    snapshot["dup_audit_count"] = len(dup_after)
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    snapshot["release_count"] = len(resp.json())
    print(f"    快照: {json.dumps(snapshot, ensure_ascii=False)}")
    print(f"    关键断言: duplicate_rejected审计={snapshot['dup_audit_count']} (必须=1)")

    section("测试结果汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


def verify_persistence(snapshot_json):
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    snapshot = json.loads(snapshot_json)

    section("服务重启后数据一致性验证")

    step("1", "活动版本一致")
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    active = resp.json()
    check("活动版本一致", active["version"] == snapshot["active_version"],
          f"before={snapshot['active_version']}, after={active['version']}")

    step("2", "审批备注一致")
    check("审批备注一致", active["approval_remark"] == snapshot["approval_remark"],
          f"before={snapshot['approval_remark']}, after={active['approval_remark']}")

    step("3", "导出结果一致")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    export = resp.json()
    check("导出版本一致", export["version"] == snapshot["export_version"])
    check("导出供应商数一致", export["supplier_count"] == snapshot["export_count"])

    step("4", "审计记录数量一致（含不膨胀验证）")
    resp = requests.get(f"{BASE_URL}/api/audit-logs", headers=admin_h)
    check("审计记录数量一致", len(resp.json()) == snapshot["audit_count"],
          f"before={snapshot['audit_count']}, after={len(resp.json())}")
    dup_after = [l for l in resp.json() if l["result"] == "duplicate_rejected"]
    check("duplicate_rejected审计仍=1", len(dup_after) == snapshot["dup_audit_count"],
          f"before={snapshot['dup_audit_count']}, after={len(dup_after)}")

    step("5", "版本历史数量一致")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    check("版本历史数量一致", len(resp.json()) == snapshot["release_count"],
          f"before={snapshot['release_count']}, after={len(resp.json())}")

    step("6", "审计记录字段完整")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=10", headers=admin_h)
    logs = resp.json()
    all_complete = all(l["action"] and l["operator"] and l["target_type"] and l["result"] and l["created_at"] for l in logs)
    check("审计记录核心字段完整", all_complete)

    section("持久化验证汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--verify-persist":
        verify_persistence(sys.argv[2])
    else:
        main()
