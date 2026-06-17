import requests
import json
import sys
import time
from datetime import datetime, timedelta, timezone

BASE_URL = "http://127.0.0.1:8001"
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

    section("0. 基础准备")
    step("0.1", "获取规则ID，预先导入所有批次")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    assert resp.status_code == 200, f"无法获取规则: {resp.status_code} {resp.text}"
    rule_id = resp.json()[0]["id"]
    print(f"    rule_id = {rule_id}")

    batch_a_id, _ = import_and_calculate("预约-批次A(自动生效)", rule_id, "SCH-A", "预约供应商A", admin_h)
    check("批次A导入计算成功", batch_a_id is not None)
    batch_b_id, _ = import_and_calculate("预约-批次B(顶替测试)", rule_id, "SCH-B", "预约供应商B", approver_h)
    check("批次B导入计算成功", batch_b_id is not None)
    batch_c_id, _ = import_and_calculate("预约-批次C(手动取消测试)", rule_id, "SCH-C", "预约供应商C", admin_h)
    check("批次C导入计算成功", batch_c_id is not None)
    batch_d_id, _ = import_and_calculate("预约-批次D(导入冲突测试)", rule_id, "SCH-D", "预约供应商D", admin_h)
    check("批次D导入计算成功", batch_d_id is not None)
    batch_e_id, _ = import_and_calculate("预约-批次E(手动发布冲突)", rule_id, "SCH-E", "预约供应商E", approver_h)
    check("批次E导入计算成功", batch_e_id is not None)
    batch_persist_id, _ = import_and_calculate("预约-批次P(持久化重启)", rule_id, "SCH-P", "预约供应商P", admin_h)
    check("批次P导入计算成功", batch_persist_id is not None)

    section("1. 预约创建成功与基本字段")
    step("1.1", "创建15秒后生效的预约(批次A)")
    sched_time_a = iso_future(15)
    sched_req = {
        "batch_id": batch_a_id,
        "scheduled_time": sched_time_a,
        "change_description": "批次A预约发布说明",
        "operation_remark": "预约备注-A",
        "release_note": "预约自动生效release note A",
        "approval_remark": "预约审批备注-A",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req, headers=admin_h)
    check("创建预约返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
    sched_a_id = None
    if resp.status_code == 200:
        body = resp.json()
        sched_a = body["scheduled_release"]
        cand_a = body["candidate"]
        sched_a_id = sched_a["id"]
        check("预约状态pending", sched_a["status"] == "pending", f"actual={sched_a['status']}")
        check("预约batch_id正确", sched_a["batch_id"] == batch_a_id)
        check("预约rule_id正确", sched_a["rule_id"] == rule_id)
        check("预约created_by=admin", sched_a["created_by"] == "admin")
        check("候选is_current=True", cand_a["is_current"] is True)
        check("候选expected_effective_time非空", cand_a.get("expected_effective_time") is not None)
        print(f"    sched_a_id = {sched_a_id}, planned_at = {sched_time_a}")

    step("1.2", "查询预约详情")
    if sched_a_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_a_id}", headers=admin_h)
        check("查询详情200", resp.status_code == 200)
        if resp.status_code == 200:
            d = resp.json()
            check("详情含candidate对象", d.get("candidate") is not None)
            check("candidate.batch_id正确", d["candidate"]["batch_id"] == batch_a_id)
            check("详情初始release_version=None", d.get("release_version") is None)

    step("1.3", "最近一次规则预约查询")
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases/rule/{rule_id}/latest", headers=admin_h)
    check("规则最近预约200", resp.status_code == 200)
    if resp.status_code == 200:
        check("最近预约id=sched_a", resp.json()["id"] == sched_a_id)

    step("1.4", "审计：create_scheduled_release + set_candidate 均存在")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=200", headers=admin_h)
    logs = resp.json()
    create_logs = [l for l in logs if l["action"] == "create_scheduled_release" and l["result"] == "success"]
    set_cand_logs = [l for l in logs if l["action"] == "set_candidate" and l["target_type"] == "candidate"]
    check("create_scheduled_release审计>=1", len(create_logs) >= 1)
    check("set_candidate审计存在(预约场景)", len(set_cand_logs) >= 1)

    section("2. 普通角色权限拒绝")
    step("2.1", "普通用户创建预约应被403")
    sched_req_user = {
        "batch_id": batch_b_id,
        "scheduled_time": iso_future(60),
        "change_description": "user尝试预约",
        "set_by": "user1",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_user, headers=user_h)
    check("普通用户创建预约被拒403", resp.status_code == 403, f"actual={resp.status_code}")

    step("2.2", "普通用户取消预约应被403")
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases/{sched_a_id}/cancel?operated_by=user1&reason=非法取消",
                         headers=user_h)
    check("普通用户取消预约被拒403", resp.status_code == 403, f"actual={resp.status_code}")

    step("2.3", "预约A状态未变(仍pending)")
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_a_id}", headers=admin_h)
    check("预约A仍pending", resp.json()["status"] == "pending")

    step("2.4", "permission_denied审计记录存在")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=permission_denied", headers=admin_h)
    logs = resp.json()
    user_denied = [l for l in logs if l["operator"] == "user1" and "scheduled" in l.get("detail", "")]
    check("普通用户预约相关权限拒绝审计存在", len(user_denied) >= 1, f"count={len(user_denied)}")

    section("3. 到点自动生效(幂等)与release_source区分")
    step("3.1", "等待预约A到点自动发布(最多30秒)")
    def sched_a_done():
        r = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_a_id}", headers=admin_h)
        return r.status_code == 200 and r.json()["status"] == "executed"
    ok = wait_until(sched_a_done, timeout_sec=30, poll_sec=2, label="等待自动发布")
    check("预约A到点自动变为executed", ok)

    step("3.2", "预约详情含生成的release_version，且release_source=scheduled")
    if sched_a_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_a_id}", headers=admin_h)
        detail = resp.json()
        check("状态=executed", detail["status"] == "executed")
        check("executed_at非空", detail.get("executed_at") is not None)
        check("release_version_id非空", detail.get("release_version_id") is not None)
        rv = detail.get("release_version")
        check("release_version对象存在", rv is not None)
        check("release_version.release_source=scheduled", rv is not None and rv.get("release_source") == "scheduled",
              f"actual={rv.get('release_source') if rv else None}")

    step("3.3", "活动版本是批次A的预约发布版本，release_source=scheduled")
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    active = resp.json()
    check("活动版本batch_id=批次A", active["batch_id"] == batch_a_id)
    check("活动版本release_source=scheduled", active["release_source"] == "scheduled",
          f"actual={active['release_source']}")

    step("3.4", "导出release_source=scheduled")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    exp = resp.json()
    check("导出release_source=scheduled", exp.get("release_source") == "scheduled",
          f"actual={exp.get('release_source')}")
    check("导出版本与活动版本一致", exp["version"] == active["version"])

    step("3.5", "审计区分手动发布vs预约生效")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=200", headers=admin_h)
    logs = resp.json()
    scheduled_logs = [l for l in logs if l["action"] == "scheduled_release" and l["result"] == "success"]
    manual_release_logs = [l for l in logs if l["action"] == "release" and l["result"] == "success"]
    check("scheduled_release审计存在", len(scheduled_logs) >= 1, f"count={len(scheduled_logs)}")
    check("manual release审计尚无(本次未手动发布)", len(manual_release_logs) == 0,
          f"count={len(manual_release_logs)}")
    if scheduled_logs:
        check("scheduled_release target_type=version", scheduled_logs[0]["target_type"] == "version")
        check("scheduled_release operator=admin", scheduled_logs[0]["operator"] == "admin")
        check("scheduled_release target_id=活动版本号", scheduled_logs[0]["target_id"] == active["version"])

    step("3.6", "幂等: 直接调用调度器逻辑不会重复发布(候选已失效)")
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases", headers=admin_h,
                        params={"status": "pending", "rule_id": rule_id})
    pending_after_exec = [s for s in resp.json() if s["id"] == sched_a_id]
    check("预约A不在pending列表", len(pending_after_exec) == 0)
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    versions = [r for r in resp.json() if r["batch_id"] == batch_a_id]
    check("批次A只生成1个发布版本", len(versions) == 1, f"count={len(versions)}")

    section("4. 冲突场景1 - 候选被新预约顶替")
    step("4.1", "为批次B创建预约(自动顶替批次A的候选，虽然A已执行完，但B是新pending)")
    sched_time_b = iso_future(120)
    sched_req_b = {
        "batch_id": batch_b_id,
        "scheduled_time": sched_time_b,
        "change_description": "B顶替测试预约",
        "set_by": "approver1",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_b, headers=approver_h)
    check("批次B预约创建200", resp.status_code == 200)
    sched_b_id = resp.json()["scheduled_release"]["id"] if resp.status_code == 200 else None

    step("4.2", "再创建批次C预约 - 应使B的pending预约自动失效")
    sched_time_c = iso_future(180)
    sched_req_c = {
        "batch_id": batch_c_id,
        "scheduled_time": sched_time_c,
        "change_description": "C顶替B",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_c, headers=admin_h)
    check("批次C预约创建200", resp.status_code == 200, f"body={resp.text}")
    sched_c_id = resp.json()["scheduled_release"]["id"] if resp.status_code == 200 else None

    step("4.3", "验证预约B已变为cancelled，cancel_reason含'顶替'")
    if sched_b_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_b_id}", headers=admin_h)
        b = resp.json()
        check("预约B已cancelled", b["status"] == "cancelled", f"actual={b['status']}")
        check("预约B取消原因含'顶替'", "顶替" in b.get("cancel_reason", ""),
              f"reason={b.get('cancel_reason')}")
        check("预约B cancelled_by非空", b.get("cancelled_by") is not None)

    step("4.4", "验证预约C仍pending")
    if sched_c_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_c_id}", headers=admin_h)
        check("预约C仍pending", resp.json()["status"] == "pending")

    step("4.5", "候选变更日志含顶替原因")
    resp = requests.get(f"{BASE_URL}/api/candidate/change-log/latest", headers=admin_h)
    latest = resp.json()
    check("最新变更原因含'顶替'或'预约发布'",
          "顶替" in latest["change_reason"] or "预约" in latest["change_reason"],
          f"actual={latest['change_reason']}")

    section("5. 冲突场景2 - 手动取消预约")
    step("5.1", "手动取消预约C")
    if sched_c_id:
        resp = requests.post(
            f"{BASE_URL}/api/scheduled-releases/{sched_c_id}/cancel?operated_by=admin&reason=手动取消预约C测试",
            headers=admin_h,
        )
        check("取消预约C返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
        if resp.status_code == 200:
            body = resp.json()
            check("返回status=cancelled", body["scheduled_release"]["status"] == "cancelled")
            check("cancel_reason含'手动取消'", "手动取消" in body["scheduled_release"]["cancel_reason"],
                  f"actual={body['scheduled_release']['cancel_reason']}")
            check("返回change_log非空(随预约一起取消候选)", body.get("change_log") is not None)

    step("5.2", "预约C详情确认已取消，且当前候选已空")
    if sched_c_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_c_id}", headers=admin_h)
        check("预约C状态=cancelled", resp.json()["status"] == "cancelled")
        check("预约C cancelled_by=admin", resp.json().get("cancelled_by") == "admin")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    check("当前候选为空(404)", resp.status_code == 404)

    step("5.3", "cancel_scheduled_release审计存在")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=cancel_scheduled_release", headers=admin_h)
    logs = resp.json()
    check("cancel_scheduled_release审计>=1", len(logs) >= 1, f"count={len(logs)}")

    section("6. 冲突场景3 - 导入同规则新批次使预约失效")
    step("6.1", "为批次D创建预约")
    sched_time_d = iso_future(120)
    sched_req_d = {
        "batch_id": batch_d_id,
        "scheduled_time": sched_time_d,
        "change_description": "D导入冲突测试",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_d, headers=admin_h)
    check("批次D预约创建200", resp.status_code == 200)
    sched_d_id = resp.json()["scheduled_release"]["id"] if resp.status_code == 200 else None

    step("6.2", "导入同规则新批次(应触发预约D取消)")
    batch_new = {
        "batch_name": "预约-同规则新导入触发冲突",
        "rule_id": rule_id,
        "imported_by": "approver1",
        "suppliers": [
            {"supplier_code": "SCH-CONF-IMPORT", "supplier_name": "导入冲突供应商",
             "metrics": {"pass_rate": 0.95, "defect_rate": 0.02, "on_time_rate": 0.93,
                         "lead_time_days": 14, "price_competitiveness": 85, "payment_terms_score": 72}}
        ],
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch_new, headers=approver_h)
    check("导入同规则新批次200", resp.status_code == 200)

    step("6.3", "预约D已被自动取消，原因含'导入'")
    if sched_d_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_d_id}", headers=admin_h)
        d = resp.json()
        check("预约D状态=cancelled", d["status"] == "cancelled", f"actual={d['status']}")
        check("预约D取消原因含'导入'", "导入" in d.get("cancel_reason", ""),
              f"actual={d.get('cancel_reason')}")

    step("6.4", "当前候选为空")
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    check("候选已空404", resp.status_code == 404)

    step("6.5", "candidate_cleared_on_import审计存在")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?action=candidate_cleared_on_import", headers=admin_h)
    logs = resp.json()
    check("candidate_cleared_on_import审计>=1", len(logs) >= 1, f"count={len(logs)}")

    section("7. 冲突场景4 - 手动正式发布使预约失效")
    step("7.1", "为批次E创建预约")
    sched_time_e = iso_future(120)
    sched_req_e = {
        "batch_id": batch_e_id,
        "scheduled_time": sched_time_e,
        "change_description": "E手动发布冲突测试",
        "set_by": "approver1",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_e, headers=approver_h)
    check("批次E预约创建200", resp.status_code == 200)
    sched_e_id = resp.json()["scheduled_release"]["id"] if resp.status_code == 200 else None

    step("7.2", "手动发布批次E（发布动作应使关联预约自动取消）")
    approve_e = {"approved_by": "admin", "approval_remark": "手动发布触发预约冲突", "release_note": "manual-vs-sched"}
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_e_id}/release", json=approve_e, headers=admin_h)
    check("手动发布批次E返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
    v_manual_version = resp.json()["version"] if resp.status_code == 200 else ""

    step("7.3", "活动版本release_source=manual(与预约生效区分)")
    resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_h)
    check("活动版本release_source=manual", resp.json()["release_source"] == "manual",
          f"actual={resp.json()['release_source']}")
    check("活动版本号=手动发布版本", resp.json()["version"] == v_manual_version)

    step("7.4", "预约E被自动取消，原因含'正式发布'")
    if sched_e_id:
        resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_e_id}", headers=admin_h)
        e = resp.json()
        check("预约E状态=cancelled", e["status"] == "cancelled", f"actual={e['status']}")
        check("预约E取消原因含'发布'", "发布" in e.get("cancel_reason", ""),
              f"actual={e.get('cancel_reason')}")

    step("7.5", "导出release_source=manual")
    resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_h)
    check("导出release_source=manual", resp.json().get("release_source") == "manual")

    step("7.6", "审计中release手动发布与scheduled_release预约同时存在")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=500", headers=admin_h)
    logs = resp.json()
    manual_rel_success = [l for l in logs if l["action"] == "release" and l["result"] == "success"
                          and l["target_id"] == v_manual_version]
    sched_rel_success = [l for l in logs if l["action"] == "scheduled_release" and l["result"] == "success"]
    check("release/success 手动审计存在(按version匹配)", len(manual_rel_success) >= 1,
          f"count={len(manual_rel_success)}")
    check("scheduled_release/success 预约审计存在", len(sched_rel_success) >= 1,
          f"count={len(sched_rel_success)}")
    both_distinct = len(manual_rel_success) >= 1 and len(sched_rel_success) >= 1
    check("手动发布与预约生效在审计中是不同action类型(可区分)", both_distinct)

    section("8. 未计算批次不能预约 + 预约时间不能是过去")
    step("8.1", "导入但未计算批次不能预约")
    batch_raw = {
        "batch_name": "预约-未计算批次",
        "rule_id": rule_id,
        "imported_by": "admin",
        "suppliers": [
            {"supplier_code": "SCH-RAW", "supplier_name": "未计算预约供应商",
             "metrics": {"pass_rate": 0.9}}
        ],
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch_raw, headers=admin_h)
    raw_batch_id = resp.json()["id"]
    bad_req = {
        "batch_id": raw_batch_id,
        "scheduled_time": iso_future(60),
        "change_description": "bad",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=bad_req, headers=admin_h)
    check("未计算预约被拒400", resp.status_code == 400)
    check("错误含'尚未完成计算'", "尚未完成计算" in resp.json().get("detail", ""))

    step("8.2", "预约时间不能是过去")
    batch_past_id, _ = import_and_calculate("预约-过去时间批次", rule_id, "SCH-PAST", "过去时间供应商", admin_h)
    past_req = {
        "batch_id": batch_past_id,
        "scheduled_time": (datetime.utcnow() - timedelta(minutes=5)).replace(microsecond=0).isoformat(),
        "change_description": "bad past",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=past_req, headers=admin_h)
    check("过去时间预约被拒400", resp.status_code == 400)
    check("错误含'必须晚于当前'", "必须晚于当前" in resp.json().get("detail", ""))

    section("9. 服务重启后预约继续生效 + 幂等(跨重启)")
    step("9.1", "创建批次P预约，生效时间在25秒后，并快照数据")
    sched_time_p = iso_future(25)
    sched_req_p = {
        "batch_id": batch_persist_id,
        "scheduled_time": sched_time_p,
        "change_description": "持久化跨重启预约-P",
        "operation_remark": "persist-op",
        "set_by": "admin",
    }
    resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req_p, headers=admin_h)
    check("批次P预约创建200", resp.status_code == 200, f"body={resp.text}")
    sched_p_id = resp.json()["scheduled_release"]["id"] if resp.status_code == 200 else None
    cand_p_id = resp.json()["candidate"]["id"] if resp.status_code == 200 else None

    snapshot = {
        "sched_p_id": sched_p_id,
        "cand_p_id": cand_p_id,
        "batch_persist_id": batch_persist_id,
        "scheduled_time": sched_time_p,
        "release_count_before": len(requests.get(f"{BASE_URL}/api/releases", headers=admin_h).json()),
    }
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_p_id}", headers=admin_h)
    snapshot["sched_status_before"] = resp.json()["status"]
    resp = requests.get(f"{BASE_URL}/api/candidate/current", headers=admin_h)
    snapshot["candidate_batch_before"] = resp.json()["batch_id"] if resp.status_code == 200 else None
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases", headers=admin_h, params={"status": "pending"})
    snapshot["pending_count_before"] = len(resp.json())
    print(f"    快照: {json.dumps(snapshot, ensure_ascii=False)}")
    print()
    print("    >>> 请在此时手动重启测试服务(8001端口)后再次运行 --verify-persist 模式 <<<")
    print(f"    命令: python {sys.argv[0]} --verify-persist \"{json.dumps(snapshot, ensure_ascii=False)}\"")
    print("    注意: 重启后服务会重新加载 pending 预约，到点后应继续自动执行批次P的发布。")
    print("          --verify-persist 模式会等待到点并验证。")

    section("测试结果汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


def verify_persistence(snapshot_json):
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    snapshot = json.loads(snapshot_json)
    sched_p_id = snapshot["sched_p_id"]

    section("服务重启后预约恢复 + 自动生效验证")

    step("1", "重启后预约P记录仍存在且是 pending")
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_p_id}", headers=admin_h)
    check("预约详情200", resp.status_code == 200)
    pre_status = resp.json()["status"]
    # 重启后可能已经到点也可能还没到，都是正常的；这里只记录
    print(f"    重启后预约P当前状态: {pre_status}")
    check("预约P存在(非404)", resp.status_code == 200)
    check("预约batch_id未变", resp.json()["batch_id"] == snapshot["batch_persist_id"])

    step("2", "候选持久化未丢失")
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_p_id}", headers=admin_h)
    d = resp.json()
    check("预约详情含candidate对象", d.get("candidate") is not None or pre_status == "executed")

    step("3", "pending 列表跨重启仍有记录")
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases", headers=admin_h, params={"status": "pending"})
    pending_now = resp.json()
    # 如果已经到点自动执行，就不在 pending 列表了；否则数量应>0
    print(f"    当前pending数量: {len(pending_now)} (重启前: {snapshot['pending_count_before']})")

    step("4", "等待预约P自动执行(最多40秒)，跨重启幂等")
    def p_executed():
        r = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_p_id}", headers=admin_h)
        return r.status_code == 200 and r.json()["status"] == "executed"
    ok = wait_until(p_executed, timeout_sec=40, poll_sec=2, label="等待重启后自动执行")
    check("预约P跨重启最终自动变为executed", ok)

    step("5", "预约P生成的release_source=scheduled，版本唯一(幂等)")
    resp = requests.get(f"{BASE_URL}/api/scheduled-releases/{sched_p_id}", headers=admin_h)
    detail = resp.json()
    if detail["status"] == "executed":
        check("release_version_id非空", detail.get("release_version_id") is not None)
        rv = detail.get("release_version")
        check("release_source=scheduled", rv is not None and rv.get("release_source") == "scheduled",
              f"actual={rv.get('release_source') if rv else None}")
        check("release_version.batch_id=批次P", rv is not None and rv["batch_id"] == snapshot["batch_persist_id"])
        # 幂等: 批次P只有1个发布版本
        resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
        p_versions = [r for r in resp.json() if r["batch_id"] == snapshot["batch_persist_id"]]
        check("批次P幂等只生成1个版本", len(p_versions) == 1, f"count={len(p_versions)}")

    step("6", "scheduled_release 审计记录(跨重启后也写入)")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=500", headers=admin_h)
    logs = resp.json()
    sched_logs_p = [l for l in logs if l["action"] in ("scheduled_release", "create_scheduled_release")
                    and l["target_id"] in (str(sched_p_id), str(snapshot["cand_p_id"]))
                    or (l["action"] == "scheduled_release" and l.get("detail", "") and "P" in l["detail"])]
    # 更精确：查找预约P创建和执行记录
    create_p = [l for l in logs if l["action"] == "create_scheduled_release" and l["target_id"] == str(sched_p_id)]
    exec_p = [l for l in logs if l["action"] == "scheduled_release" and l["result"] == "success"
              and str(snapshot["batch_persist_id"]) in l.get("detail", "")]
    check("create_scheduled_release审计持久化未丢", len(create_p) >= 1, f"count={len(create_p)}")
    check("scheduled_release执行审计存在(跨重启后仍写入)", len(exec_p) >= 1, f"count={len(exec_p)}")

    step("7", "发布总数(重启前后一致+P生成1个新版本)")
    resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_h)
    release_count_after = len(resp.json())
    expected_min = snapshot["release_count_before"] + 1
    check(f"发布总数>=重启前+1(P新增) {release_count_after}>={expected_min}",
          release_count_after >= expected_min)

    step("8", "审计记录字段完整性抽样(scheduled相关)")
    resp = requests.get(f"{BASE_URL}/api/audit-logs?limit=100", headers=admin_h)
    logs = resp.json()
    sched_actions = ("create_scheduled_release", "cancel_scheduled_release", "scheduled_release",
                     "scheduled_release_conflict", "scheduled_release_failed")
    sched_logs = [l for l in logs if l["action"] in sched_actions]
    check("scheduled相关审计>=3条", len(sched_logs) >= 3, f"count={len(sched_logs)}")
    all_complete = all(
        l["action"] and l["operator"] and l["target_type"] and l["target_id"] is not None
        and l["result"] and l["created_at"]
        for l in sched_logs
    )
    check("scheduled相关审计6字段完整", all_complete)

    section("持久化 + 跨重启自动生效验证汇总")
    print(f"    通过: {PASS_COUNT}")
    print(f"    失败: {FAIL_COUNT}")
    if FAIL_COUNT > 0:
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--verify-persist":
        verify_persistence(sys.argv[2])
    else:
        main()
