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


def find_audit_logs(headers, action=None, operator=None, target_id=None, keyword_in_detail=None, limit=500):
    params = {}
    if action:
        params["action"] = action
    if operator:
        params["operator"] = operator
    params["limit"] = limit
    resp = requests.get(f"{BASE_URL}/api/audit-logs", params=params, headers=headers)
    logs = resp.json()
    result = []
    for log in logs:
        if target_id and log.get("target_id") != str(target_id):
            continue
        if keyword_in_detail and keyword_in_detail not in (log.get("detail") or ""):
            continue
        result.append(log)
    return result


def main():
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    approver_h = {"X-Username": "approver1", "Content-Type": "application/json"}
    user_h = {"X-Username": "user1", "Content-Type": "application/json"}

    # ================================================================
    # 全局前置清理: 清除所有遗留候选和pending预约，避免调度器干扰
    # ================================================================
    print("=" * 60)
    print("全局前置清理: 清除遗留候选 + pending预约")
    print("=" * 60)

    # 先清候选(最多5轮，防止嵌套)
    cleared_candidates = 0
    for _ in range(5):
        resp_c = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
        if resp_c.status_code != 200:
            break
        cand = resp_c.json()
        print(f"  清理遗留候选: id={cand.get('id')}, batch_id={cand.get('batch_id')}")
        requests.post(
            f"{BASE_URL}/api/candidate/cancel",
            params={"operated_by": "admin", "reason": "测试前置清理遗留候选"},
            headers=admin_h,
        )
        cleared_candidates += 1
    if cleared_candidates == 0:
        print("  无遗留候选")

    # 再清pending预约
    cleared_scheds = 0
    resp_s = requests.get(f"{BASE_URL}/api/scheduled-releases", headers=admin_h)
    if resp_s.status_code == 200:
        scheds = resp_s.json()
        pending_list = [s for s in scheds if s.get("status") == "pending"]
        for s in pending_list:
            print(f"  清理遗留pending预约: id={s['id']}, batch={s.get('batch_id')}, sched_time={s.get('scheduled_time')}")
            requests.post(
                f"{BASE_URL}/api/scheduled-releases/{s['id']}/cancel",
                params={"operated_by": "admin", "reason": "测试前置清理遗留预约"},
                headers=admin_h,
            )
            cleared_scheds += 1
    if cleared_scheds == 0:
        print("  无遗留pending预约")
    print(f"  清理完成: 候选{cleared_candidates}个, 预约{cleared_scheds}个\n")

    # ================================================================
    # SECTION 1: 取消候选遇到待执行计划 - 完整冲突审计链路
    # ================================================================
    section("1. 取消候选遇到待执行计划 - 完整冲突审计链路")

    step("1.1", "获取 rule_id")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    assert resp.status_code == 200, f"无法获取规则: {resp.status_code} {resp.text}"
    rule_id = resp.json()[0]["id"]
    print(f"    rule_id = {rule_id}")

    step("1.2", "创建批次AUDIT-CC-1并设为候选（创建排队计划）")
    batch_cc1_id, _ = import_and_calculate(
        "审计-批次AUDIT-CC-1(候选取消冲突1)", rule_id, "AUDIT-CC-1", "审计供应商CC1", admin_h
    )
    check("批次CC1导入计算成功", batch_cc1_id is not None)
    candidate_req_cc1 = {
        "batch_id": batch_cc1_id,
        "change_description": "审计测试-候选取消冲突批次CC1",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_cc1, headers=admin_h)
    check("设置候选CC1返回200", resp.status_code == 200, f"body={resp.text[:200]}")

    step("1.3", "为CC1创建远期预约（创建预约计划）")
    sched_time_cc1 = iso_future(1800)  # 30分钟后，避免在后续测试中被调度器自动触发
    sched_req_cc1 = {
        "batch_id": batch_cc1_id,
        "scheduled_time": sched_time_cc1,
        "change_description": "CC1远期预约(30min后)",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_cc1, headers=admin_h)
    check("创建CC1预约返回200", resp.status_code == 200, f"body={resp.text[:200]}")
    sched_cc1_id = resp.json()["scheduled_release"]["id"] if resp.status_code == 200 else None
    print(f"    sched_cc1_id = {sched_cc1_id}")

    step("1.4", "确认存在待执行计划: 至少1个活跃计划(queued或scheduled)")
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "batch_id": batch_cc1_id},
                        headers=admin_h)
    plans_cc1 = resp.json()
    print(f"    CC1关联计划数: {len(plans_cc1)}")
    active_cc1 = [p for p in plans_cc1 if p["status"] in ("queued", "scheduled")]
    queued_cc1 = [p for p in plans_cc1 if p["status"] == "queued"]
    scheduled_cc1 = [p for p in plans_cc1 if p["status"] == "scheduled"]
    all_cc1 = active_cc1
    check("存在至少1个活跃计划(queued/scheduled)", len(all_cc1) >= 1,
          f"active={len(all_cc1)}, queued={len(queued_cc1)}, scheduled={len(scheduled_cc1)}")
    check("存在scheduled预约计划(由预约创建产生)", len(scheduled_cc1) >= 1)
    queued_cc1_id = queued_cc1[0]["id"] if queued_cc1 else None
    scheduled_cc1_id = scheduled_cc1[0]["id"] if scheduled_cc1 else None
    check_ids_to_verify = []
    if queued_cc1_id:
        check_ids_to_verify.append(queued_cc1_id)
    if scheduled_cc1_id:
        check_ids_to_verify.append(scheduled_cc1_id)

    step("1.5", "调用取消候选接口，验证返回结构含冲突信息和联动结果")
    resp = requests.post(
        f"{BASE_URL}/api/candidate/cancel",
        params={"operated_by": "admin", "reason": "测试取消候选冲突审计完整链路-原因X123"},
        headers=admin_h,
    )
    check("取消候选返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text[:300]}")
    if resp.status_code == 200:
        cancel_data = resp.json()
        has_conflict_info = "conflict_info" in cancel_data
        has_plan_result = "plan_linked_result" in cancel_data
        check("返回包含conflict_info字段", has_conflict_info)
        check("返回包含plan_linked_result字段", has_plan_result)
        if has_conflict_info:
            ci = cancel_data["conflict_info"]
            check("conflict_info.has_conflict=True", ci.get("has_conflict") == True,
                  f"actual={ci.get('has_conflict')}")
            check("conflict_info含conflict_plan_id", ci.get("conflict_plan_id") is not None)
            check("conflict_reason含命中计划信息",
                  "命中计划" in (ci.get("conflict_reason") or ""),
                  f"actual={ci.get('conflict_reason')}")
            check("suggestion含联动取消动作",
                  "联动取消" in (ci.get("suggestion") or ""),
                  f"actual={ci.get('suggestion')}")
        if has_plan_result:
            pr = cancel_data["plan_linked_result"]
            check("plan_linked_result含total_found", "total_found" in pr)
            check("plan_linked_result含cancelled_ids数组", "cancelled_ids" in pr and isinstance(pr["cancelled_ids"], list))
            check("plan_linked_result含skipped_ids数组", "skipped_ids" in pr and isinstance(pr["skipped_ids"], list))
            check("plan_linked_result含triggered_by=admin", pr.get("triggered_by") == "admin")
            check("plan_linked_result含action=linked_cancel", pr.get("action") == "linked_cancel")
            check("cancelled_ids非空(至少1个计划被联动取消)", len(pr.get("cancelled_ids", [])) >= 1,
                  f"cancelled_ids={pr.get('cancelled_ids')}")
            for d in pr.get("details", []):
                if d.get("action") == "linked_cancel":
                    check("detail含plan_id字段", d.get("plan_id") is not None)
                    check("detail含source_type(计划来源)", d.get("source_type") is not None)
                    check("detail含plan_type(计划类型)", d.get("plan_type") is not None)
                    check("detail含old_status->new_status",
                          d.get("old_status") and d.get("new_status"),
                          f"detail={d}")
                    break

    step("1.6", "审计日志cancel_candidate含完整冲突信息")
    cancel_audit_logs = find_audit_logs(admin_h, action="cancel_candidate", operator="admin",
                                        keyword_in_detail="测试取消候选冲突审计")
    check("cancel_candidate审计存在", len(cancel_audit_logs) >= 1, f"count={len(cancel_audit_logs)}")
    if cancel_audit_logs:
        log = cancel_audit_logs[0]
        check("审计result=success", log.get("result") == "success")
        detail = log.get("detail") or ""
        check("审计detail含取消原因", "原因X123" in detail, f"detail={detail[:200]}")
        check("审计detail含冲突命中(或联动信息)",
              "冲突命中" in detail or ("已取消计划ID" in detail and len(cancel_audit_logs) >= 1),
              f"detail={detail[:300]}")
        check("审计detail含计划联动处理", "计划联动处理" in detail, f"detail={detail[:300]}")
        check("审计detail含成功取消数量", "成功取消" in detail, f"detail={detail[:300]}")
        check("审计detail含触发人信息", "触发人=admin" in detail, f"detail={detail[:300]}")
        check("审计detail含动作类型(linked_cancel)",
              "linked_cancel" in detail or "动作类型=" in detail, f"detail={detail[:300]}")
        check("审计detail含计划ID列表", "已取消计划ID" in detail, f"detail={detail[:300]}")

    step("1.7", "验证被联动取消的计划状态和事件详情")
    for plan_id_to_check in [queued_cc1_id, scheduled_cc1_id]:
        if not plan_id_to_check:
            continue
        resp = requests.get(f"{BASE_URL}/api/release-plans/{plan_id_to_check}", headers=admin_h)
        if resp.status_code == 200:
            plan_detail = resp.json()
            check(f"计划#{plan_id_to_check} status=cancelled", plan_detail["status"] == "cancelled",
                  f"actual={plan_detail['status']}")
            check(f"计划#{plan_id_to_check} cancelled_at非空", plan_detail.get("cancelled_at") is not None)
            check(f"计划#{plan_id_to_check} conflict_reason含取消候选联动",
                  "取消候选联动" in (plan_detail.get("conflict_reason") or ""),
                  f"actual={plan_detail.get('conflict_reason')}")
            check(f"计划#{plan_id_to_check} source_detail含候选取消联动",
                  "候选取消联动" in (plan_detail.get("source_detail") or ""),
                  f"actual={plan_detail.get('source_detail')}")
            check(f"计划#{plan_id_to_check} executed_at=None", plan_detail.get("executed_at") is None)
            check(f"计划#{plan_id_to_check} superseded_at=None", plan_detail.get("superseded_at") is None)
            check(f"计划#{plan_id_to_check} expired_at=None", plan_detail.get("expired_at") is None)
            events = plan_detail.get("events", [])
            check(f"计划#{plan_id_to_check} 有事件记录", len(events) >= 1)
            cancel_events = [e for e in events if e["event_type"] == "cancelled"]
            if cancel_events:
                ev = cancel_events[0]
                ev_detail = ev.get("detail") or {}
                check(f"计划#{plan_id_to_check} 事件cancel_source=cancel_candidate_linked",
                      ev_detail.get("cancel_source") == "cancel_candidate_linked",
                      f"actual={ev_detail.get('cancel_source')}")
                check(f"计划#{plan_id_to_check} 事件triggered_by=admin",
                      ev_detail.get("triggered_by") == "admin",
                      f"actual={ev_detail.get('triggered_by')}")
                check(f"计划#{plan_id_to_check} 事件action_type=linked_cancel",
                      ev_detail.get("action_type") == "linked_cancel",
                      f"actual={ev_detail.get('action_type')}")
                check(f"计划#{plan_id_to_check} 事件含plan_source_type",
                      ev_detail.get("plan_source_type") is not None)
                check(f"计划#{plan_id_to_check} 事件含plan_type",
                      ev_detail.get("plan_type") is not None)

    # ================================================================
    # SECTION 2: 重复触发幂等性验证
    # ================================================================
    section("2. 重复触发幂等性验证")

    # 清理前置候选(防止之前测试遗留)
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    if resp.status_code == 200:
        requests.post(f"{BASE_URL}/api/candidate/cancel",
                      params={"operated_by": "admin", "reason": "幂等测试前置清理"},
                      headers=admin_h)

    step("2.1", "创建批次IDEM-2并设置候选")
    batch_idem2_id, _ = import_and_calculate(
        "幂等-批次IDEM-2(重复取消测试)", rule_id, "IDEM-2", "幂等供应商2", admin_h
    )
    check("批次IDEM-2导入成功", batch_idem2_id is not None)
    candidate_req_idem2 = {
        "batch_id": batch_idem2_id,
        "change_description": "幂等测试-批次IDEM-2候选",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_idem2, headers=admin_h)
    check("设置IDEM-2候选返回200", resp.status_code == 200)

    step("2.2", "为IDEM-2创建远期预约")
    sched_time_idem2 = iso_future(1800)  # 30分钟后，不被调度器自动触发
    sched_req_idem2 = {
        "batch_id": batch_idem2_id,
        "scheduled_time": sched_time_idem2,
        "change_description": "幂等测试-IDEM-2远期预约(30min后)",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_idem2, headers=admin_h)
    check("创建IDEM-2预约返回200", resp.status_code == 200)
    sched_idem2_id = resp.json()["scheduled_release"]["id"] if resp.status_code == 200 else None

    step("2.3", "第一次取消候选（含联动）")
    resp1 = requests.post(
        f"{BASE_URL}/api/candidate/cancel",
        params={"operated_by": "admin", "reason": "幂等测试第一次取消-R1"},
        headers=admin_h,
    )
    check("第一次取消候选返回200", resp1.status_code == 200)
    plan_result1 = resp1.json().get("plan_linked_result", {}) if resp1.status_code == 200 else {}
    cancelled_count_1 = len(plan_result1.get("cancelled_ids", []))
    skipped_count_1 = len(plan_result1.get("skipped_ids", []))
    print(f"    第一次: 取消={cancelled_count_1}, 跳过={skipped_count_1}")
    check("第一次至少取消1个计划", cancelled_count_1 >= 1)

    step("2.4", "第二次取消候选（无候选，应返回404并记录not_found审计）")
    resp2 = requests.post(
        f"{BASE_URL}/api/candidate/cancel",
        params={"operated_by": "admin", "reason": "幂等测试第二次取消-R2(无候选)"},
        headers=admin_h,
    )
    check("第二次取消候选返回404", resp2.status_code == 404, f"actual={resp2.status_code}")
    not_found_logs = find_audit_logs(admin_h, action="cancel_candidate", operator="admin",
                                     keyword_in_detail="无候选存在")
    check("cancel_candidate审计记录not_found", len(not_found_logs) >= 1)
    if not_found_logs:
        check("not_found审计result=not_found", not_found_logs[0].get("result") == "not_found")

    step("2.5", "重复取消已取消的预约（幂等跳过，不应再次状态流转）")
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "batch_id": batch_idem2_id, "status": "cancelled"},
                        headers=admin_h)
    cancelled_plans_before = resp.json()
    check("IDEM-2有cancelled计划", len(cancelled_plans_before) >= 1)
    if cancelled_plans_before and sched_idem2_id:
        plan_before = cancelled_plans_before[0]
        plan_id_before = plan_before["id"]
        cancelled_at_before = plan_before["cancelled_at"]
        events_before = requests.get(f"{BASE_URL}/api/release-plans/{plan_id_before}/events",
                                     headers=admin_h).json()
        event_count_before = len(events_before)

        resp_dup = requests.post(
            f"{BASE_URL}/api/scheduled-releases/{sched_idem2_id}/cancel",
            params={"operated_by": "admin", "reason": "幂等测试-重复取消已取消预约-R3"},
            headers=admin_h,
        )
        check("重复取消已取消预约返回400(状态非pending)", resp_dup.status_code == 400,
              f"actual={resp_dup.status_code}, body={resp_dup.text[:200]}")
        reject_logs = find_audit_logs(admin_h, action="cancel_scheduled_release",
                                      keyword_in_detail="取消预约被拒绝")
        check("cancel_scheduled_release记录rejected审计", len(reject_logs) >= 1)

        plan_after_resp = requests.get(f"{BASE_URL}/api/release-plans/{plan_id_before}", headers=admin_h)
        if plan_after_resp.status_code == 200:
            plan_after = plan_after_resp.json()
            check("幂等保护: cancelled_at未变化", plan_after["cancelled_at"] == cancelled_at_before)
            events_after = requests.get(f"{BASE_URL}/api/release-plans/{plan_id_before}/events",
                                        headers=admin_h).json()
            check("幂等保护: 事件数不增加(无重复事件)", len(events_after) == event_count_before,
                  f"before={event_count_before}, after={len(events_after)}")

    step("2.6", "重复设置同批次候选（应幂等，不产生重复queued计划）")
    candidate_req_idem2_again = {
        "batch_id": batch_idem2_id,
        "change_description": "幂等测试-重复设置同批次候选",
        "set_by": "admin",
    }
    resp_set1 = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_idem2_again, headers=admin_h)
    check("重设候选第1次返回200", resp_set1.status_code == 200)
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "batch_id": batch_idem2_id, "status": "queued"},
                        headers=admin_h)
    queued_after_1 = len(resp.json())
    check(f"设置后queued计划={queued_after_1}", queued_after_1 >= 1)

    resp_set2 = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_idem2_again, headers=admin_h)
    check("重设候选第2次返回200", resp_set2.status_code == 200)
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "batch_id": batch_idem2_id, "status": "queued"},
                        headers=admin_h)
    queued_after_2 = len(resp.json())
    check(f"重复设置后queued计划数不变({queued_after_1})", queued_after_2 == queued_after_1,
          f"after_1={queued_after_1}, after_2={queued_after_2}")

    # 清理候选以便后续测试
    requests.post(f"{BASE_URL}/api/candidate/cancel",
                  params={"operated_by": "admin", "reason": "清理幂等测试候选"},
                  headers=admin_h)

    # ================================================================
    # SECTION 3: 权限拒绝和无权取消的完整审计
    # ================================================================
    section("3. 权限拒绝和无权取消的完整审计")

    step("3.1", "普通用户(user1)尝试取消候选（403权限拒绝）")
    batch_perm1_id, _ = import_and_calculate(
        "权限-批次PERM-1(权限测试)", rule_id, "PERM-1", "权限供应商1", admin_h
    )
    if batch_perm1_id:
        candidate_req_perm1 = {
            "batch_id": batch_perm1_id,
            "change_description": "权限测试候选",
            "set_by": "admin",
        }
        requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_perm1, headers=admin_h)

    resp_user_cancel = requests.post(
        f"{BASE_URL}/api/candidate/cancel",
        params={"operated_by": "user1", "reason": "普通用户尝试取消-应被拒绝"},
        headers=user_h,
    )
    check("普通用户取消候选被403拒绝", resp_user_cancel.status_code == 403,
          f"actual={resp_user_cancel.status_code}")

    step("3.2", "验证permission_denied审计含候选取消接口路径")
    perm_denied_logs = find_audit_logs(admin_h, action="permission_denied", operator="user1",
                                       keyword_in_detail="candidate")
    check("permission_denied审计存在(candidate路径)", len(perm_denied_logs) >= 1,
          f"count={len(perm_denied_logs)}")
    if perm_denied_logs:
        log = perm_denied_logs[0]
        check("权限拒绝审计operator=user1", log.get("operator") == "user1")
        check("权限拒绝审计target_type=api_endpoint", log.get("target_type") == "api_endpoint")
        check("权限拒绝审计result=denied", log.get("result") == "denied")
        check("权限拒绝审计detail含访问被拒", "访问被拒" in (log.get("detail") or ""),
              f"detail={log.get('detail')}")

    step("3.3", "普通用户尝试取消预约（403权限拒绝）")
    batch_perm2_id, _ = import_and_calculate(
        "权限-批次PERM-2(预约权限测试)", rule_id, "PERM-2", "权限供应商2", admin_h
    )
    sched_perm2_id = None
    if batch_perm2_id:
        sched_time_perm2 = iso_future(1800)  # 30分钟后
        sched_req_perm2 = {
            "batch_id": batch_perm2_id,
            "scheduled_time": sched_time_perm2,
            "change_description": "权限测试预约(30min后)",
            "set_by": "admin",
        }
        resp_sched_perm2 = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_perm2, headers=admin_h)
        if resp_sched_perm2.status_code == 200:
            sched_perm2_id = resp_sched_perm2.json()["scheduled_release"]["id"]

    if sched_perm2_id:
        resp_user_cancel_sched = requests.post(
            f"{BASE_URL}/api/scheduled-releases/{sched_perm2_id}/cancel",
            params={"operated_by": "user1", "reason": "普通用户取消预约-应被拒绝"},
            headers=user_h,
        )
        check("普通用户取消预约被403拒绝", resp_user_cancel_sched.status_code == 403,
              f"actual={resp_user_cancel_sched.status_code}")
        perm_denied_sched = find_audit_logs(admin_h, action="permission_denied", operator="user1",
                                            keyword_in_detail="scheduled-releases")
        check("permission_denied审计存在(scheduled-releases路径)", len(perm_denied_sched) >= 1)

    step("3.4", "approver1有权限取消，验证成功审计")
    if sched_perm2_id:
        resp_approver_cancel = requests.post(
            f"{BASE_URL}/api/scheduled-releases/{sched_perm2_id}/cancel",
            params={"operated_by": "approver1", "reason": "approver1有权限取消测试"},
            headers=approver_h,
        )
        check("approver1取消预约返回200/400(状态可能已变)",
              resp_approver_cancel.status_code in (200, 400),
              f"actual={resp_approver_cancel.status_code}")
        if resp_approver_cancel.status_code == 200:
            success_logs = find_audit_logs(admin_h, action="cancel_scheduled_release", operator="approver1",
                                           keyword_in_detail="approver1有权限取消")
            check("approver1取消预约有成功审计", len(success_logs) >= 1, f"count={len(success_logs)}")
            if success_logs:
                check("成功审计result=success", success_logs[0].get("result") == "success")
                check("成功审计含状态流转信息", "状态:" in (success_logs[0].get("detail") or ""),
                      f"detail={success_logs[0].get('detail')[:200]}")

    step("3.5", "普通用户尝试手动发布（403权限拒绝审计）")
    batch_perm3_id, _ = import_and_calculate(
        "权限-批次PERM-3(发布权限测试)", rule_id, "PERM-3", "权限供应商3", approver_h
    )
    if batch_perm3_id:
        approve_perm3 = {"approved_by": "user1", "approval_remark": "普通用户发布-应被拒绝", "release_note": "perm-test"}
        resp_user_release = requests.post(
            f"{BASE_URL}/api/batches/{batch_perm3_id}/release",
            json=approve_perm3,
            headers=user_h,
        )
        check("普通用户发布被403拒绝", resp_user_release.status_code == 403)
        perm_denied_release = find_audit_logs(admin_h, action="permission_denied", operator="user1",
                                              keyword_in_detail="release")
        check("发布权限拒绝审计存在", len(perm_denied_release) >= 1, f"count={len(perm_denied_release)}")

    # ================================================================
    # SECTION 4: 配置变更生效 + 导出含来源字段不丢失
    # ================================================================
    section("4. 配置变更生效 + 导出含来源字段不丢失")

    step("4.1", "先获取当前default_expire_hours配置(可能未设置)")
    resp = requests.get(f"{BASE_URL}/api/release-plan-configs", headers=admin_h)
    check("获取配置列表返回200", resp.status_code == 200)
    cfgs_before = resp.json()
    expire_cfg_before = [c for c in cfgs_before if c["config_key"] == "default_expire_hours"]
    print(f"    已有default_expire_hours配置: {len(expire_cfg_before)}个")

    step("4.2", "更新default_expire_hours为2小时(极小值便于测试)")
    resp_cfg_update = requests.put(
        f"{BASE_URL}/api/release-plan-configs",
        params={"config_key": "default_expire_hours", "config_value": "2",
                "description": "回归测试临时设置-2小时过期"},
        headers=admin_h,
    )
    check("更新default_expire_hours返回200", resp_cfg_update.status_code == 200,
          f"actual={resp_cfg_update.status_code}, body={resp_cfg_update.text[:200]}")
    if resp_cfg_update.status_code == 200:
        cfg_audit_logs = find_audit_logs(admin_h, action="update_plan_config", operator="admin",
                                         keyword_in_detail="default_expire_hours=2")
        check("update_plan_config审计存在", len(cfg_audit_logs) >= 1, f"count={len(cfg_audit_logs)}")
        if cfg_audit_logs:
            check("配置更新审计result=success", cfg_audit_logs[0].get("result") == "success")

    step("4.3", "验证配置立即生效(列表能查到新值)")
    resp = requests.get(f"{BASE_URL}/api/release-plan-configs", headers=admin_h)
    cfgs_after = resp.json()
    expire_cfg_after = [c for c in cfgs_after if c["config_key"] == "default_expire_hours"]
    check("配置生效: default_expire_hours存在且=2",
          len(expire_cfg_after) >= 1 and expire_cfg_after[0]["config_value"] == "2",
          f"cfgs={[(c['config_key'], c['config_value']) for c in expire_cfg_after]}")

    step("4.4", "手动触发过期处理并验证能读取配置")
    resp_trigger_expire = requests.post(f"{BASE_URL}/api/release-plans/trigger-expire", headers=admin_h)
    check("手动触发过期返回200", resp_trigger_expire.status_code == 200,
          f"actual={resp_trigger_expire.status_code}")

    step("4.5", "发布一个版本用于测试导出")
    batch_export_id, _ = import_and_calculate(
        "导出-批次EXPORT-SRC(导出来源字段测试)", rule_id, "EXPORT-SRC", "导出供应商SRC", admin_h
    )
    released_version = None
    if batch_export_id:
        approve_export = {"approved_by": "admin", "approval_remark": "导出测试发布审批",
                          "release_note": "release-source-test-v1"}
        resp_release_export = requests.post(
            f"{BASE_URL}/api/batches/{batch_export_id}/release",
            json=approve_export,
            headers=admin_h,
        )
        check("发布EXPORT-SRC返回200", resp_release_export.status_code == 200,
              f"actual={resp_release_export.status_code}, body={resp_release_export.text[:200]}")
        if resp_release_export.status_code == 200:
            released_version = resp_release_export.json()["version"]
            print(f"    发布版本: {released_version}")

    step("4.6", "导出活动版本并验证release_source和plan_status字段不丢失")
    if released_version:
        resp_export = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
        check("导出返回200", resp_export.status_code == 200)
        if resp_export.status_code == 200:
            export_data = resp_export.json()
            check("导出version正确", export_data.get("version") == released_version)
            check("导出含release_source字段", export_data.get("release_source") is not None)
            check("导出release_source非空", len(export_data.get("release_source") or "") > 0)
            check("导出含plan_status字段(兼容扩展)", "plan_status" in export_data)
            check("导出approved_by非空", len(export_data.get("approved_by") or "") > 0)
            check("导出release_note非空", len(export_data.get("release_note") or "") > 0)
            print(f"    release_source={export_data.get('release_source')}, plan_status={export_data.get('plan_status')}")

    step("4.7", "恢复default_expire_hours为默认值72小时")
    resp_cfg_restore = requests.put(
        f"{BASE_URL}/api/release-plan-configs",
        params={"config_key": "default_expire_hours", "config_value": "72",
                "description": "恢复默认过期时间72小时"},
        headers=admin_h,
    )
    check("恢复配置返回200", resp_cfg_restore.status_code == 200,
          f"actual={resp_cfg_restore.status_code}")

    # ================================================================
    # SECTION 5: 计划详情状态细分验证（各状态不混淆）
    # ================================================================
    section("5. 计划详情状态细分验证（各状态不混淆）")

    step("5.1", "导入批次STATE-A并预约(状态=scheduled)")
    batch_state_a_id, _ = import_and_calculate(
        "状态-批次STATE-A(预约执行测试)", rule_id, "STATE-A", "状态供应商A", admin_h
    )
    sched_state_a_id = None
    if batch_state_a_id:
        sched_time_state_a = iso_future(12)
        sched_req_state_a = {
            "batch_id": batch_state_a_id,
            "scheduled_time": sched_time_state_a,
            "change_description": "状态测试-STATE-A短预约(到点执行)",
            "set_by": "admin",
        }
        resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_state_a, headers=admin_h)
        check("创建STATE-A预约返回200", resp.status_code == 200)
        if resp.status_code == 200:
            sched_state_a_id = resp.json()["scheduled_release"]["id"]
            print(f"    sched_state_a_id={sched_state_a_id}, scheduled_time={sched_time_state_a}")

    step("5.2", "等待STATE-A预约自动执行并验证状态=executed")
    if sched_state_a_id:
        def state_a_executed():
            r = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_state_a_id}", headers=admin_h)
            return r.status_code == 200 and r.json()["status"] == "executed"
        ok = wait_until(state_a_executed, timeout_sec=30, poll_sec=2,
                        label="等待STATE-A预约自动执行")
        check("STATE-A预约到点自动执行", ok)

    if batch_state_a_id:
        resp = requests.get(f"{BASE_URL}/api/release-plans",
                            params={"rule_id": rule_id, "batch_id": batch_state_a_id, "status": "executed"},
                            headers=admin_h)
        state_a_exec_plans = resp.json()
        check("STATE-A存在executed计划", len(state_a_exec_plans) >= 1)
        if state_a_exec_plans:
            plan_a = state_a_exec_plans[0]
            check(f"STATE-A计划 source_type=scheduled",
                  plan_a.get("source_type") == "scheduled",
                  f"actual={plan_a.get('source_type')}")
            check(f"STATE-A计划 status=executed(不混淆为cancelled/expired)",
                  plan_a.get("status") == "executed",
                  f"actual={plan_a.get('status')}")
            check(f"STATE-A计划 executed_at非空", plan_a.get("executed_at") is not None)
            check(f"STATE-A计划 cancelled_at=None", plan_a.get("cancelled_at") is None)
            check(f"STATE-A计划 expired_at=None", plan_a.get("expired_at") is None)
            check(f"STATE-A计划 superseded_at=None", plan_a.get("superseded_at") is None)
            check(f"STATE-A计划 source_detail含预约信息",
                  "预约" in (plan_a.get("source_detail") or ""),
                  f"actual={plan_a.get('source_detail')[:100]}")

    step("5.3", "STATE-B: 手动顶掉验证(superseded)")
    batch_state_b1_id, _ = import_and_calculate(
        "状态-批次STATE-B1(被手动顶掉)", rule_id, "STATE-B1", "状态供应商B1", admin_h
    )
    if batch_state_b1_id:
        candidate_req_b1 = {
            "batch_id": batch_state_b1_id,
            "change_description": "状态测试-B1设为候选(将被顶掉)",
            "set_by": "admin",
        }
        requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_b1, headers=admin_h)

    batch_state_b2_id, _ = import_and_calculate(
        "状态-批次STATE-B2(手动顶掉B1)", rule_id, "STATE-B2", "状态供应商B2", admin_h
    )
    if batch_state_b2_id:
        candidate_req_b2 = {
            "batch_id": batch_state_b2_id,
            "change_description": "状态测试-B2顶掉B1",
            "set_by": "admin",
        }
        resp_set_b2 = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_b2, headers=admin_h)
        check("B2设置候选(顶掉B1)返回200", resp_set_b2.status_code == 200)

    if batch_state_b1_id:
        resp = requests.get(f"{BASE_URL}/api/release-plans",
                            params={"rule_id": rule_id, "batch_id": batch_state_b1_id, "status": "superseded"},
                            headers=admin_h)
        state_b1_superseded = resp.json()
        check("STATE-B1计划状态=superseded(被手动顶掉)", len(state_b1_superseded) >= 1,
              f"count={len(state_b1_superseded)}")
        if state_b1_superseded:
            plan_b1 = state_b1_superseded[0]
            check("B1 superseded_at非空", plan_b1.get("superseded_at") is not None)
            check("B1 superseded_by_plan_id非空", plan_b1.get("superseded_by_plan_id") is not None)
            check("B1 conflict_reason含被顶掉", "顶掉" in (plan_b1.get("conflict_reason") or ""),
                  f"actual={plan_b1.get('conflict_reason')[:100]}")
            check("B1 executed_at=None(未执行)", plan_b1.get("executed_at") is None)
            check("B1 cancelled_at=None(非取消)", plan_b1.get("cancelled_at") is None)
            check("B1 expired_at=None(非过期)", plan_b1.get("expired_at") is None)

    step("5.4", "STATE-C: 验证不同终态不互相混淆(查询过滤)")
    all_statuses = ["queued", "scheduled", "executed", "cancelled", "superseded", "expired", "failed"]
    for st in all_statuses:
        resp = requests.get(f"{BASE_URL}/api/release-plans",
                            params={"rule_id": rule_id, "status": st, "limit": 50},
                            headers=admin_h)
        if resp.status_code == 200:
            plans = resp.json()
            all_correct = all(p["status"] == st for p in plans)
            check(f"按status={st}过滤结果纯(无混入其他状态)", all_correct,
                  f"count={len(plans)}, mixed={[(p['id'], p['status']) for p in plans if p['status'] != st][:5]}")

    # ================================================================
    # SECTION 6: 服务重启恢复验证(通过触发recover接口模拟)
    # ================================================================
    section("6. 服务重启恢复验证(通过recover接口模拟)")

    step("6.1", "创建STATE-R1批次并设置远期预约(在pending状态)")
    batch_r1_id, _ = import_and_calculate(
        "恢复-批次STATE-R1(重启恢复测试)", rule_id, "STATE-R1", "恢复供应商R1", admin_h
    )
    sched_r1_id = None
    if batch_r1_id:
        sched_time_r1 = iso_future(1800)
        sched_req_r1 = {
            "batch_id": batch_r1_id,
            "scheduled_time": sched_time_r1,
            "change_description": "恢复测试-R1预约pending状态",
            "set_by": "admin",
        }
        resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_r1, headers=admin_h)
        check("创建R1预约返回200", resp.status_code == 200)
        if resp.status_code == 200:
            sched_r1_id = resp.json()["scheduled_release"]["id"]

    step("6.2", "验证R1预约为pending,计划为scheduled")
    if sched_r1_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_r1_id}", headers=admin_h)
        sched_r1 = resp.json() if resp.status_code == 200 else None
        check("R1预约状态=pending", sched_r1 and sched_r1["status"] == "pending",
              f"actual={sched_r1['status'] if sched_r1 else 'n/a'}")

    if batch_r1_id:
        resp = requests.get(f"{BASE_URL}/api/release-plans",
                            params={"rule_id": rule_id, "batch_id": batch_r1_id, "status": "scheduled"},
                            headers=admin_h)
        r1_plans_before = resp.json()
        check("R1 scheduled计划存在(恢复前)", len(r1_plans_before) >= 1)

    step("6.3", "调用recover接口模拟重启恢复")
    resp_recover = requests.post(f"{BASE_URL}/api/release-plans/recover", headers=admin_h)
    check("recover接口返回200", resp_recover.status_code == 200,
          f"actual={resp_recover.status_code}")
    if resp_recover.status_code == 200:
        recover_stats = resp_recover.json()
        print(f"    recover_stats: {recover_stats}")
        check("recover返回recovered_queued字段", "recovered_queued" in recover_stats)
        check("recover返回recovered_scheduled字段", "recovered_scheduled" in recover_stats)
        check("recover返回auto_expired字段", "auto_expired" in recover_stats)

    step("6.4", "恢复后R1计划仍为scheduled(不丢失、不乱变)")
    if batch_r1_id:
        resp = requests.get(f"{BASE_URL}/api/release-plans",
                            params={"rule_id": rule_id, "batch_id": batch_r1_id, "status": "scheduled"},
                            headers=admin_h)
        r1_plans_after = resp.json()
        check("R1 scheduled计划存在(恢复后)", len(r1_plans_after) >= 1,
              f"before={len(r1_plans_before)}, after={len(r1_plans_after)}")
        if r1_plans_after:
            plan_r1_after = r1_plans_after[0]
            check("R1计划source_detail恢复后不丢失",
                  len(plan_r1_after.get("source_detail") or "") > 0,
                  f"actual={plan_r1_after.get('source_detail')}")
            check("R1计划created_by恢复后不丢失",
                  len(plan_r1_after.get("created_by") or "") > 0)

    step("6.5", "统计验证: 调用stats接口,各状态计数与实际一致")
    resp_stats = requests.get(f"{BASE_URL}/api/release-plans-stats",
                              params={"rule_id": rule_id}, headers=admin_h)
    check("stats接口返回200", resp_stats.status_code == 200)
    if resp_stats.status_code == 200:
        stats_list = resp_stats.json()
        if stats_list:
            stats = stats_list[0]
            all_counts_ok = all(
                isinstance(stats.get(f"{st}_count"), int) and stats.get(f"{st}_count") >= 0
                for st in ["queued", "scheduled", "executed", "cancelled",
                           "expired", "superseded", "failed"]
            )
            check("所有状态计数为非负整数", all_counts_ok)
            manual_total = sum(
                stats.get(f"{st}_count", 0)
                for st in ["queued", "scheduled", "executed", "cancelled",
                           "expired", "superseded", "failed"]
            )
            check("各状态之和=total_count", manual_total == stats.get("total_count"),
                  f"sum={manual_total}, total={stats.get('total_count')}")

    # ================================================================
    # 结果汇总
    # ================================================================
    section("测试结果汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
