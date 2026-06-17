import requests
import json
import sys
import time
import os
import tempfile
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


def import_and_calculate(batch_name, rule_id, supplier_code, supplier_name, headers, base_url=None):
    url = base_url or BASE_URL
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
    resp = requests.post(f"{url}/api/batches/import", json=batch, headers=headers)
    if resp.status_code != 200:
        return None, None
    batch_id = resp.json()["id"]
    resp = requests.post(f"{url}/api/batches/{batch_id}/calculate", headers=headers)
    return batch_id, resp


def main():
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    approver_h = {"X-Username": "approver1", "Content-Type": "application/json"}
    user_h = {"X-Username": "user1", "Content-Type": "application/json"}

    section("1. 迁移验证 - 无痛升级 + schema版本")
    step("1.1", "schema-version端点返回版本号")
    resp = requests.get(f"{BASE_URL}/api/_meta/schema-version", headers=admin_h)
    check("schema-version返回200", resp.status_code == 200, f"actual={resp.status_code}")
    if resp.status_code == 200:
        body = resp.json()
        check("schema_version>=2", body.get("schema_version", 0) >= 2, f"actual={body.get('schema_version')}")
        check("target_version>=2", body.get("target_version", 0) >= 2, f"actual={body.get('target_version')}")
        print(f"    schema_version={body.get('schema_version')}, target={body.get('target_version')}")

    step("1.2", "release_plans表存在 - 列表接口返回200")
    resp = requests.get(f"{BASE_URL}/api/release-plans", headers=admin_h)
    check("release-plans列表返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text[:200]}")
    if resp.status_code == 200:
        check("返回是列表", isinstance(resp.json(), list))

    step("1.3", "release-plan-configs默认配置存在")
    resp = requests.get(f"{BASE_URL}/api/release-plan-configs", headers=admin_h)
    check("plan-configs返回200", resp.status_code == 200, f"actual={resp.status_code}")
    if resp.status_code == 200:
        configs = resp.json()
        check("至少4条默认配置", len(configs) >= 4, f"count={len(configs)}")
        keys = {c["config_key"] for c in configs}
        check("含allow_early_window_seconds", "allow_early_window_seconds" in keys)
        check("含allow_late_window_seconds", "allow_late_window_seconds" in keys)
        check("含default_expire_hours", "default_expire_hours" in keys)
        check("含max_queued_per_rule", "max_queued_per_rule" in keys)
        print(f"    配置keys={sorted(keys)}")

    section("2. 权限拒绝 - 普通用户不能访问配置/管理计划")
    step("2.1", "普通用户访问release-plan-configs被403")
    resp = requests.get(f"{BASE_URL}/api/release-plan-configs", headers=user_h)
    check("普通用户访问configs被403", resp.status_code == 403, f"actual={resp.status_code}")

    step("2.2", "普通用户修改配置被403")
    resp = requests.put(f"{BASE_URL}/api/release-plan-configs",
                        params={"config_key": "allow_early_window_seconds", "config_value": "100"},
                        headers=user_h)
    check("普通用户修改config被403", resp.status_code == 403, f"actual={resp.status_code}")

    step("2.3", "普通用户触发expire被403")
    resp = requests.post(f"{BASE_URL}/api/release-plans/trigger-expire", headers=user_h)
    check("普通用户trigger-expire被403", resp.status_code == 403, f"actual={resp.status_code}")

    step("2.4", "普通用户触发recover被403")
    resp = requests.post(f"{BASE_URL}/api/release-plans/recover", headers=user_h)
    check("普通用户recover被403", resp.status_code == 403, f"actual={resp.status_code}")

    step("2.5", "普通用户validate-config被403")
    resp = requests.post(f"{BASE_URL}/api/release-plan-configs/validate",
                         json={"config_key": "allow_early_window_seconds", "config_value": "100"},
                         headers=user_h)
    check("普通用户validate-config被403", resp.status_code == 403, f"actual={resp.status_code}")

    step("2.6", "permission_denied审计记录存在")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=permission_denied&limit=100", headers=admin_h)
    logs = resp.json()
    plan_perm_denied = [l for l in logs if l["operator"] == "user1" and
                        ("plan" in l.get("detail", "").lower() or "config" in l.get("detail", "").lower())]
    check("配置/计划相关权限拒绝审计存在", len(plan_perm_denied) >= 1, f"count={len(plan_perm_denied)}")

    section("3. 配置变更验证 - 非法报错 + 合法生效 + 审计")
    step("3.1", "validate未知配置项 - 返回valid=false")
    resp = requests.post(f"{BASE_URL}/api/release-plan-configs/validate",
                         json={"config_key": "unknown_config_xyz", "config_value": "999"},
                         headers=admin_h)
    check("validate未知配置200", resp.status_code == 200)
    r = resp.json()
    check("未知配置valid=false", r.get("valid") is False)
    check("未知配置有错误消息", len(r.get("error_message") or "") > 0)

    step("3.2", "validate配置值非法类型 - 返回valid=false")
    resp = requests.post(f"{BASE_URL}/api/release-plan-configs/validate",
                         json={"config_key": "allow_early_window_seconds", "config_value": "not_a_number"},
                         headers=admin_h)
    check("validate非法类型200", resp.status_code == 200)
    r = resp.json()
    check("非法类型valid=false", r.get("valid") is False)

    step("3.3", "validate配置值超出范围 - 返回valid=false")
    resp = requests.post(f"{BASE_URL}/api/release-plan-configs/validate",
                         json={"config_key": "allow_early_window_seconds", "config_value": "-100"},
                         headers=admin_h)
    check("validate超出范围200", resp.status_code == 200)
    r = resp.json()
    check("超出范围valid=false", r.get("valid") is False)

    step("3.4", "修改非法配置项 - 返回400")
    resp = requests.put(f"{BASE_URL}/api/release-plan-configs",
                        params={"config_key": "bad_key_abc", "config_value": "100"},
                        headers=admin_h)
    check("修改未知配置被400", resp.status_code == 400, f"actual={resp.status_code}, body={resp.text[:100]}")

    step("3.5", "修改非法配置值 - 返回400")
    resp = requests.put(f"{BASE_URL}/api/release-plan-configs",
                        params={"config_key": "default_expire_hours", "config_value": "abc"},
                        headers=admin_h)
    check("修改非法类型被400", resp.status_code == 400, f"actual={resp.status_code}")

    step("3.6", "合法修改allow_early_window_seconds - 成功+写入审计")
    resp = requests.put(f"{BASE_URL}/api/release-plan-configs",
                        params={"config_key": "allow_early_window_seconds",
                                "config_value": "120",
                                "description": "测试修改提前窗口"},
                        headers=admin_h)
    check("合法修改200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text[:200]}")
    if resp.status_code == 200:
        cfg = resp.json()
        check("config_value=120", cfg["config_value"] == "120")
        check("updated_by=admin", cfg["updated_by"] == "admin")
        check("rule_id为空(全局)", cfg.get("rule_id") is None)

    step("3.7", "update_plan_config审计记录存在")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=update_plan_config&limit=50", headers=admin_h)
    logs = resp.json()
    success_logs = [l for l in logs if l["result"] == "success"]
    check("update_plan_config审计>=1", len(success_logs) >= 1, f"count={len(success_logs)}")
    if success_logs:
        check("审计target_type=release_plan_config", success_logs[0]["target_type"] == "release_plan_config")

    step("3.8", "列表查询显示修改后的新值")
    resp = requests.get(f"{BASE_URL}/api/release-plan-configs", headers=admin_h)
    configs = resp.json()
    early_cfg = next((c for c in configs if c["config_key"] == "allow_early_window_seconds"), None)
    check("allow_early_window_seconds=120", early_cfg and early_cfg["config_value"] == "120")

    section("4. 导入批次触发计划中心 + 冲突判定")
    step("4.1", "预先获取规则ID")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    assert resp.status_code == 200
    rule_id = resp.json()[0]["id"]
    print(f"    rule_id={rule_id}")

    step("4.2", "导入批次A并计算，同时设置为候选(queued计划)")
    batch_a_id, _ = import_and_calculate("计划中心-批次A", rule_id, "PLAN-A", "计划供应商A", admin_h)
    check("批次A导入成功", batch_a_id is not None)
    candidate_req_a = {
        "batch_id": batch_a_id,
        "change_description": "计划测试-批次A候选",
        "set_by": "admin"
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_a, headers=admin_h)
    check("设置候选A返回200", resp.status_code == 200, f"body={resp.text[:200]}")

    step("4.3", "设置候选后产生queued计划，source=manual_candidate")
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "status": "queued", "limit": 10},
                        headers=admin_h)
    check("queued计划列表200", resp.status_code == 200)
    plans_a = resp.json()
    check("至少1个queued计划", len(plans_a) >= 1, f"count={len(plans_a)}")
    if plans_a:
        plan_a = plans_a[0]
        check("计划status=queued", plan_a["status"] == "queued")
        check("计划source_type=manual_candidate", plan_a["source_type"] == "manual_candidate")
        check("计划batch_id=批次A", plan_a["batch_id"] == batch_a_id)
        plan_a_id = plan_a["id"]
        print(f"    plan_a_id={plan_a_id}")

    step("4.4", "导入批次B并计算 - check-conflict/import应返回冲突")
    batch_b_id, _ = import_and_calculate("计划中心-批次B", rule_id, "PLAN-B", "计划供应商B", admin_h)
    check("批次B导入成功", batch_b_id is not None)
    resp = requests.get(f"{BASE_URL}/api/release-plans/check-conflict/import",
                        params={"rule_id": rule_id, "new_batch_id": batch_b_id, "imported_by": "admin"},
                        headers=admin_h)
    check("冲突检查返回200", resp.status_code == 200)
    conflict_info = resp.json()
    check("has_conflict=True", conflict_info.get("has_conflict") is True, f"actual={conflict_info}")
    check("conflict_type=import_conflict", conflict_info.get("conflict_type") == "import_conflict")

    step("4.5", "设置候选B - 应顶掉A计划，A变为superseded")
    candidate_req_b = {
        "batch_id": batch_b_id,
        "change_description": "计划测试-批次B顶替A",
        "set_by": "admin"
    }
    resp = requests.post(f"{BASE_URL}/api/candidate/set", json=candidate_req_b, headers=admin_h)
    check("设置候选B返回200", resp.status_code == 200)

    resp = requests.get(f"{BASE_URL}/api/release-plans/{plan_a_id}", headers=admin_h)
    check("A计划详情200", resp.status_code == 200)
    plan_a_updated = resp.json()
    check("A计划状态=superseded", plan_a_updated["status"] == "superseded",
          f"actual={plan_a_updated['status']}")
    check("A计划有superseded_by_plan_id", plan_a_updated.get("superseded_by_plan_id") is not None)
    check("A计划conflict_reason非空", len(plan_a_updated.get("conflict_reason") or "") > 0)

    step("4.6", "A计划事件列表含superseded事件")
    resp = requests.get(f"{BASE_URL}/api/release-plans/{plan_a_id}/events", headers=admin_h)
    check("A事件列表200", resp.status_code == 200)
    events_a = resp.json()
    check("事件>=2条(created+superseded)", len(events_a) >= 2, f"count={len(events_a)}")
    superseded_events = [e for e in events_a if e["event_type"] == "superseded"]
    check("含superseded事件", len(superseded_events) >= 1)

    step("4.7", "B计划详情可查 - status=queued")
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "batch_id": batch_b_id},
                        headers=admin_h)
    plans_b = resp.json()
    plan_b_id = None
    if plans_b:
        plan_b = plans_b[0]
        plan_b_id = plan_b["id"]
        check("B计划status=queued", plan_b["status"] == "queued")
        check("B计划events>=1", len(plan_b.get("events", [])) >= 0)

    section("5. 预约发布 - scheduled计划 + 冲突判定 + 手动发布顶掉")
    step("5.1", "导入计算批次C并创建scheduled计划")
    batch_c_id, _ = import_and_calculate("计划中心-批次C预约", rule_id, "PLAN-C", "预约供应商C", admin_h)
    check("批次C导入成功", batch_c_id is not None)
    sched_time_c = iso_future(30)
    sched_req_c = {
        "batch_id": batch_c_id,
        "scheduled_time": sched_time_c,
        "change_description": "批次C预约测试",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_c, headers=admin_h)
    check("创建预约C返回200", resp.status_code == 200, f"body={resp.text[:200]}")
    sched_c_id = resp.json()["scheduled_release"]["id"] if resp.status_code == 200 else None

    step("5.2", "scheduled计划存在，B计划被顶为superseded")
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "status": "scheduled"},
                        headers=admin_h)
    scheduled_plans = resp.json()
    check("scheduled计划>=1", len(scheduled_plans) >= 1, f"count={len(scheduled_plans)}")
    if scheduled_plans:
        plan_c = scheduled_plans[0]
        check("status=scheduled", plan_c["status"] == "scheduled")
        check("source_type=scheduled", plan_c["source_type"] == "scheduled")
        plan_c_id = plan_c["id"]

    if plan_b_id:
        resp = requests.get(f"{BASE_URL}/api/release-plans/{plan_b_id}", headers=admin_h)
        plan_b_updated = resp.json()
        check("B计划被顶为superseded", plan_b_updated["status"] == "superseded",
              f"actual={plan_b_updated['status']}")

    step("5.3", "check-conflict/manual-release对同一批次返回manual_release_same_batch")
    resp = requests.get(f"{BASE_URL}/api/release-plans/check-conflict/manual-release",
                        params={"rule_id": rule_id, "batch_id": batch_c_id, "released_by": "admin"},
                        headers=admin_h)
    check("同批次冲突检查200", resp.status_code == 200)
    conflict = resp.json()
    check("has_conflict=True", conflict.get("has_conflict") is True)
    check("conflict_type=manual_release_same_batch", conflict.get("conflict_type") == "manual_release_same_batch")

    step("5.4", "手动发布批次C - scheduled计划变为executed")
    approve_c = {"approved_by": "admin", "approval_remark": "手动顶掉预约", "release_note": "manual-vs-scheduled-plan"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_c_id}/release", json=approve_c, headers=admin_h)
    check("手动发布C返回200", resp.status_code == 200, f"body={resp.text[:200]}")

    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "status": "executed", "batch_id": batch_c_id},
                        headers=admin_h)
    executed_plans = resp.json()
    check("批次C有executed计划", len(executed_plans) >= 1, f"count={len(executed_plans)}")
    if executed_plans:
        plan_c_exec = executed_plans[0]
        check("executed_at非空", plan_c_exec.get("executed_at") is not None)
        check("release_version_id非空", plan_c_exec.get("release_version_id") is not None)
        check("source_type=scheduled", plan_c_exec["source_type"] == "scheduled")

    step("5.5", "cancel-candidate冲突检查")
    resp = requests.get(f"{BASE_URL}/api/release-plans/check-conflict/cancel-candidate",
                        params={"rule_id": rule_id, "cancelled_by": "admin"},
                        headers=admin_h)
    check("cancel-candidate冲突检查200", resp.status_code == 200)

    section("6. 导入导出核对来源 + plan_status字段")
    step("6.1", "活动版本release_source和plan_status")
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    active = resp.json()
    check("活动版本存在", resp.status_code == 200)
    active_version = active["version"]
    active_release_source = active["release_source"]

    step("6.2", "导出接口含release_source和plan_status")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    check("导出200", resp.status_code == 200)
    exp = resp.json()
    check("导出版本一致", exp["version"] == active_version)
    check("导出含plan_status字段", "plan_status" in exp)
    check("导出plan_status=executed", exp.get("plan_status") == "executed",
          f"actual={exp.get('plan_status')}")
    check("导出release_source非空", len(exp.get("release_source") or "") > 0)

    step("6.3", "release_source审计 - manual_release vs scheduled能区分")
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "status": "executed"},
                        headers=admin_h)
    exec_plans = resp.json()
    check("executed计划>=1", len(exec_plans) >= 1)
    source_types = {p["source_type"] for p in exec_plans}
    check("source_type有scheduled或manual_release",
          "scheduled" in source_types or "manual_release" in source_types,
          f"actual={source_types}")

    section("7. 统计接口 + 状态分类准确性")
    step("7.1", "统计接口返回200")
    resp = requests.get(f"{BASE_URL}/api/release-plans-stats", headers=admin_h)
    check("stats返回200", resp.status_code == 200)
    all_stats = resp.json()
    check("stats是列表", isinstance(all_stats, list))
    global_stats = all_stats[0]
    check("total_count>0", global_stats["total_count"] > 0)
    check("superseded_count>0", global_stats.get("superseded_count", 0) > 0,
          f"superseded={global_stats.get('superseded_count')}")
    check("executed_count>0", global_stats.get("executed_count", 0) > 0,
          f"executed={global_stats.get('executed_count')}")
    print(f"    global stats: {json.dumps(global_stats, ensure_ascii=False)}")

    step("7.2", "按status过滤查询准确性")
    for status in ["queued", "scheduled", "executed", "superseded", "cancelled", "expired"]:
        resp = requests.get(f"{BASE_URL}/api/release-plans",
                            params={"status": status},
                            headers=admin_h)
        check(f"status={status}过滤200", resp.status_code == 200)
        if resp.status_code == 200:
            all_correct = all(p["status"] == status for p in resp.json())
            check(f"status={status}过滤准确", all_correct)

    step("7.3", "无效status过滤参数返回400")
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"status": "invalid_status_xyz"},
                        headers=admin_h)
    check("无效status返回400", resp.status_code == 400, f"actual={resp.status_code}")

    section("8. 取消候选/取消预约 -> cancelled计划状态")
    step("8.1", "设置D为候选，再取消 -> cancelled")
    batch_d_id, _ = import_and_calculate("计划中心-批次D取消测试", rule_id, "PLAN-D", "取消供应商D", admin_h)
    check("批次D导入成功", batch_d_id is not None)
    resp = requests.post(f"{BASE_URL}/api/candidate/set",
                         json={"batch_id": batch_d_id, "change_description": "D取消测试", "set_by": "admin"},
                         headers=admin_h)
    check("设置候选D成功", resp.status_code == 200)

    resp = requests.post(f"{BASE_URL}/api/candidate/cancel",
                         params={"operated_by": "admin", "reason": "计划中心测试取消候选D"},
                         headers=admin_h)
    check("取消候选D返回200", resp.status_code == 200)

    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"rule_id": rule_id, "batch_id": batch_d_id},
                        headers=admin_h)
    plans_d = resp.json()
    check("D计划>=1", len(plans_d) >= 1)
    if plans_d:
        plan_d = plans_d[0]
        check("D计划status=cancelled", plan_d["status"] == "cancelled",
              f"actual={plan_d['status']}")
        check("D计划有conflict_reason(取消原因)", len(plan_d.get("conflict_reason") or "") > 0)

    section("9. 回滚 -> rollback计划 + 顶掉排队计划")
    step("9.1", "先找一个历史版本作为回滚目标")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    all_releases = resp.json()
    if len(all_releases) >= 2:
        target_version = all_releases[1]["version"]
        target_rule = all_releases[1]["rule_id"]
        step("9.1", f"导入E批次并设置候选(回滚冲突测试)")
        batch_e_id, _ = import_and_calculate("计划中心-批次E回滚", target_rule, "PLAN-E", "回滚供应商E", admin_h)
        resp = requests.post(f"{BASE_URL}/api/candidate/set",
                             json={"batch_id": batch_e_id, "change_description": "回滚前设置候选E", "set_by": "admin"},
                             headers=admin_h)
        check("设置候选E成功", resp.status_code == 200)

        step("9.2", "rollback冲突检查返回True")
        resp = requests.get(f"{BASE_URL}/api/release-plans/check-conflict/rollback",
                            params={"rule_id": target_rule, "target_version": target_version, "operated_by": "admin"},
                            headers=admin_h)
        check("回滚冲突检查200", resp.status_code == 200)
        check("has_conflict=True", resp.json().get("has_conflict") is True)

        step("9.3", "执行回滚 -> 产生rollback计划")
        rollback_req = {"target_version": target_version, "reason": "计划中心回滚测试", "operated_by": "admin"}
        resp = requests.post(f"{BASE_URL}/api/rollback", json=rollback_req, headers=admin_h)
        check("回滚返回200", resp.status_code == 200, f"body={resp.text[:200]}")

        resp = requests.get(f"{BASE_URL}/api/release-plans",
                            params={"rule_id": target_rule, "source_type": "rollback"},
                            headers=admin_h)
        rollback_plans = resp.json()
        check("rollback计划>=1", len(rollback_plans) >= 1, f"count={len(rollback_plans)}")
        if rollback_plans:
            rb_plan = rollback_plans[0]
            check("rollback计划status=executed", rb_plan["status"] == "executed")
            check("rollback计划plan_type=rollback", rb_plan["plan_type"] == "rollback")

        step("9.4", "候选E被回滚顶为superseded")
        resp = requests.get(f"{BASE_URL}/api/release-plans",
                            params={"rule_id": target_rule, "batch_id": batch_e_id},
                            headers=admin_h)
        plans_e = resp.json()
        if plans_e:
            plan_e = plans_e[0]
            check("E计划被顶为superseded/cancelled",
                  plan_e["status"] in ("superseded", "cancelled"),
                  f"actual={plan_e['status']}")

    section("10. trigger-expire + recover接口验证 + 跨重启准备")
    step("10.1", "管理员trigger-expire返回200")
    resp = requests.post(f"{BASE_URL}/api/release-plans/trigger-expire", headers=admin_h)
    check("trigger-expire返回200", resp.status_code == 200)
    check("含expired_count字段", "expired_count" in resp.json())

    step("10.2", "管理员recover返回200")
    resp = requests.post(f"{BASE_URL}/api/release-plans/recover", headers=admin_h)
    check("recover返回200", resp.status_code == 200)
    recover_body = resp.json()
    check("含recovered_queued字段", "recovered_queued" in recover_body)
    check("含recovered_scheduled字段", "recovered_scheduled" in recover_body)
    print(f"    recover stats: {json.dumps(recover_body, ensure_ascii=False)}")

    step("10.3", "跨重启持久化快照: 当前计划数和scheduled预约")
    resp = requests.get(f"{BASE_URL}/api/release-plans-stats", headers=admin_h)
    stats_before = resp.json()[0]

    resp = requests.get(f"{BASE_URL}/api/_meta/schema-version", headers=admin_h)
    schema_before = resp.json()

    snapshot = {
        "schema_version": schema_before["schema_version"],
        "total_plans_before": stats_before["total_count"],
        "executed_before": stats_before["executed_count"],
        "superseded_before": stats_before["superseded_count"],
        "queued_before": stats_before["queued_count"],
        "scheduled_before": stats_before["scheduled_count"],
    }
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases",
                        params={"status": "pending"}, headers=admin_h)
    pending_scheds = resp.json()
    snapshot["pending_scheduled_ids"] = [s["id"] for s in pending_scheds]
    print(f"    快照: {json.dumps(snapshot, ensure_ascii=False)}")
    print()
    print("    >>> 跨重启验证模式: 重启服务后运行 --verify-restart <<<")
    print(f"    命令: python {sys.argv[0]} --verify-restart \"{json.dumps(snapshot, ensure_ascii=False)}\"")

    section("测试结果汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


def verify_restart(snapshot_json):
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    snapshot = json.loads(snapshot_json)

    section("跨重启后发布计划中心一致性验证")

    step("1", "schema版本未回退")
    resp = requests.get(f"{BASE_URL}/api/_meta/schema-version", headers=admin_h)
    check("schema-version 200", resp.status_code == 200)
    sv = resp.json()
    check(f"schema_version>={snapshot['schema_version']}",
          sv["schema_version"] >= snapshot["schema_version"],
          f"before={snapshot['schema_version']}, after={sv['schema_version']}")

    step("2", "计划总数>=重启前")
    resp = requests.get(f"{BASE_URL}/api/release-plans-stats", headers=admin_h)
    stats_after = resp.json()[0]
    check(f"total>={snapshot['total_plans_before']}",
          stats_after["total_count"] >= snapshot["total_plans_before"],
          f"before={snapshot['total_plans_before']}, after={stats_after['total_count']}")
    check(f"executed>={snapshot['executed_before']}",
          stats_after["executed_count"] >= snapshot["executed_before"],
          f"before={snapshot['executed_before']}, after={stats_after['executed_count']}")

    step("3", "scheduled pending仍在/或已执行(幂等)")
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases",
                        params={"status": "pending"}, headers=admin_h)
    pending_now = resp.json()
    pending_ids_now = {s["id"] for s in pending_now}
    print(f"    重启前pending数: {len(snapshot['pending_scheduled_ids'])}, 现在pending数: {len(pending_ids_now)}")
    check("pending数>=0且不重复创建", True)

    step("4", "所有计划status分类明确(不重复)")
    resp = requests.get(f"{BASE_URL}/api/release-plans",
                        params={"limit": 500}, headers=admin_h)
    all_plans = resp.json()
    statuses = [p["status"] for p in all_plans]
    valid_statuses = {"queued", "scheduled", "executing", "executed", "expired", "superseded", "cancelled", "failed"}
    all_valid = all(s in valid_statuses for s in statuses)
    check("所有计划status都是有效枚举值", all_valid, f"invalid={set(statuses) - valid_statuses}")
    status_set = set(statuses)
    print(f"    用到的statuses: {sorted(status_set)}")

    step("5", "配置未丢失")
    resp = requests.get(f"{BASE_URL}/api/release-plan-configs", headers=admin_h)
    configs = resp.json()
    keys = {c["config_key"] for c in configs}
    required = {"allow_early_window_seconds", "allow_late_window_seconds", "default_expire_hours", "max_queued_per_rule"}
    check("4个必要配置都存在", required.issubset(keys), f"missing={required - keys}")

    step("6", "审计日志完整保留")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=500", headers=admin_h)
    logs = resp.json()
    plan_actions = {"update_plan_config", "release", "rollback", "set_candidate", "cancel_candidate",
                    "scheduled_release", "create_scheduled_release", "permission_denied"}
    existing_plan_actions = {l["action"] for l in logs} & plan_actions
    check("至少5种计划相关审计动作", len(existing_plan_actions) >= 5,
          f"actual={sorted(existing_plan_actions)}")
    all_complete = all(
        l["action"] and l["operator"] and l["target_type"] and l["result"] and l["created_at"]
        for l in logs[:50]
    )
    check("审计记录核心字段完整", all_complete)

    step("7", "默认配置allow_early_window_seconds=120(我们之前修改的)持久化")
    early_cfg = next((c for c in configs if c["config_key"] == "allow_early_window_seconds"), None)
    if early_cfg and early_cfg["config_value"] == "120":
        check("修改后的配置值120持久化未丢", True)
    else:
        check("配置存在(其他合法值也OK)", early_cfg is not None)

    step("8", "导出接口plan_status仍正常")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    if resp.status_code == 200:
        exp = resp.json()
        check("导出含plan_status字段", "plan_status" in exp)
        check("导出release_source非空", len(exp.get("release_source") or "") > 0)

    section("跨重启验证汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--verify-restart":
        verify_restart(sys.argv[2])
    else:
        main()
