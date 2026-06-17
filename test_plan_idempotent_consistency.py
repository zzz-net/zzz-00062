import requests
import json
import sys
import time
from datetime import datetime, timedelta, timezone

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

    # ----------------------------------------------------------------
    # SECTION 1: Duplicate trigger idempotent - scheduled release won't double-execute
    # ----------------------------------------------------------------
    section("1. Duplicate trigger idempotent - scheduled release won't double-execute")

    step("1.1", "Get rule_id")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    assert resp.status_code == 200, f"无法获取规则: {resp.status_code} {resp.text}"
    rule_id = resp.json()[0]["id"]
    print(f"    rule_id = {rule_id}")

    step("1.2", "Import and calculate batch IDEMP-A")
    batch_idemp_a_id, _ = import_and_calculate(
        "幂等-批次IDEMP-A(重复触发测试)", rule_id, "IDEMP-A", "幂等供应商A", admin_h
    )
    check("批次IDEMP-A导入计算成功", batch_idemp_a_id is not None)

    step("1.3", "Create scheduled release for IDEMP-A 15 seconds in future")
    sched_time_idemp = iso_future(15)
    sched_req_idemp = {
        "batch_id": batch_idemp_a_id,
        "scheduled_time": sched_time_idemp,
        "change_description": "IDEMP-A幂等测试预约发布",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_idemp, headers=admin_h)
    check("创建IDEMP-A预约返回200", resp.status_code == 200,
          f"actual={resp.status_code}, body={resp.text}")
    sched_idemp_id = None
    if resp.status_code == 200:
        sched_idemp_id = resp.json()["scheduled_release"]["id"]
        print(f"    sched_idemp_id = {sched_idemp_id}, scheduled_time = {sched_time_idemp}")

    step("1.4", "Wait for it to execute (max 30s)")
    if sched_idemp_id:
        def idemp_executed():
            r = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_idemp_id}", headers=admin_h)
            return r.status_code == 200 and r.json()["status"] == "executed"
        ok = wait_until(idemp_executed, timeout_sec=30, poll_sec=2,
                        label="等待IDEMP-A预约自动执行")
        check("IDEMP-A预约到点自动执行", ok)

    step("1.5", "Verify only 1 release version for this batch (no duplicate)")
    if batch_idemp_a_id:
        resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
        versions = [r for r in resp.json() if r["batch_id"] == batch_idemp_a_id]
        check("批次IDEMP-A只生成1个发布版本(无重复)", len(versions) == 1,
              f"count={len(versions)}")

    step("1.6", "Call the release endpoint for same batch - should get 400 '已发布过'")
    if batch_idemp_a_id:
        approve_dup = {"approved_by": "admin", "approval_remark": "重复发布尝试", "release_note": "dup-test"}
        resp = requests.post(f"{BASE_URL}/api/batches/{batch_idemp_a_id}/release",
                             json=approve_dup, headers=admin_h)
        check("重复发布返回400", resp.status_code == 400, f"actual={resp.status_code}")
        check("错误信息含'已发布过'", "已发布过" in resp.json().get("detail", ""),
              f"actual={resp.json().get('detail', '')}")

    step("1.7", "Verify duplicate_rejected audit exists")
    if batch_idemp_a_id:
        resp = requests.get(f"{BASE_URL}/api/audit-logs?action=release&limit=200", headers=admin_h)
        logs = resp.json()
        dup_logs = [l for l in logs
                    if l["result"] == "duplicate_rejected" and l["target_id"] == str(batch_idemp_a_id)]
        check("duplicate_rejected审计存在", len(dup_logs) >= 1, f"count={len(dup_logs)}")

    step("1.8", "Verify the release plan status=executed, cancelled_at is None, source_detail is not empty")
    if batch_idemp_a_id:
        resp = requests.get(f"{BASE_URL}/api/release-plans",
                            params={"rule_id": rule_id, "status": "executed", "batch_id": batch_idemp_a_id},
                            headers=admin_h)
        plans = resp.json()
        if plans:
            plan = plans[0]
            check("plan status=executed", plan["status"] == "executed",
                  f"actual={plan['status']}")
            check("plan cancelled_at is None", plan.get("cancelled_at") is None,
                  f"actual={plan.get('cancelled_at')}")
            check("plan source_detail非空", len(plan.get("source_detail") or "") > 0,
                  f"actual={plan.get('source_detail')}")
        else:
            check("找到IDEMP-A的executed计划", False, "无匹配计划")

    # ----------------------------------------------------------------
    # SECTION 2: Detail status consistency - no contradictory fields
    # ----------------------------------------------------------------
    section("2. Detail status consistency - no contradictory fields")

    step("2.1", "Import and calculate batch CONSIST-B")
    batch_consist_b_id, _ = import_and_calculate(
        "一致性-批次CONSIST-B(状态字段一致性)", rule_id, "CONSIST-B", "一致性供应商B", admin_h
    )
    check("批次CONSIST-B导入计算成功", batch_consist_b_id is not None)

    step("2.2", "Set as candidate (creates queued plan)")
    candidate_req_b = {
        "batch_id": batch_consist_b_id,
        "change_description": "一致性测试-批次B候选",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_b, headers=admin_h)
    check("设置候选B返回200", resp.status_code == 200, f"body={resp.text[:200]}")
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "status": "queued", "batch_id": batch_consist_b_id},
                        headers=admin_h)
    plans_b = resp.json()
    plan_b_id = None
    if plans_b:
        plan_b_id = plans_b[0]["id"]
        print(f"    plan_b_id = {plan_b_id}")

    step("2.3", "Import another batch with same rule (should supersede the queued plan)")
    batch_consist_b2_id, _ = import_and_calculate(
        "一致性-批次CONSIST-B2(顶替B)", rule_id, "CONSIST-B2", "一致性供应商B2", admin_h
    )
    check("批次CONSIST-B2导入计算成功", batch_consist_b2_id is not None)
    candidate_req_b2 = {
        "batch_id": batch_consist_b2_id,
        "change_description": "一致性测试-批次B2顶替B",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_b2, headers=admin_h)
    check("设置候选B2返回200", resp.status_code == 200)

    step("2.4", "Get the superseded plan detail, verify consistency")
    if plan_b_id:
        resp = requests.get(f"{BASE_URL}/api/release-plans/{plan_b_id}", headers=admin_h)
        check("superseded计划详情200", resp.status_code == 200)
        if resp.status_code == 200:
            plan = resp.json()
            check("superseded status=superseded", plan["status"] == "superseded",
                  f"actual={plan['status']}")
            check("superseded_at非空", plan.get("superseded_at") is not None,
                  f"actual={plan.get('superseded_at')}")
            check("superseded_by_plan_id非空", plan.get("superseded_by_plan_id") is not None,
                  f"actual={plan.get('superseded_by_plan_id')}")
            check("superseded executed_at is None", plan.get("executed_at") is None,
                  f"actual={plan.get('executed_at')}")
            check("superseded cancelled_at is None", plan.get("cancelled_at") is None,
                  f"actual={plan.get('cancelled_at')}")
            check("superseded expired_at is None", plan.get("expired_at") is None,
                  f"actual={plan.get('expired_at')}")
            check("superseded conflict_reason非空", len(plan.get("conflict_reason") or "") > 0,
                  f"actual={plan.get('conflict_reason')}")
            check("superseded source_detail非空", len(plan.get("source_detail") or "") > 0,
                  f"actual={plan.get('source_detail')}")
    else:
        check("plan_b_id存在可查询", False, "plan_b_id为空")

    step("2.5", "Get the executed plan detail for B2, verify consistency")
    if batch_consist_b2_id:
        approve_b2 = {"approved_by": "admin", "approval_remark": "一致性测试-发布B2", "release_note": "consist-b2"}
        resp = requests.post(f"{BASE_URL}/api/batches/{batch_consist_b2_id}/release",
                             json=approve_b2, headers=admin_h)
        check("发布B2返回200", resp.status_code == 200, f"body={resp.text[:200]}")
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "status": "executed", "batch_id": batch_consist_b2_id},
                        headers=admin_h)
    exec_plans = resp.json()
    if exec_plans:
        plan = exec_plans[0]
        check("executed status=executed", plan["status"] == "executed",
              f"actual={plan['status']}")
        check("executed executed_at非空", plan.get("executed_at") is not None,
              f"actual={plan.get('executed_at')}")
        check("executed cancelled_at is None", plan.get("cancelled_at") is None,
              f"actual={plan.get('cancelled_at')}")
        check("executed superseded_at is None", plan.get("superseded_at") is None,
              f"actual={plan.get('superseded_at')}")
        check("executed expired_at is None", plan.get("expired_at") is None,
              f"actual={plan.get('expired_at')}")
    else:
        check("找到B2的executed计划", False, "无匹配计划")

    step("2.6", "Create a scheduled release, then manually cancel it")
    batch_consist_c_id, _ = import_and_calculate(
        "一致性-批次CONSIST-C(取消测试)", rule_id, "CONSIST-C", "一致性供应商C", admin_h
    )
    check("批次CONSIST-C导入计算成功", batch_consist_c_id is not None)
    sched_time_c = iso_future(120)
    sched_req_c = {
        "batch_id": batch_consist_c_id,
        "scheduled_time": sched_time_c,
        "change_description": "一致性测试-C预约(后手动取消)",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_c, headers=admin_h)
    check("创建CONSIST-C预约200", resp.status_code == 200, f"body={resp.text[:200]}")
    sched_c_id = None
    if resp.status_code == 200:
        sched_c_id = resp.json()["scheduled_release"]["id"]
    if sched_c_id:
        resp = requests.post(
            f"{BASE_URL}/api/scheduled-releases/{sched_c_id}/cancel?operated_by=admin&reason=一致性手动取消测试",
            headers=admin_h,
        )
        check("取消预约C返回200", resp.status_code == 200,
              f"actual={resp.status_code}, body={resp.text[:200]}")

    step("2.7", "Get the cancelled plan detail, verify consistency")
    if batch_consist_c_id:
        resp = requests.get(f"{BASE_URL}/api/release-plans",
                            params={"rule_id": rule_id, "status": "cancelled", "batch_id": batch_consist_c_id},
                            headers=admin_h)
        cancelled_plans = resp.json()
        if cancelled_plans:
            plan = cancelled_plans[0]
            check("cancelled status=cancelled", plan["status"] == "cancelled",
                  f"actual={plan['status']}")
            check("cancelled cancelled_at非空", plan.get("cancelled_at") is not None,
                  f"actual={plan.get('cancelled_at')}")
            check("cancelled executed_at is None", plan.get("executed_at") is None,
                  f"actual={plan.get('executed_at')}")
            check("cancelled superseded_at is None", plan.get("superseded_at") is None,
                  f"actual={plan.get('superseded_at')}")
            check("cancelled expired_at is None", plan.get("expired_at") is None,
                  f"actual={plan.get('expired_at')}")
        else:
            check("找到C的cancelled计划", False, "无匹配计划")

    # ----------------------------------------------------------------
    # SECTION 3: Scheduled release detail response - executed records don't show cancel info
    # ----------------------------------------------------------------
    section("3. Scheduled release detail response - executed records don't show cancel info")

    step("3.1", "Import and calculate batch DETAIL-C")
    batch_detail_c_id, _ = import_and_calculate(
        "详情-批次DETAIL-C(执行后不显取消)", rule_id, "DETAIL-C", "详情供应商C", admin_h
    )
    check("批次DETAIL-C导入计算成功", batch_detail_c_id is not None)

    step("3.2", "Create scheduled release for DETAIL-C 15 seconds in future")
    sched_time_dc = iso_future(15)
    sched_req_dc = {
        "batch_id": batch_detail_c_id,
        "scheduled_time": sched_time_dc,
        "change_description": "详情测试-DETAIL-C预约发布",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_dc, headers=admin_h)
    check("创建DETAIL-C预约返回200", resp.status_code == 200,
          f"actual={resp.status_code}, body={resp.text}")
    sched_dc_id = None
    if resp.status_code == 200:
        sched_dc_id = resp.json()["scheduled_release"]["id"]

    step("3.3", "Wait for execution")
    if sched_dc_id:
        def dc_executed():
            r = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_dc_id}", headers=admin_h)
            return r.status_code == 200 and r.json()["status"] == "executed"
        ok = wait_until(dc_executed, timeout_sec=30, poll_sec=2,
                        label="等待DETAIL-C预约自动执行")
        check("DETAIL-C预约到点自动执行", ok)

    step("3.4", "Get scheduled release detail, verify executed fields clean")
    if sched_dc_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_dc_id}", headers=admin_h)
        detail = resp.json()
        check("status=executed", detail["status"] == "executed",
              f"actual={detail['status']}")
        check("cancel_reason为空(executed状态)", detail.get("cancel_reason") == "",
              f"actual={detail.get('cancel_reason')}")
        check("cancelled_at=None(executed状态)", detail.get("cancelled_at") is None,
              f"actual={detail.get('cancelled_at')}")
        check("cancelled_by=None(executed状态)", detail.get("cancelled_by") is None,
              f"actual={detail.get('cancelled_by')}")
        check("executed_at非空", detail.get("executed_at") is not None,
              f"actual={detail.get('executed_at')}")
        check("release_version非空", detail.get("release_version") is not None,
              f"actual={detail.get('release_version')}")

    # ----------------------------------------------------------------
    # SECTION 4: Import conflict + source log audit trail
    # ----------------------------------------------------------------
    section("4. Import conflict + source log audit trail")

    step("4.1", "Set current candidate (creates queued plan)")
    batch_audit_d_id, _ = import_and_calculate(
        "审计-批次AUDIT-D(导入冲突审计)", rule_id, "AUDIT-D", "审计供应商D", admin_h
    )
    check("批次AUDIT-D导入计算成功", batch_audit_d_id is not None)
    candidate_req_d = {
        "batch_id": batch_audit_d_id,
        "change_description": "审计测试-批次D候选",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_d, headers=admin_h)
    check("设置候选D返回200", resp.status_code == 200)
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "status": "queued", "batch_id": batch_audit_d_id},
                        headers=admin_h)
    plans_d = resp.json()
    plan_d_id = plans_d[0]["id"] if plans_d else None

    step("4.2", "Import new batch with same rule_id")
    batch_audit_e_id, _ = import_and_calculate(
        "审计-批次AUDIT-E(新导入冲突D)", rule_id, "AUDIT-E", "审计供应商E", admin_h
    )
    check("批次AUDIT-E导入计算成功", batch_audit_e_id is not None)

    step("4.3", "Verify import_batch audit contains conflict info")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=import_batch&limit=200", headers=admin_h)
    logs = resp.json()
    conflict_import_logs = [l for l in logs
                            if l["target_id"] == str(batch_audit_e_id)
                            and ("冲突" in (l.get("detail") or "") or "conflict" in (l.get("detail") or "").lower())]
    check("import_batch审计含冲突信息", len(conflict_import_logs) >= 1,
          f"count={len(conflict_import_logs)}")

    step("4.4", "Verify the superseded plan has source_detail set")
    if plan_d_id:
        resp = requests.get(f"{BASE_URL}/api/release-plans/{plan_d_id}", headers=admin_h)
        check("superseded计划详情200", resp.status_code == 200)
        if resp.status_code == 200:
            plan = resp.json()
            check("superseded计划source_detail非空", len(plan.get("source_detail") or "") > 0,
                  f"actual={plan.get('source_detail')}")
            check("superseded计划conflict_reason非空", len(plan.get("conflict_reason") or "") > 0,
                  f"actual={plan.get('conflict_reason')}")

    # ----------------------------------------------------------------
    # SECTION 5: Export permission denial edge case
    # ----------------------------------------------------------------
    section("5. Export permission denial edge case")

    step("5.1", "A user with 'user' role tries to access release-plan-configs (admin only)")
    resp = requests.get(f"{BASE_URL}/api/release-plan-configs", headers=user_h)
    check("普通用户访问configs被403", resp.status_code == 403,
          f"actual={resp.status_code}")

    step("5.2", "Verify 403 response")
    check("403响应body含detail", "detail" in resp.json() or resp.text,
          f"body={resp.text[:200]}")

    step("5.3", "Verify permission_denied audit log exists with the endpoint path")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=permission_denied&limit=200", headers=admin_h)
    logs = resp.json()
    perm_denied_logs = [l for l in logs if l["operator"] == "user1"]
    check("permission_denied审计存在(operator=user1)", len(perm_denied_logs) >= 1,
          f"count={len(perm_denied_logs)}")
    config_denied = [l for l in perm_denied_logs
                     if "release-plan-configs" in (l.get("detail") or "")
                     or "release-plan-configs" in (l.get("target_id") or "")]
    check("审计含release-plan-configs路径", len(config_denied) >= 1,
          f"config_denied={len(config_denied)}, all_details={[(l.get('detail','')[:80]) for l in perm_denied_logs[:5]]}")

    step("5.4", "Same user tries PUT to update config - verify 403")
    resp = requests.put(
        f"{BASE_URL}/api/release-plan-configs",
        params={"config_key": "allow_early_window_seconds", "config_value": "999"},
        headers=user_h,
    )
    check("普通用户修改config被403", resp.status_code == 403, f"actual={resp.status_code}")

    step("5.5", "Verify another permission_denied audit entry")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=permission_denied&limit=200", headers=admin_h)
    logs = resp.json()
    perm_denied_after = [l for l in logs if l["operator"] == "user1"]
    check("permission_denied审计条数增加", len(perm_denied_after) >= 2,
          f"count={len(perm_denied_after)}")

    step("5.6", "User tries POST /api/release-plans/trigger-expire - verify 403")
    resp = requests.post(f"{BASE_URL}/api/release-plans/trigger-expire", headers=user_h)
    check("普通用户trigger-expire被403", resp.status_code == 403, f"actual={resp.status_code}")

    step("5.7", "Check all permission_denied audits have complete fields")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=permission_denied&limit=200", headers=admin_h)
    logs = resp.json()
    user_perm_logs = [l for l in logs if l["operator"] == "user1"]
    if user_perm_logs:
        all_complete = all(
            l.get("action") and l.get("operator") and l.get("target_type")
            and l.get("result") and l.get("created_at") and l.get("detail")
            for l in user_perm_logs
        )
        check("所有permission_denied审计字段完整", all_complete)
    else:
        check("permission_denied审计记录存在", False, "无user1的权限拒绝记录")

    # ----------------------------------------------------------------
    # SECTION 6: Conflict check before manual release logs in audit
    # ----------------------------------------------------------------
    section("6. Conflict check before manual release logs in audit")

    step("6.1", "Import and calculate batch CONFLICT-D, set as candidate (creates scheduled plan)")
    batch_conflict_d_id, _ = import_and_calculate(
        "冲突-批次CONFLICT-D(手动发布冲突)", rule_id, "CONFLICT-D", "冲突供应商D", admin_h
    )
    check("批次CONFLICT-D导入计算成功", batch_conflict_d_id is not None)
    candidate_req_cd = {
        "batch_id": batch_conflict_d_id,
        "change_description": "冲突测试-批次D候选",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_cd, headers=admin_h)
    check("设置候选CONFLICT-D返回200", resp.status_code == 200)

    step("6.2", "Create scheduled release for CONFLICT-D far in future")
    sched_time_cd = iso_future(300)
    sched_req_cd = {
        "batch_id": batch_conflict_d_id,
        "scheduled_time": sched_time_cd,
        "change_description": "冲突测试-CONFLICT-D远期预约",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_cd, headers=admin_h)
    check("创建CONFLICT-D预约返回200", resp.status_code == 200,
          f"actual={resp.status_code}, body={resp.text[:200]}")
    sched_cd_id = None
    if resp.status_code == 200:
        sched_cd_id = resp.json()["scheduled_release"]["id"]

    step("6.3", "Manually release CONFLICT-D batch")
    approve_cd = {"approved_by": "admin", "approval_remark": "手动发布冲突测试",
                  "release_note": "conflict-manual-release"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_conflict_d_id}/release",
                         json=approve_cd, headers=admin_h)
    check("手动发布CONFLICT-D返回200", resp.status_code == 200,
          f"actual={resp.status_code}, body={resp.text[:200]}")

    step("6.4", "Verify release audit detail contains '冲突' keyword")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=release&limit=200", headers=admin_h)
    logs = resp.json()
    conflict_release_logs = [l for l in logs
                             if l["result"] == "success"
                             and "冲突" in (l.get("detail") or "")]
    check("release审计detail含'冲突'关键词", len(conflict_release_logs) >= 1,
          f"count={len(conflict_release_logs)}")

    step("6.5", "Verify the original scheduled plan is now superseded/executed with clean status")
    if sched_cd_id:
        resp = requests.get(f"{BASE_URL}/api/release-plans",
                            params={"rule_id": rule_id, "batch_id": batch_conflict_d_id},
                            headers=admin_h)
        plans_cd = resp.json()
        if plans_cd:
            plan = plans_cd[0]
            check("CONFLICT-D计划状态=executed", plan["status"] == "executed",
                  f"actual={plan['status']}")
            check("executed_at非空", plan.get("executed_at") is not None,
                  f"actual={plan.get('executed_at')}")
            check("cancelled_at is None", plan.get("cancelled_at") is None,
                  f"actual={plan.get('cancelled_at')}")
            check("superseded_at is None", plan.get("superseded_at") is None,
                  f"actual={plan.get('superseded_at')}")

    section("测试结果汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
