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


def count_releases():
    resp = requests.get(f"{BASE_URL}/api/releases?limit=999", headers=admin_h)
    return len(resp.json())


def count_batch_audit(batch_id, action="release", result=None):
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action={action}&limit=999", headers=admin_h)
    logs = resp.json()
    filtered = [l for l in logs if l["target_id"] == str(batch_id)]
    if result:
        filtered = [l for l in filtered if l["result"] == result]
    return len(filtered)


def count_version_audit(version, action="release", result=None):
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action={action}&limit=999", headers=admin_h)
    logs = resp.json()
    filtered = [l for l in logs if l["target_id"] == version]
    if result:
        filtered = [l for l in filtered if l["result"] == result]
    return len(filtered)


admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
approver_h = {"X-Username": "approver1", "Content-Type": "application/json"}


def main():
    section("0. 记录操作前基线")
    releases_before = count_releases()
    print(f"    操作前版本总数: {releases_before}")

    section("1. 首次发布 -> 审计正常写入")

    step("1.1", "获取规则并导入批次")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    rule_id = resp.json()[0]["id"]

    batch = {
        "batch_name": "重复发布回归批次",
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

    step("1.4", "验证版本数量增量=1")
    releases_after_first = count_releases()
    check("版本数量增量=1", releases_after_first - releases_before == 1,
          f"before={releases_before}, after={releases_after_first}")

    step("1.5", "验证该批次 release/success 审计增量=1")
    success_count = count_version_audit(version, result="success")
    check("该版本 success 审计=1", success_count == 1, f"count={success_count}")

    step("1.6", "验证该批次 duplicate_rejected 审计=0")
    dup_count = count_batch_audit(batch_id, result="duplicate_rejected")
    check("该批次 duplicate_rejected=0", dup_count == 0, f"count={dup_count}")

    section("2. 第2次重复发布 -> 写1条 duplicate_rejected 审计")

    step("2.1", "第2次发布请求")
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/release", json=approve, headers=approver_h)
    check("返回400", resp.status_code == 400, f"actual={resp.status_code}")
    check("错误信息含'已发布过'", "已发布过" in resp.json().get("detail", ""))

    step("2.2", "验证版本数量未增加")
    releases_after_dup2 = count_releases()
    check("版本数量不变", releases_after_dup2 == releases_after_first,
          f"before={releases_after_first}, after={releases_after_dup2}")

    step("2.3", "验证该批次 duplicate_rejected 审计=1")
    dup_count = count_batch_audit(batch_id, result="duplicate_rejected")
    check("duplicate_rejected=1", dup_count == 1, f"count={dup_count}")

    step("2.4", "验证 duplicate_rejected 审计字段完整")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=release&limit=999", headers=admin_h)
    dup_logs = [l for l in resp.json() if l["result"] == "duplicate_rejected" and l["target_id"] == str(batch_id)]
    if dup_logs:
        l = dup_logs[0]
        check("action=release", l["action"] == "release")
        check("operator=approver1", l["operator"] == "approver1")
        check("target_type=batch", l["target_type"] == "batch")
        check("target_id=批次ID", l["target_id"] == str(batch_id))
        check("result=duplicate_rejected", l["result"] == "duplicate_rejected")
        check("created_at非空", l["created_at"] is not None and len(l["created_at"]) > 0)
    else:
        check("duplicate_rejected记录存在", False, "未找到记录")

    section("3. 第3次重复发布 -> 不追加审计")

    step("3.1", "第3次发布请求")
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/release", json=approve, headers=approver_h)
    check("返回400", resp.status_code == 400, f"actual={resp.status_code}")

    step("3.2", "关键: duplicate_rejected 审计仍=1（不膨胀）")
    dup_count = count_batch_audit(batch_id, result="duplicate_rejected")
    check("duplicate_rejected仍=1", dup_count == 1, f"count={dup_count}")

    step("3.3", "success 审计仍=1")
    success_count = count_version_audit(version, result="success")
    check("success仍=1", success_count == 1, f"count={success_count}")

    step("3.4", "版本数量仍未增加")
    releases_after_dup3 = count_releases()
    check("版本数量不变", releases_after_dup3 == releases_after_first,
          f"before={releases_after_first}, after={releases_after_dup3}")

    step("3.5", "该批次 release 审计总共=2（1 success + 1 duplicate_rejected）")
    total_batch_release = count_batch_audit(batch_id) + count_version_audit(version)
    check("该批次 release 审计总计=2", total_batch_release == 2,
          f"batch_audit={count_batch_audit(batch_id)}, version_audit={count_version_audit(version)}")

    section("4. 正常发布不受影响")

    step("4.1", "导入新批次并首次发布")
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

    step("4.2", "版本数量增量=2（相比操作前）")
    releases_after_batch2 = count_releases()
    check("版本增量=2", releases_after_batch2 - releases_before == 2,
          f"before={releases_before}, after={releases_after_batch2}")

    step("4.3", "活动版本正确")
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    check("活动版本=第二个批次", resp.json()["version"] == version2)

    step("4.4", "版本历史展示正常")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    releases = resp.json()
    versions = [r["version"] for r in releases]
    check("版本历史包含v1", version in versions)
    check("版本历史包含v2", version2 in versions)

    step("4.5", "导出结果一致")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    export = resp.json()
    check("导出版本=活动版本", export["version"] == version2)

    step("4.6", "已有审计读取正常")
    v2_success = count_version_audit(version2, result="success")
    check("v2 success审计=1", v2_success == 1, f"count={v2_success}")

    step("4.7", "第一个批次的审计不受影响")
    v1_success = count_version_audit(version, result="success")
    v1_dup = count_batch_audit(batch_id, result="duplicate_rejected")
    check("v1 success审计仍=1", v1_success == 1, f"count={v1_success}")
    check("v1 duplicate_rejected仍=1", v1_dup == 1, f"count={v1_dup}")

    section("5. 持久化快照")

    step("5.1", "记录重启校验快照（增量式）")
    snapshot = {}
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    snapshot["active_version"] = resp.json()["version"]
    snapshot["approval_remark"] = resp.json()["approval_remark"]
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    snapshot["export_version"] = resp.json()["version"]
    snapshot["export_count"] = resp.json()["supplier_count"]
    snapshot["release_count"] = count_releases()
    snapshot["batch1_dup_audit"] = count_batch_audit(batch_id, result="duplicate_rejected")
    snapshot["batch1_success_audit"] = count_version_audit(version, result="success")
    snapshot["batch2_success_audit"] = count_version_audit(version2, result="success")
    snapshot["batch1_id"] = batch_id
    snapshot["version1"] = version
    snapshot["version2"] = version2
    print(f"    快照: {json.dumps(snapshot, ensure_ascii=False)}")
    print(f"    batch1_id={batch_id}, version1={version}, version2={version2}")

    section("测试结果汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


def verify_persistence(snapshot):
    snapshot = json.loads(snapshot)
    batch_id = snapshot["batch1_id"]
    version = snapshot["version1"]
    version2 = snapshot["version2"]

    section("服务重启后数据一致性验证（增量式）")

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

    step("4", "版本总数一致")
    check("版本总数一致", count_releases() == snapshot["release_count"],
          f"before={snapshot['release_count']}, after={count_releases()}")

    step("5", "batch1 duplicate_rejected审计仍=1（不膨胀）")
    dup_count = count_batch_audit(batch_id, result="duplicate_rejected")
    check("duplicate_rejected仍=1", dup_count == snapshot["batch1_dup_audit"],
          f"before={snapshot['batch1_dup_audit']}, after={dup_count}")

    step("6", "batch1 success审计仍=1")
    success_count = count_version_audit(version, result="success")
    check("success仍=1", success_count == snapshot["batch1_success_audit"],
          f"before={snapshot['batch1_success_audit']}, after={success_count}")

    step("7", "batch2 success审计仍=1")
    success2 = count_version_audit(version2, result="success")
    check("batch2 success仍=1", success2 == snapshot["batch2_success_audit"],
          f"before={snapshot['batch2_success_audit']}, after={success2}")

    step("8", "审计记录字段完整")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=10", headers=admin_h)
    logs = resp.json()
    all_complete = all(
        l["action"] and l["operator"] and l["target_type"] and l["result"] and l["created_at"]
        for l in logs
    )
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
