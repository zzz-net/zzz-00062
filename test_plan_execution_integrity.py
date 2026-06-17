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


def main():
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    approver_h = {"X-Username": "approver1", "Content-Type": "application/json"}
    user_h = {"X-Username": "user1", "Content-Type": "application/json"}

    # ----------------------------------------------------------------
    # SECTION 1: Migration v3 - Early window default is now 120
    # ----------------------------------------------------------------
    section("1. Migration v3 - Early window default is now 120")

    step("1.1", "Check schema version >= 3")
    resp = requests.get(f"{BASE_URL}/api/_meta/schema-version", headers=admin_h)
    check("schema-version接口200", resp.status_code == 200, f"actual={resp.status_code}")
    schema_ver = None
    if resp.status_code == 200:
        body = resp.json()
        schema_ver = body.get("schema_version") or body.get("target_version")
        check("schema version >= 3", schema_ver is not None and schema_ver >= 3,
              f"actual={schema_ver}")
        print(f"    schema_version = {schema_ver}")

    step("1.2", "Check allow_early_window_seconds global default is '120' (not '300')")
    resp = requests.get(f"{BASE_URL}/api/release-plan-configs", headers=admin_h)
    check("plan configs接口200", resp.status_code == 200)
    early_val = None
    if resp.status_code == 200:
        configs = resp.json()
        for c in configs:
            if c.get("config_key") == "allow_early_window_seconds" and c.get("rule_id") is None:
                early_val = c.get("config_value")
                break
        check("allow_early_window_seconds global = '120'", early_val == "120",
              f"actual={early_val}")

    step("1.3", "Verify no contradictory plan data (executed plans should not have cancelled_at)")
    resp = requests.get(f"{BASE_URL}/api/release-plans?status=executed&limit=500", headers=admin_h)
    check("executed plans查询200", resp.status_code == 200)
    if resp.status_code == 200:
        plans = resp.json()
        contradictory = [p for p in plans if p.get("cancelled_at") is not None]
        check("executed计划中cancelled_at均为None", len(contradictory) == 0,
              f"contradictory count={len(contradictory)}")

    # ----------------------------------------------------------------
    # SECTION 2: Early window 120s actual execution test
    # ----------------------------------------------------------------
    section("2. Early window 120s actual execution test")

    step("2.1", "Get rule_id")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    assert resp.status_code == 200, f"无法获取规则: {resp.status_code} {resp.text}"
    rule_id = resp.json()[0]["id"]
    print(f"    rule_id = {rule_id}")

    step("2.2", "Import and calculate batch EARLY-A")
    batch_early_a_id, _ = import_and_calculate(
        "早窗口-批次EARLY-A(120s窗口测试)", rule_id, "EARLY-A", "早窗口供应商A", admin_h
    )
    check("批次EARLY-A导入计算成功", batch_early_a_id is not None)

    step("2.3", "Change early window to 120s (already default, verify)")
    resp = requests.put(
        f"{BASE_URL}/api/release-plan-configs",
        params={"config_key": "allow_early_window_seconds", "config_value": "120",
                "description": "允许提前执行的时间窗口（秒），默认2分钟"},
        headers=admin_h,
    )
    check("设置early window=120返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
    resp = requests.get(f"{BASE_URL}/api/release-plan-configs", headers=admin_h)
    if resp.status_code == 200:
        configs = resp.json()
        for c in configs:
            if c.get("config_key") == "allow_early_window_seconds" and c.get("rule_id") is None:
                early_val = c.get("config_value")
                break
        check("确认early window = '120'", early_val == "120", f"actual={early_val}")

    step("2.4", "Create scheduled release for EARLY-A, scheduled_time = 90 seconds in future")
    sched_time_a = iso_future(90)
    sched_req_a = {
        "batch_id": batch_early_a_id,
        "scheduled_time": sched_time_a,
        "change_description": "EARLY-A早窗口执行测试(90s < 120s)",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_a, headers=admin_h)
    check("创建EARLY-A预约返回200", resp.status_code == 200,
          f"actual={resp.status_code}, body={resp.text}")
    sched_early_a_id = None
    if resp.status_code == 200:
        body = resp.json()
        sched_early_a_id = body["scheduled_release"]["id"]
        check("预约初始状态pending", body["scheduled_release"]["status"] == "pending",
              f"actual={body['scheduled_release']['status']}")
    print(f"    sched_early_a_id = {sched_early_a_id}, scheduled_time = {sched_time_a}")

    step("2.5", "Wait up to 30 seconds - plan should execute within early window (90s < 120s)")
    if sched_early_a_id:
        def early_a_executed():
            r = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_early_a_id}", headers=admin_h)
            return r.status_code == 200 and r.json()["status"] == "executed"
        ok = wait_until(early_a_executed, timeout_sec=30, poll_sec=2,
                        label="等待EARLY-A早窗口自动执行")
        check("EARLY-A在120s早窗口内自动执行(90s<120s)", ok)

    step("2.6", "Verify the scheduled release status = executed")
    if sched_early_a_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_early_a_id}", headers=admin_h)
        detail = resp.json()
        check("scheduled release status = executed", detail["status"] == "executed",
              f"actual={detail['status']}")
        check("executed_at非空", detail.get("executed_at") is not None)

    step("2.7", "Verify release_source = scheduled")
    if sched_early_a_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_early_a_id}", headers=admin_h)
        detail = resp.json()
        rv = detail.get("release_version")
        check("release_source = scheduled", rv is not None and rv.get("release_source") == "scheduled",
              f"actual={rv.get('release_source') if rv else None}")

    step("2.8", "Verify the plan detail status = executed, and cancelled_at is None")
    if sched_early_a_id:
        resp = requests.get(f"{BASE_URL}/api/release-plans", headers=admin_h,
                            params={"status": "executed", "limit": 500})
        plans = resp.json()
        matching = [p for p in plans if p.get("scheduled_release_id") == sched_early_a_id]
        if matching:
            plan = matching[0]
            check("plan status = executed", plan["status"] == "executed",
                  f"actual={plan['status']}")
            check("plan cancelled_at is None", plan.get("cancelled_at") is None,
                  f"actual={plan.get('cancelled_at')}")
        else:
            resp2 = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_early_a_id}", headers=admin_h)
            rv_id = resp2.json().get("release_version_id")
            if rv_id:
                plans_by_rv = [p for p in plans if p.get("release_version_id") == rv_id]
                if plans_by_rv:
                    plan = plans_by_rv[0]
                    check("plan status = executed (by release_version_id)", plan["status"] == "executed")
                    check("plan cancelled_at is None", plan.get("cancelled_at") is None,
                          f"actual={plan.get('cancelled_at')}")
                else:
                    check("找到关联plan记录", False, "无法通过scheduled_release_id或release_version_id定位plan")
            else:
                check("找到关联plan记录", False, "无scheduled_release_id匹配且无release_version_id")

    # ----------------------------------------------------------------
    # SECTION 3: Config change mid-flight
    # ----------------------------------------------------------------
    section("3. Config change mid-flight")

    step("3.1", "Import and calculate batch CONFIG-B")
    batch_config_b_id, _ = import_and_calculate(
        "配置变更-批次CONFIG-B(中途改窗口)", rule_id, "CONFIG-B", "配置变更供应商B", admin_h
    )
    check("批次CONFIG-B导入计算成功", batch_config_b_id is not None)

    step("3.2", "Create scheduled release for CONFIG-B, scheduled_time = 200s in future (outside 120s window)")
    sched_time_b = iso_future(200)
    sched_req_b = {
        "batch_id": batch_config_b_id,
        "scheduled_time": sched_time_b,
        "change_description": "CONFIG-B中途改窗口测试(200s > 120s, 后改为300s)",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_b, headers=admin_h)
    check("创建CONFIG-B预约返回200", resp.status_code == 200,
          f"actual={resp.status_code}, body={resp.text}")
    sched_config_b_id = None
    if resp.status_code == 200:
        sched_config_b_id = resp.json()["scheduled_release"]["id"]
    print(f"    sched_config_b_id = {sched_config_b_id}, scheduled_time = {sched_time_b}")

    step("3.3", "Wait 5 seconds, verify still pending (too far from scheduled time with 120s window)")
    time.sleep(5)
    if sched_config_b_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_config_b_id}", headers=admin_h)
        check("CONFIG-B预约仍pending(200s > 120s窗口)", resp.json()["status"] == "pending",
              f"actual={resp.json()['status']}")

    step("3.4", "Change allow_early_window_seconds to 300s")
    resp = requests.put(
        f"{BASE_URL}/api/release-plan-configs",
        params={"config_key": "allow_early_window_seconds", "config_value": "300",
                "description": "允许提前执行的时间窗口（秒），临时改为300秒用于测试"},
        headers=admin_h,
    )
    check("设置early window=300返回200", resp.status_code == 200,
          f"actual={resp.status_code}, body={resp.text}")

    step("3.5", "Now wait up to 30 seconds - plan should execute (scheduled 200s away, window 300s, earliest = -100s, already past)")
    if sched_config_b_id:
        def config_b_executed():
            r = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_config_b_id}", headers=admin_h)
            return r.status_code == 200 and r.json()["status"] == "executed"
        ok = wait_until(config_b_executed, timeout_sec=30, poll_sec=2,
                        label="等待CONFIG-B窗口扩大后自动执行")
        check("CONFIG-B在300s窗口内自动执行(200s<300s)", ok)

    step("3.6", "Verify scheduled release executed")
    if sched_config_b_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_config_b_id}", headers=admin_h)
        detail = resp.json()
        check("CONFIG-B status = executed", detail["status"] == "executed",
              f"actual={detail['status']}")
        rv = detail.get("release_version")
        check("CONFIG-B release_source = scheduled",
              rv is not None and rv.get("release_source") == "scheduled",
              f"actual={rv.get('release_source') if rv else None}")

    step("3.7", "Reset allow_early_window_seconds back to 120")
    resp = requests.put(
        f"{BASE_URL}/api/release-plan-configs",
        params={"config_key": "allow_early_window_seconds", "config_value": "120",
                "description": "允许提前执行的时间窗口（秒），默认2分钟"},
        headers=admin_h,
    )
    check("重置early window=120返回200", resp.status_code == 200,
          f"actual={resp.status_code}, body={resp.text}")

    # ----------------------------------------------------------------
    # SECTION 4: Cross-restart persistence snapshot
    # ----------------------------------------------------------------
    section("4. Cross-restart persistence snapshot")

    step("4.1", "Import and calculate batch PERSIST-P")
    batch_persist_id, _ = import_and_calculate(
        "持久化-批次PERSIST-P(重启测试)", rule_id, "PERSIST-P", "持久化供应商P", admin_h
    )
    check("批次PERSIST-P导入计算成功", batch_persist_id is not None)

    step("4.2", "Create scheduled release 25 seconds in future")
    sched_time_p = iso_future(25)
    sched_req_p = {
        "batch_id": batch_persist_id,
        "scheduled_time": sched_time_p,
        "change_description": "持久化跨重启预约-P",
        "operation_remark": "persist-op",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_p, headers=admin_h)
    check("PERSIST-P预约创建200", resp.status_code == 200, f"body={resp.text}")
    sched_p_id = None
    cand_p_id = None
    if resp.status_code == 200:
        body = resp.json()
        sched_p_id = body["scheduled_release"]["id"]
        cand_p_id = body["candidate"]["id"]

    step("4.3", "Take snapshot of current state (plan count, scheduled release id, config values)")
    snapshot = {
        "sched_p_id": sched_p_id,
        "cand_p_id": cand_p_id,
        "batch_persist_id": batch_persist_id,
        "scheduled_time": sched_time_p,
        "rule_id": rule_id,
        "release_count_before": len(requests.get(f"{BASE_URL}/api/releases", headers=admin_h).json()),
    }
    if sched_p_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_p_id}", headers=admin_h)
        snapshot["sched_status_before"] = resp.json()["status"]
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    snapshot["candidate_batch_before"] = resp.json()["batch_id"] if resp.status_code == 200 else None
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases", headers=admin_h, params={"status": "pending"})
    snapshot["pending_count_before"] = len(resp.json())
    resp = requests.get(f"{BASE_URL}/api/_meta/schema-version", headers=admin_h)
    if resp.status_code == 200:
        snapshot["schema_version_before"] = resp.json().get("schema_version")
    resp = requests.get(f"{BASE_URL}/api/release-plan-configs", headers=admin_h)
    if resp.status_code == 200:
        snapshot["config_snapshot"] = {
            c["config_key"]: c["config_value"] for c in resp.json() if c.get("rule_id") is None
        }
    resp = requests.get(f"{BASE_URL}/api/release-plans", headers=admin_h, params={"status": "executed"})
    snapshot["executed_plan_count_before"] = len(resp.json()) if resp.status_code == 200 else 0
    print(f"    snapshot = {json.dumps(snapshot, ensure_ascii=False)}")

    step("4.4", "Print restart command with snapshot JSON")
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    print()
    print("    >>> 请在此时手动重启测试服务(8002端口)后再次运行 --verify-restart 模式 <<<")
    print(f"    命令: python {sys.argv[0]} --verify-restart \"{snapshot_json}\"")
    print("    注意: 重启后服务会重新加载 pending 预约，到点后应继续自动执行PERSIST-P的发布。")
    print("          --verify-restart 模式会等待到点并验证。")

    section("测试结果汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


def verify_restart(snapshot_json):
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    snapshot = json.loads(snapshot_json)
    sched_p_id = snapshot["sched_p_id"]

    section("5. Verify restart persistence")

    step("5.1", "Verify schema version preserved")
    resp = requests.get(f"{BASE_URL}/api/_meta/schema-version", headers=admin_h)
    check("schema-version接口200", resp.status_code == 200)
    if resp.status_code == 200:
        body = resp.json()
        schema_ver = body.get("schema_version") or body.get("target_version")
        expected_ver = snapshot.get("schema_version_before")
        if expected_ver is not None:
            check("schema version preserved after restart", schema_ver == expected_ver,
                  f"actual={schema_ver}, expected={expected_ver}")
        else:
            check("schema version >= 3", schema_ver is not None and schema_ver >= 3,
                  f"actual={schema_ver}")

    step("5.2", "Verify plan data intact (scheduled release still exists)")
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_p_id}", headers=admin_h)
    check("预约详情200", resp.status_code == 200)
    if resp.status_code == 200:
        detail = resp.json()
        check("预约batch_id未变", detail["batch_id"] == snapshot["batch_persist_id"],
              f"actual={detail['batch_id']}")
        check("预约rule_id未变", detail["rule_id"] == snapshot["rule_id"],
              f"actual={detail['rule_id']}")
        pre_status = detail["status"]
        print(f"    重启后预约P当前状态: {pre_status}")

    step("5.3", "Verify pending scheduled release executes after restart")
    def p_executed():
        r = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_p_id}", headers=admin_h)
        return r.status_code == 200 and r.json()["status"] == "executed"
    ok = wait_until(p_executed, timeout_sec=40, poll_sec=2, label="等待重启后自动执行")
    check("PERSIST-P跨重启最终自动变为executed", ok)

    step("5.4", "Verify config values persisted")
    resp = requests.get(f"{BASE_URL}/api/release-plan-configs", headers=admin_h)
    check("plan configs接口200", resp.status_code == 200)
    if resp.status_code == 200:
        configs = resp.json()
        config_map = {c["config_key"]: c["config_value"] for c in configs if c.get("rule_id") is None}
        expected_configs = snapshot.get("config_snapshot", {})
        for key, expected_val in expected_configs.items():
            actual_val = config_map.get(key)
            check(f"config {key} persisted = '{expected_val}'", actual_val == expected_val,
                  f"actual={actual_val}")

    step("5.5", "Verify no contradictory plan data after restart")
    resp = requests.get(f"{BASE_URL}/api/release-plans?status=executed&limit=500", headers=admin_h)
    check("executed plans查询200", resp.status_code == 200)
    if resp.status_code == 200:
        plans = resp.json()
        contradictory = [p for p in plans if p.get("cancelled_at") is not None]
        check("executed计划中cancelled_at均为None(重启后)", len(contradictory) == 0,
              f"contradictory count={len(contradictory)}")

    step("5.6", "Verify executed plan count increased by at least 1")
    resp = requests.get(f"{BASE_URL}/api/release-plans", headers=admin_h, params={"status": "executed"})
    executed_count_after = len(resp.json()) if resp.status_code == 200 else 0
    expected_min = snapshot.get("executed_plan_count_before", 0) + 1
    check(f"executed plan count >= {expected_min}", executed_count_after >= expected_min,
          f"actual={executed_count_after}")

    step("5.7", "Verify release count increased by at least 1")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    release_count_after = len(resp.json())
    expected_min = snapshot.get("release_count_before", 0) + 1
    check(f"release count >= {expected_min}", release_count_after >= expected_min,
          f"actual={release_count_after}")

    step("5.8", "Verify PERSIST-P release_source = scheduled")
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_p_id}", headers=admin_h)
    if resp.status_code == 200 and resp.json()["status"] == "executed":
        rv = resp.json().get("release_version")
        check("PERSIST-P release_source = scheduled",
              rv is not None and rv.get("release_source") == "scheduled",
              f"actual={rv.get('release_source') if rv else None}")

    section("重启持久化验证汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--verify-restart":
        verify_restart(sys.argv[2])
    else:
        main()
