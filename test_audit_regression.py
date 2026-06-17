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

    section("1. 审批发布 -> 审计日志")
    step("1.1", "导入有效批次")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    rule_id = resp.json()[0]["id"]

    batch1 = {
        "batch_name": "审计测试-批次A",
        "rule_id": rule_id,
        "imported_by": "admin",
        "suppliers": [
            {"supplier_code": "AUD-001", "supplier_name": "审计供应商1",
             "metrics": {"pass_rate": 0.96, "defect_rate": 0.015, "on_time_rate": 0.94,
                         "lead_time_days": 13, "price_competitiveness": 86, "payment_terms_score": 73}}
        ]
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch1, headers=admin_h)
    check("批次导入返回200", resp.status_code == 200, f"actual={resp.status_code}")
    batch1_id = resp.json()["id"]

    step("1.2", "计算草稿")
    resp = requests.post(f"{BASE_URL}/api/batches/{batch1_id}/calculate", headers=approver_h)
    check("计算返回200", resp.status_code == 200, f"actual={resp.status_code}")

    step("1.3", "审批发布")
    approve = {"approved_by": "approver1", "approval_remark": "审计测试-同意发布", "release_note": "审计测试发布v1"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch1_id}/release", json=approve, headers=approver_h)
    check("发布返回200", resp.status_code == 200, f"actual={resp.status_code}")
    v1_version = resp.json()["version"] if resp.status_code == 200 else ""

    step("1.4", "验证审计日志中有 release/success 记录")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=release&operator=approver1", headers=admin_h)
    logs = resp.json()
    release_success_logs = [l for l in logs if l["action"] == "release" and l["result"] == "success"]
    check("release审计记录存在", len(release_success_logs) >= 1, f"count={len(release_success_logs)}")
    if release_success_logs:
        l = release_success_logs[0]
        check("操作类型=release", l["action"] == "release")
        check("操作者=approver1", l["operator"] == "approver1")
        check("目标类型=version", l["target_type"] == "version")
        check("目标ID=版本号", l["target_id"] == v1_version, f"actual={l['target_id']}")
        check("结果=success", l["result"] == "success")
        check("备注非空", len(l["detail"]) > 0)
        check("时间存在", l["created_at"] is not None)

    section("2. 回滚 -> 审计日志")

    step("2.1", "导入第二个批次并发布")
    batch2 = {
        "batch_name": "审计测试-批次B",
        "rule_id": rule_id,
        "imported_by": "admin",
        "suppliers": [
            {"supplier_code": "AUD-002", "supplier_name": "审计供应商2",
             "metrics": {"pass_rate": 0.93, "defect_rate": 0.02, "on_time_rate": 0.91,
                         "lead_time_days": 16, "price_competitiveness": 82, "payment_terms_score": 68}}
        ]
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch2, headers=admin_h)
    batch2_id = resp.json()["id"]
    requests.post(f"{BASE_URL}/api/batches/{batch2_id}/calculate", headers=approver_h)
    approve2 = {"approved_by": "approver1", "approval_remark": "v2发布", "release_note": "v2"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch2_id}/release", json=approve2, headers=approver_h)
    v2_version = resp.json()["version"] if resp.status_code == 200 else ""
    check("v2发布成功", resp.status_code == 200)

    step("2.2", "执行回滚到v1")
    rollback = {"target_version": v1_version, "reason": "审计测试回滚", "operated_by": "admin"}
    resp = requests.post(f"{BASE_URL}/api/rollback", json=rollback, headers=admin_h)
    check("回滚返回200", resp.status_code == 200, f"actual={resp.status_code}")

    step("2.3", "验证审计日志中有 rollback/success 记录")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=rollback", headers=admin_h)
    logs = resp.json()
    rollback_logs = [l for l in logs if l["result"] == "success"]
    check("rollback审计记录存在", len(rollback_logs) >= 1, f"count={len(rollback_logs)}")
    if rollback_logs:
        l = rollback_logs[0]
        check("操作类型=rollback", l["action"] == "rollback")
        check("操作者=admin", l["operator"] == "admin")
        check("结果=success", l["result"] == "success")
        check("备注包含回滚信息", "回滚" in l["detail"] or "rollback" in l["detail"].lower(), f"detail={l['detail']}")

    step("2.4", "确认回滚后活动版本是v1")
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    active_after_rollback = resp.json()
    check("活动版本=v1", active_after_rollback["version"] == v1_version,
          f"expected={v1_version}, actual={active_after_rollback['version']}")

    section("3. 缺少供应商编号导入失败 -> 审计日志")

    step("3.1", "导入缺少编号的批次")
    active_before_bad = active_after_rollback["version"]
    bad_batch = {
        "batch_name": "审计测试-无效批次",
        "rule_id": rule_id,
        "imported_by": "admin",
        "suppliers": [
            {"supplier_code": "", "supplier_name": "坏供应商", "metrics": {"pass_rate": 0.9}}
        ]
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=bad_batch, headers=admin_h)
    check("导入被拒400", resp.status_code == 400, f"actual={resp.status_code}")

    step("3.2", "验证审计日志中有 import_batch/rejected 记录")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=import_batch", headers=admin_h)
    logs = resp.json()
    import_rejected = [l for l in logs if l["result"] == "rejected"]
    check("import rejected审计记录存在", len(import_rejected) >= 1, f"count={len(import_rejected)}")
    if import_rejected:
        l = import_rejected[0]
        check("操作类型=import_batch", l["action"] == "import_batch")
        check("结果=rejected", l["result"] == "rejected")
        check("备注包含缺少编号", "缺少供应商编号" in l["detail"], f"detail={l['detail']}")

    step("3.3", "验证已发布版本未被影响")
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    active_after_bad = resp.json()
    check("活动版本未变", active_after_bad["version"] == active_before_bad,
          f"before={active_before_bad}, after={active_after_bad['version']}")
    resp = requests.get(f"{BASE_URL}/api/batches", headers=admin_h)
    batches = resp.json()
    bad_batches = [b for b in batches if "无效" in b["batch_name"]]
    check("无效批次未被保存", len(bad_batches) == 0, f"count={len(bad_batches)}")

    section("4. 普通角色发布被拒 -> 审计日志")

    step("4.1", "导入一个有效批次（普通用户）")
    batch3 = {
        "batch_name": "审计测试-权限测试批次",
        "rule_id": rule_id,
        "imported_by": "user1",
        "suppliers": [
            {"supplier_code": "AUD-003", "supplier_name": "权限测试供应商",
             "metrics": {"pass_rate": 0.92, "defect_rate": 0.025, "on_time_rate": 0.90,
                         "lead_time_days": 17, "price_competitiveness": 80, "payment_terms_score": 65}}
        ]
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch3, headers=user_h)
    batch3_id = resp.json()["id"]
    requests.post(f"{BASE_URL}/api/batches/{batch3_id}/calculate", headers=admin_h)

    step("4.2", "普通用户尝试发布")
    approve_user = {"approved_by": "user1", "approval_remark": "普通用户发布尝试"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch3_id}/release", json=approve_user, headers=user_h)
    check("普通用户发布被拒403", resp.status_code == 403, f"actual={resp.status_code}")

    step("4.3", "验证审计日志中有 permission_denied 记录")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=permission_denied", headers=admin_h)
    logs = resp.json()
    perm_denied_logs = [l for l in logs if l["operator"] == "user1"]
    check("permission_denied审计记录存在", len(perm_denied_logs) >= 1, f"count={len(perm_denied_logs)}")
    if perm_denied_logs:
        l = perm_denied_logs[0]
        check("操作类型=permission_denied", l["action"] == "permission_denied")
        check("操作者=user1", l["operator"] == "user1")
        check("结果=denied", l["result"] == "denied")
        check("备注非空", len(l["detail"]) > 0)

    section("5. 同一草稿重复发布 -> 不重复写版本和审计")

    step("5.1", "管理员发布batch3")
    approve3 = {"approved_by": "admin", "approval_remark": "正式发布batch3"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch3_id}/release", json=approve3, headers=admin_h)
    check("batch3发布成功", resp.status_code == 200)
    v3_version = resp.json()["version"] if resp.status_code == 200 else ""
    version_count_before = len(requests.get(f"{BASE_URL}/api/releases", headers=admin_h).json())

    step("5.2", "重复发布batch3")
    resp = requests.post(f"{BASE_URL}/api/batches/{batch3_id}/release", json=approve3, headers=admin_h)
    check("重复发布被拒400", resp.status_code == 400, f"actual={resp.status_code}")

    step("5.3", "验证版本数量未增加")
    version_count_after = len(requests.get(f"{BASE_URL}/api/releases", headers=admin_h).json())
    check("版本数量不变", version_count_after == version_count_before,
          f"before={version_count_before}, after={version_count_after}")

    step("5.4", "验证审计日志中有 duplicate_rejected 记录，且只有1条")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=release", headers=admin_h)
    logs = resp.json()
    dup_logs = [l for l in logs if l["result"] == "duplicate_rejected" and l["target_id"] == str(batch3_id)]
    check("duplicate_rejected审计记录存在", len(dup_logs) >= 1, f"count={len(dup_logs)}")
    check("duplicate_rejected只有1条", len(dup_logs) == 1, f"count={len(dup_logs)}")

    step("5.5", "验证 release/success 审计中 target_id=v3_version 的只有1条")
    release_success_for_v3 = [l for l in logs if l["result"] == "success" and l["target_id"] == v3_version]
    check("release success审计只1条(按version匹配)", len(release_success_for_v3) == 1,
          f"count={len(release_success_for_v3)}")

    section("6. 现有版本历史和权限行为未改坏")

    step("6.1", "版本历史正常可查")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    check("版本历史返回200", resp.status_code == 200)
    releases = resp.json()
    check("版本数量>=3", len(releases) >= 3, f"count={len(releases)}")

    step("6.2", "活动版本正确（发布batch3后应为v3）")
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    check("活动版本返回200", resp.status_code == 200)
    active = resp.json()
    check("活动版本为v3", active["version"] == v3_version,
          f"expected={v3_version}, actual={active['version']}")

    step("6.3", "审批备注完整保留")
    check("审批备注=正式发布batch3", active["approval_remark"] == "正式发布batch3",
          f"actual={active['approval_remark']}")

    step("6.4", "导出结果与活动版本一致")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    check("导出返回200", resp.status_code == 200)
    export = resp.json()
    check("导出版本=活动版本", export["version"] == active["version"])
    check("导出审批人一致", export["approved_by"] == active["approved_by"])

    step("6.5", "回滚记录正常")
    resp = requests.get(f"{BASE_URL}/api/rollback-records", headers=admin_h)
    check("回滚记录返回200", resp.status_code == 200)
    check("回滚记录>=1", len(resp.json()) >= 1)

    step("6.6", "普通用户仍不能回滚")
    rollback_bad = {"target_version": v1_version, "reason": "非法尝试", "operated_by": "user1"}
    resp = requests.post(f"{BASE_URL}/api/rollback", json=rollback_bad, headers=user_h)
    check("普通用户回滚被拒403", resp.status_code == 403, f"actual={resp.status_code}")

    section("7. 审计日志查询API功能验证")

    step("7.1", "按操作类型过滤")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=release", headers=admin_h)
    check("action过滤返回200", resp.status_code == 200)
    all_release = all(l["action"] == "release" for l in resp.json())
    check("全部记录action=release", all_release)

    step("7.2", "按操作者过滤")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?operator=user1", headers=admin_h)
    check("operator过滤返回200", resp.status_code == 200)
    all_user1 = all(l["operator"] == "user1" for l in resp.json())
    check("全部记录operator=user1", all_user1)

    step("7.3", "审计记录6字段完整性抽样（target_id允许空串表示无目标）")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=50", headers=admin_h)
    logs = resp.json()
    all_complete = True
    for l in logs:
        if not (l["action"] and l["operator"] and l["target_type"] and l["result"] and l["created_at"]):
            all_complete = False
            break
    check("所有审计记录核心5字段完整(action/operator/target_type/result/created_at)", all_complete)

    section("8. 服务重启数据持久性")

    step("8.1", "记录重启前关键数据快照")
    snapshot = {}
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    snapshot["active_version"] = resp.json()["version"]
    snapshot["approval_remark"] = resp.json()["approval_remark"]
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    snapshot["export_version"] = resp.json()["version"]
    snapshot["export_count"] = resp.json()["supplier_count"]
    resp = requests.get(f"{BASE_URL}/api/audit-logs", headers=admin_h)
    snapshot["audit_count"] = len(resp.json())
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    snapshot["release_count"] = len(resp.json())
    resp = requests.get(f"{BASE_URL}/api/rollback-records", headers=admin_h)
    snapshot["rollback_count"] = len(resp.json())
    print(f"    快照: {json.dumps(snapshot, ensure_ascii=False)}")
    print()
    print("    >>> 请手动重启服务后再次运行本脚本的 --verify-persist 模式 <<<")
    print(f"    命令: python {sys.argv[0]} --verify-persist \"{json.dumps(snapshot, ensure_ascii=False)}\"")

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

    step("4", "审计记录数量一致")
    resp = requests.get(f"{BASE_URL}/api/audit-logs", headers=admin_h)
    check("审计记录数量一致", len(resp.json()) == snapshot["audit_count"],
          f"before={snapshot['audit_count']}, after={len(resp.json())}")

    step("5", "版本历史数量一致")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    check("版本历史数量一致", len(resp.json()) == snapshot["release_count"],
          f"before={snapshot['release_count']}, after={len(resp.json())}")

    step("6", "回滚记录数量一致")
    resp = requests.get(f"{BASE_URL}/api/rollback-records", headers=admin_h)
    check("回滚记录数量一致", len(resp.json()) == snapshot["rollback_count"],
          f"before={snapshot['rollback_count']}, after={len(resp.json())}")

    step("7", "审计记录抽样字段完整")
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
