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


def main():
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    approver_h = {"X-Username": "approver1", "Content-Type": "application/json"}
    user_h = {"X-Username": "user1", "Content-Type": "application/json"}

    section("1. 基本候选设置与查询")

    step("1.1", "获取规则并预先导入计算所有需要的批次（避免候选设置后导入触发清空）")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    rule_id = resp.json()[0]["id"]

    batch_a_id, _ = import_and_calculate("候选回归-批次A", rule_id, "CND-001", "候选供应商A", admin_h)
    check("批次A导入计算成功", batch_a_id is not None)
    batch_b_id, _ = import_and_calculate("候选回归-批次B", rule_id, "CND-002", "候选供应商B", approver_h)
    check("批次B导入计算成功", batch_b_id is not None)
    batch_c_id, _ = import_and_calculate("候选回归-批次C", rule_id, "CND-003", "候选供应商C", admin_h)
    check("批次C导入计算成功", batch_c_id is not None)

    step("1.2", "设置批次A为候选发布")
    candidate_req = {
        "batch_id": batch_a_id,
        "change_description": "批次A的变更说明-首次候选",
        "expected_effective_time": "2026-07-01T10:00:00",
        "operation_remark": "操作备注-设置候选A",
        "set_by": "admin"
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req, headers=admin_h)
    check("设置候选返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
    if resp.status_code == 200:
        candidate_data = resp.json()["candidate"]
        change_log = resp.json()["change_log"]
        check("候选batch_id正确", candidate_data["batch_id"] == batch_a_id)
        check("候选is_current=True", candidate_data["is_current"] is True)
        check("候选change_description正确", candidate_data["change_description"] == "批次A的变更说明-首次候选")
        check("候选operation_remark正确", candidate_data["operation_remark"] == "操作备注-设置候选A")
        check("候选set_by=admin", candidate_data["set_by"] == "admin")
        check("变更日志new_candidate_id非空", change_log["new_candidate_id"] is not None)
        check("变更日志old_candidate_id为空(首次)", change_log["old_candidate_id"] is None)
        check("变更日志change_reason非空", len(change_log["change_reason"]) > 0)
        check("变更日志operated_by=admin", change_log["operated_by"] == "admin")
        candidate_a_id = candidate_data["id"]
    else:
        candidate_a_id = None

    step("1.3", "查询当前候选")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    check("查询当前候选返回200", resp.status_code == 200)
    if resp.status_code == 200:
        current = resp.json()
        check("当前候选batch_id=批次A", current["batch_id"] == batch_a_id)
        check("当前候选is_current=True", current["is_current"] is True)

    step("1.4", "查询最近候选变更原因")
    resp = requests.get(f"{BASE_URL}/api/candidate/change-log/latest", headers=admin_h)
    check("查询变更日志返回200", resp.status_code == 200)
    if resp.status_code == 200:
        latest_log = resp.json()
        check("变更原因非空", len(latest_log["change_reason"]) > 0)
        check("operated_by=admin", latest_log["operated_by"] == "admin")

    step("1.5", "验证set_candidate审计记录")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=set_candidate", headers=admin_h)
    logs = resp.json()
    set_logs = [l for l in logs if l["result"] == "success"]
    check("set_candidate审计存在", len(set_logs) >= 1, f"count={len(set_logs)}")
    if set_logs:
        l = set_logs[0]
        check("action=set_candidate", l["action"] == "set_candidate")
        check("target_type=candidate", l["target_type"] == "candidate")
        check("result=success", l["result"] == "success")
        check("detail包含变更说明", "变更说明" in l["detail"])

    section("2. 候选切换(顶替)与审计")

    step("2.1", "设置批次B为候选(应顶替批次A)")
    candidate_req_b = {
        "batch_id": batch_b_id,
        "change_description": "批次B的变更说明-顶替A",
        "expected_effective_time": "2026-07-15T10:00:00",
        "operation_remark": "操作备注-设置候选B顶替A",
        "set_by": "approver1"
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_b, headers=approver_h)
    check("设置候选B返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
    if resp.status_code == 200:
        new_candidate = resp.json()["candidate"]
        new_change_log = resp.json()["change_log"]
        check("新候选batch_id=批次B", new_candidate["batch_id"] == batch_b_id)
        check("新候选is_current=True", new_candidate["is_current"] is True)
        check("变更日志old_candidate_id=候选A的ID", new_change_log["old_candidate_id"] == candidate_a_id)
        check("变更日志new_candidate_id非空", new_change_log["new_candidate_id"] is not None)
        check("变更日志change_reason包含'顶替'", "顶替" in new_change_log["change_reason"])

    step("2.2", "验证当前候选已切换到批次B")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    check("当前候选返回200", resp.status_code == 200)
    if resp.status_code == 200:
        current = resp.json()
        check("当前候选batch_id=批次B", current["batch_id"] == batch_b_id)

    step("2.3", "验证candidate_replaced审计记录")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=candidate_replaced", headers=admin_h)
    logs = resp.json()
    replaced_logs = [l for l in logs if l["result"] == "replaced"]
    check("candidate_replaced审计存在", len(replaced_logs) >= 1, f"count={len(replaced_logs)}")
    if replaced_logs:
        l = replaced_logs[0]
        check("action=candidate_replaced", l["action"] == "candidate_replaced")
        check("result=replaced", l["result"] == "replaced")
        check("detail包含旧候选ID", str(candidate_a_id) in l["detail"])
        check("target_type=candidate", l["target_type"] == "candidate")

    step("2.4", "验证变更日志列表可查")
    resp = requests.get(f"{BASE_URL}/api/candidate/change-logs", headers=admin_h)
    check("变更日志列表返回200", resp.status_code == 200)
    change_logs = resp.json()
    check("变更日志>=2条", len(change_logs) >= 2, f"count={len(change_logs)}")

    section("3. 普通角色权限拒绝")

    step("3.1", "普通用户尝试设置候选（批次C已预先导入）")
    candidate_req_c = {
        "batch_id": batch_c_id,
        "change_description": "普通用户尝试设候选",
        "set_by": "user1"
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_c, headers=user_h)
    check("普通用户设置候选被拒403", resp.status_code == 403, f"actual={resp.status_code}")

    step("3.2", "普通用户尝试取消候选")
    resp = requests.post(f"{BASE_URL}/api/candidate/cancel?operated_by=user1&reason=非法取消", headers=user_h)
    check("普通用户取消候选被拒403", resp.status_code == 403, f"actual={resp.status_code}")

    step("3.3", "验证权限拒绝审计")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=permission_denied", headers=admin_h)
    logs = resp.json()
    user_denied = [l for l in logs if l["operator"] == "user1" and "candidate" in l.get("detail", "")]
    check("权限拒绝审计存在", len(user_denied) >= 1, f"count={len(user_denied)}")

    step("3.4", "验证当前候选未变(仍是批次B)")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    check("当前候选仍为批次B", resp.json()["batch_id"] == batch_b_id)

    section("4. 正式发布后候选清空")

    step("4.1", "发布批次B(当前候选批次)")
    approve_b = {"approved_by": "approver1", "approval_remark": "发布批次B-候选应清空", "release_note": "v-candidate-test"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_b_id}/release", json=approve_b, headers=approver_h)
    check("发布批次B返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
    v_b_version = resp.json()["version"] if resp.status_code == 200 else ""

    step("4.2", "验证候选已清空")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    check("候选已清空返回404", resp.status_code == 404, f"actual={resp.status_code}")

    step("4.3", "验证变更日志记录了发布清空")
    resp = requests.get(f"{BASE_URL}/api/candidate/change-log/latest", headers=admin_h)
    check("变更日志返回200", resp.status_code == 200)
    if resp.status_code == 200:
        latest = resp.json()
        check("变更原因包含'正式发布'", "正式发布" in latest["change_reason"], f"actual={latest['change_reason']}")
        check("new_candidate_id为空", latest["new_candidate_id"] is None)
        check("old_candidate_id非空", latest["old_candidate_id"] is not None)

    step("4.4", "验证导出中候选信息")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    check("导出返回200", resp.status_code == 200)
    if resp.status_code == 200:
        export = resp.json()
        check("导出版本正确", export["version"] == v_b_version)
        check("candidate_batch_id为空(无候选)", export.get("candidate_batch_id") is None)
        check("candidate_matches_active为空(无候选)", export.get("candidate_matches_active") is None)

    section("5. 回滚后候选与导出一致性")

    step("5.1", "先发布一个新版本(批次A2)以创建回滚场景")
    batch_a2_id, _ = import_and_calculate("候选回归-批次A2", rule_id, "CND-004", "候选供应商A2", admin_h)
    approve_a2 = {"approved_by": "admin", "approval_remark": "发布批次A2"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_a2_id}/release", json=approve_a2, headers=admin_h)
    check("发布批次A2返回200", resp.status_code == 200)
    v_a2_version = resp.json()["version"] if resp.status_code == 200 else ""

    step("5.2", "导入计算新批次并设为候选(与活动版本不同)")
    batch_f_id, _ = import_and_calculate("候选回归-批次F", rule_id, "CND-007", "候选供应商F", admin_h)
    check("批次F导入计算成功", batch_f_id is not None)
    candidate_req_f = {
        "batch_id": batch_f_id,
        "change_description": "回滚前设置候选",
        "set_by": "admin"
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_f, headers=admin_h)
    check("设置候选返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")

    step("5.3", "验证候选与活动版本不一致(导出)")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    check("导出返回200", resp.status_code == 200)
    if resp.status_code == 200:
        export = resp.json()
        check("candidate_batch_id非空", export.get("candidate_batch_id") is not None)
        check("candidate_matches_active=False(候选批次≠活动版本批次)", export.get("candidate_matches_active") is False,
              f"actual={export.get('candidate_matches_active')}")

    step("5.4", "回滚到批次B的版本")
    rollback_req = {"target_version": v_b_version, "reason": "回滚测试-验证候选清空", "operated_by": "admin"}
    resp = requests.post(f"{BASE_URL}/api/rollback", json=rollback_req, headers=admin_h)
    check("回滚返回200", resp.status_code == 200, f"actual={resp.status_code}")

    step("5.5", "验证回滚后候选已清空")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    check("回滚后候选已清空404", resp.status_code == 404, f"actual={resp.status_code}")

    step("5.6", "验证回滚后导出一致性")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    check("导出返回200", resp.status_code == 200)
    if resp.status_code == 200:
        export = resp.json()
        check("导出版本=回滚后活动版本", export["version"] == v_b_version)
        check("candidate_batch_id为空", export.get("candidate_batch_id") is None)
        check("candidate_matches_active为空", export.get("candidate_matches_active") is None)

    step("5.7", "验证回滚清空候选的变更日志")
    resp = requests.get(f"{BASE_URL}/api/candidate/change-log/latest", headers=admin_h)
    check("变更日志返回200", resp.status_code == 200)
    if resp.status_code == 200:
        latest = resp.json()
        check("变更原因包含'回滚'或'清空'", "回滚" in latest["change_reason"] or "清空" in latest["change_reason"],
              f"actual={latest['change_reason']}")
        check("new_candidate_id为空", latest["new_candidate_id"] is None)

    section("6. 取消候选功能")

    step("6.1", "设置一个新的候选")
    batch_d_id, _ = import_and_calculate("候选回归-批次D", rule_id, "CND-005", "候选供应商D", admin_h)
    candidate_req_d = {
        "batch_id": batch_d_id,
        "change_description": "测试取消候选",
        "set_by": "approver1"
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_d, headers=approver_h)
    check("设置候选D返回200", resp.status_code == 200)

    step("6.2", "管理员取消候选")
    resp = requests.post(f"{BASE_URL}/api/candidate/cancel?operated_by=admin&reason=测试取消", headers=admin_h)
    check("取消候选返回200", resp.status_code == 200, f"actual={resp.status_code}")
    if resp.status_code == 200:
        cancel_result = resp.json()
        check("取消后is_current=False", cancel_result["candidate"]["is_current"] is False)
        check("变更日志change_reason包含取消信息", "取消" in cancel_result["change_log"]["change_reason"])

    step("6.3", "验证候选已清空")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    check("取消后候选已清空404", resp.status_code == 404)

    step("6.4", "验证取消候选审计")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=cancel_candidate", headers=admin_h)
    logs = resp.json()
    cancel_logs = [l for l in logs if l["result"] == "success"]
    check("cancel_candidate审计存在", len(cancel_logs) >= 1, f"count={len(cancel_logs)}")

    step("6.5", "无候选时再次取消应返回404")
    resp = requests.post(f"{BASE_URL}/api/candidate/cancel?operated_by=admin&reason=重复取消", headers=admin_h)
    check("无候选时取消返回404", resp.status_code == 404, f"actual={resp.status_code}")

    section("7. 未计算批次不能设为候选")

    step("7.1", "导入但未计算的批次")
    batch_raw = {
        "batch_name": "候选回归-未计算批次",
        "rule_id": rule_id,
        "imported_by": "admin",
        "suppliers": [
            {"supplier_code": "CND-RAW", "supplier_name": "未计算供应商",
             "metrics": {"pass_rate": 0.9}}
        ]
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch_raw, headers=admin_h)
    check("导入返回200", resp.status_code == 200)
    raw_batch_id = resp.json()["id"]

    step("7.2", "尝试设置未计算批次为候选")
    candidate_req_raw = {
        "batch_id": raw_batch_id,
        "change_description": "未计算批次候选",
        "set_by": "admin"
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_raw, headers=admin_h)
    check("未计算批次设置候选被拒400", resp.status_code == 400, f"actual={resp.status_code}")
    check("错误信息包含'尚未完成计算'", "尚未完成计算" in resp.json().get("detail", ""))

    section("7.5 导入同规则新批次自动清空旧候选")

    step("7.5.1", "设置批次E为当前候选")
    batch_e_id, _ = import_and_calculate("候选回归-批次E-同规则测试", rule_id, "CND-SAME-001", "同规则供应商1", admin_h)
    candidate_req_e = {
        "batch_id": batch_e_id,
        "change_description": "同规则测试-旧候选",
        "set_by": "admin"
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_e, headers=admin_h)
    check("设置候选E返回200", resp.status_code == 200)
    old_candidate_id = resp.json()["candidate"]["id"]

    step("7.5.2", "验证当前候选是批次E")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    check("当前候选是批次E", resp.json()["batch_id"] == batch_e_id)

    step("7.5.3", "先发布一个旧批次获得活动版本（用于导出验证）")
    batch_release_id, _ = import_and_calculate("候选回归-发布批次", rule_id, "CND-REL-001", "发布供应商", admin_h)
    approve_release = {"approved_by": "admin", "approval_remark": "获得活动版本"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_release_id}/release", json=approve_release, headers=admin_h)
    check("发布成功", resp.status_code == 200)
    v_release_version = resp.json()["version"] if resp.status_code == 200 else ""

    step("7.5.4", "重新设置批次E为候选")
    candidate_req_e2 = {
        "batch_id": batch_e_id,
        "change_description": "重新设置候选E",
        "set_by": "admin"
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_e2, headers=admin_h)
    check("重新设置候选E返回200", resp.status_code == 200)

    step("7.5.5", "导入同规则新批次F（关键：应自动清空旧候选）")
    batch_f = {
        "batch_name": "候选回归-批次F-同规则新批次",
        "rule_id": rule_id,
        "imported_by": "admin",
        "suppliers": [
            {"supplier_code": "CND-SAME-002", "supplier_name": "同规则供应商2",
             "metrics": {"pass_rate": 0.95, "defect_rate": 0.02, "on_time_rate": 0.93,
                         "lead_time_days": 14, "price_competitiveness": 85, "payment_terms_score": 72}}
        ]
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch_f, headers=admin_h)
    check("批次F导入成功", resp.status_code == 200)
    batch_f_id = resp.json()["id"]

    step("7.5.6", "验证候选已被清空（返回404）")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    check("候选已清空返回404", resp.status_code == 404, f"actual={resp.status_code}")

    step("7.5.7", "验证导出中没有过期候选残留")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    check("导出返回200", resp.status_code == 200)
    if resp.status_code == 200:
        export = resp.json()
        check("导出版本正确", export["version"] == v_release_version)
        check("导出candidate_batch_id为空", export.get("candidate_batch_id") is None,
              f"actual={export.get('candidate_batch_id')}")
        check("导出candidate_matches_active为空", export.get("candidate_matches_active") is None,
              f"actual={export.get('candidate_matches_active')}")

    step("7.5.8", "验证变更日志记录了导入清空")
    resp = requests.get(f"{BASE_URL}/api/candidate/change-log/latest", headers=admin_h)
    check("变更日志返回200", resp.status_code == 200)
    if resp.status_code == 200:
        latest = resp.json()
        check("变更原因包含'导入同规则'或'自动失效'",
              "导入同规则" in latest["change_reason"] or "自动失效" in latest["change_reason"],
              f"actual={latest['change_reason']}")
        check("new_candidate_id为空", latest["new_candidate_id"] is None)
        check("old_candidate_id非空", latest["old_candidate_id"] is not None)

    step("7.5.9", "验证candidate_cleared_on_import审计记录")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=candidate_cleared_on_import", headers=admin_h)
    logs = resp.json()
    check("candidate_cleared_on_import审计存在", len(logs) >= 1, f"count={len(logs)}")
    if logs:
        l = logs[0]
        check("action=candidate_cleared_on_import", l["action"] == "candidate_cleared_on_import")
        check("target_type=candidate", l["target_type"] == "candidate")
        check("target_id=旧候选ID", l["target_id"] == str(old_candidate_id + 1) or l["target_id"] == str(old_candidate_id),
              f"target_id={l['target_id']}, old_id={old_candidate_id}")
        check("result=cleared", l["result"] == "cleared")
        check("detail包含变更原因", "导入同规则" in l["detail"] or "自动失效" in l["detail"])

    step("7.5.10", "不同规则的候选不应被清空")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    all_rules = resp.json()
    if len(all_rules) >= 2:
        rule2_id = all_rules[1]["id"]
        batch_diff_id, _ = import_and_calculate("候选回归-不同规则批次", rule2_id, "CND-DIFF-001", "不同规则供应商", admin_h)
        candidate_req_diff = {
            "batch_id": batch_diff_id,
            "change_description": "不同规则候选",
            "set_by": "admin"
        }
        resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_diff, headers=admin_h)
        check("设置不同规则候选返回200", resp.status_code == 200)
        batch_same_rule_id, _ = import_and_calculate("候选回归-规则1新批次", rule_id, "CND-SAME-003", "规则1新供应商", admin_h)
        resp = requests.post(f"{BASE_URL}/api/batches/import", json={
            "batch_name": "规则1新批次-验证不同规则",
            "rule_id": rule_id,
            "imported_by": "admin",
            "suppliers": [{"supplier_code": "CND-SAME-004", "supplier_name": "测试", "metrics": {"pass_rate": 0.9}}]
        }, headers=admin_h)
        check("导入规则1新批次返回200", resp.status_code == 200)
        resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
        check("不同规则候选仍保留", resp.status_code == 200 and resp.json()["batch_id"] == batch_diff_id)

    section("8. 候选与活动版本不一致时的导出")

    step("8.1", "设置一个非活动版本的候选")
    batch_e2_id, _ = import_and_calculate("候选回归-批次E2", rule_id, "CND-006", "候选供应商E2", admin_h)
    candidate_req_e2 = {
        "batch_id": batch_e2_id,
        "change_description": "非活动版本候选",
        "set_by": "admin"
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_e2, headers=admin_h)
    check("设置候选E2返回200", resp.status_code == 200)

    step("8.2", "导出中候选与活动版本不一致")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    check("导出返回200", resp.status_code == 200)
    if resp.status_code == 200:
        export = resp.json()
        check("candidate_batch_id非空", export.get("candidate_batch_id") is not None)
        check("candidate_matches_active=False", export.get("candidate_matches_active") is False,
              f"actual={export.get('candidate_matches_active')}")

    section("9. 审计字段完整性抽查")

    step("9.1", "抽查所有候选相关审计日志的6字段完整性")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=200", headers=admin_h)
    all_logs = resp.json()
    candidate_logs = [l for l in all_logs if l["action"] in (
        "set_candidate", "cancel_candidate", "candidate_replaced", "candidate_cleared_on_import"
    )]
    check("候选相关审计>=5条", len(candidate_logs) >= 5, f"count={len(candidate_logs)}")
    all_complete = all(
        l["action"] and l["operator"] and l["target_type"] and l["target_id"] is not None
        and l["result"] and l["created_at"]
        for l in candidate_logs
    )
    check("所有候选审计6字段完整", all_complete)

    section("10. 服务重启持久化快照")

    step("10.1", "记录关键数据快照")
    snapshot = {}
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    snapshot["active_version"] = resp.json()["version"]
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    export = resp.json()
    snapshot["export_version"] = export["version"]
    snapshot["candidate_batch_id"] = export.get("candidate_batch_id")
    snapshot["candidate_matches_active"] = export.get("candidate_matches_active")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    if resp.status_code == 200:
        snapshot["current_candidate_batch_id"] = resp.json()["batch_id"]
        snapshot["current_candidate_id"] = resp.json()["id"]
    else:
        snapshot["current_candidate_batch_id"] = None
        snapshot["current_candidate_id"] = None
    resp = requests.get(f"{BASE_URL}/api/candidate/change-log/latest", headers=admin_h)
    if resp.status_code == 200:
        snapshot["latest_change_reason"] = resp.json()["change_reason"]
        snapshot["latest_change_log_id"] = resp.json()["id"]
    else:
        snapshot["latest_change_reason"] = None
        snapshot["latest_change_log_id"] = None
    resp = requests.get(f"{BASE_URL}/api/candidate/change-logs", headers=admin_h)
    snapshot["change_logs_count"] = len(resp.json())
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=999", headers=admin_h)
    candidate_audit = [l for l in resp.json() if l["action"] in ("set_candidate", "cancel_candidate", "candidate_replaced")]
    snapshot["candidate_audit_count"] = len(candidate_audit)
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

    section("服务重启后候选数据持久性验证")

    step("1", "活动版本一致")
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    active = resp.json()
    check("活动版本一致", active["version"] == snapshot["active_version"],
          f"before={snapshot['active_version']}, after={active['version']}")

    step("2", "导出数据一致")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    export = resp.json()
    check("导出版本一致", export["version"] == snapshot["export_version"])
    check("candidate_batch_id一致", export.get("candidate_batch_id") == snapshot["candidate_batch_id"],
          f"before={snapshot['candidate_batch_id']}, after={export.get('candidate_batch_id')}")
    check("candidate_matches_active一致", export.get("candidate_matches_active") == snapshot["candidate_matches_active"],
          f"before={snapshot['candidate_matches_active']}, after={export.get('candidate_matches_active')}")

    step("3", "当前候选状态一致")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    if snapshot["current_candidate_batch_id"] is not None:
        check("候选返回200", resp.status_code == 200)
        if resp.status_code == 200:
            check("候选batch_id一致", resp.json()["batch_id"] == snapshot["current_candidate_batch_id"])
            check("候选id一致", resp.json()["id"] == snapshot["current_candidate_id"])
    else:
        check("候选已清空404", resp.status_code == 404)

    step("4", "最近变更日志一致")
    resp = requests.get(f"{BASE_URL}/api/candidate/change-log/latest", headers=admin_h)
    if snapshot["latest_change_reason"] is not None:
        check("变更日志返回200", resp.status_code == 200)
        if resp.status_code == 200:
            check("变更原因一致", resp.json()["change_reason"] == snapshot["latest_change_reason"])
            check("变更日志id一致", resp.json()["id"] == snapshot["latest_change_log_id"])

    step("5", "变更日志总数一致")
    resp = requests.get(f"{BASE_URL}/api/candidate/change-logs", headers=admin_h)
    check("变更日志总数一致", len(resp.json()) == snapshot["change_logs_count"],
          f"before={snapshot['change_logs_count']}, after={len(resp.json())}")

    step("6", "候选审计记录数量一致")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=999", headers=admin_h)
    candidate_audit = [l for l in resp.json() if l["action"] in ("set_candidate", "cancel_candidate", "candidate_replaced")]
    check("候选审计数量一致", len(candidate_audit) == snapshot["candidate_audit_count"],
          f"before={snapshot['candidate_audit_count']}, after={len(candidate_audit)}")

    step("7", "审计记录字段完整性")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=50", headers=admin_h)
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
