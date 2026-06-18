import requests
import sys
import time
from datetime import datetime, timezone, timedelta

BASE = "http://127.0.0.1:8002"

ADMIN_H = {"X-Username": "admin"}
APPR_H = {"X-Username": "approver1"}
USER1_H = {"X-Username": "user1"}

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"    [PASS] {label}")
    else:
        FAIL += 1
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


def post(path, json=None, headers=None, params=None):
    url = f"{BASE}{path}"
    h = headers or ADMIN_H
    return requests.post(url, json=json, headers=h, params=params, timeout=30)


def get(path, headers=None, params=None):
    url = f"{BASE}{path}"
    h = headers or ADMIN_H
    return requests.get(url, headers=h, params=params, timeout=30)


def _ts():
    return str(int(time.time()))


def iso_future_utc(seconds=3600):
    dt = datetime.utcnow().replace(microsecond=0) + timedelta(seconds=seconds)
    return dt.isoformat()


def get_default_rule_id():
    r = get("/api/rules", ADMIN_H)
    assert r.status_code == 200, f"list rules failed: {r.status_code} {r.text}"
    rules = r.json()
    assert rules, "No rules"
    return rules[0]["id"]


def cancel_current_candidate(operator="admin"):
    try:
        r = post("/api/candidate/cancel", None, ADMIN_H, params={"operated_by": operator, "reason": "test cleanup before new schedule"})
        if r.status_code in (200, 404):
            return True
    except Exception:
        pass
    return False


def setup_batch(rule_id, ts_tag):
    suppliers = [
        {"supplier_code": f"SP_PDR_{ts_tag}_1", "supplier_name": f"PermDrift-A-{ts_tag}",
         "metrics": {"pass_rate": 0.96, "defect_rate": 0.015, "on_time_rate": 0.94,
                      "lead_time_days": 13, "price_competitiveness": 86, "payment_terms_score": 73}},
        {"supplier_code": f"SP_PDR_{ts_tag}_2", "supplier_name": f"PermDrift-B-{ts_tag}",
         "metrics": {"pass_rate": 0.92, "defect_rate": 0.02, "on_time_rate": 0.90,
                      "lead_time_days": 16, "price_competitiveness": 80, "payment_terms_score": 68}},
    ]
    payload = {
        "batch_name": f"perm-drift-{ts_tag}",
        "rule_id": rule_id,
        "imported_by": "admin",
        "suppliers": suppliers,
        "remark": "permission drift regression test",
    }
    r = post("/api/batches/import", payload, ADMIN_H)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"import batch failed: {r.status_code} {r.text}")
    batch_id = r.json()["id"]
    r2 = post(f"/api/batches/{batch_id}/calculate", None, ADMIN_H)
    if r2.status_code != 200:
        raise RuntimeError(f"calc failed: {r2.status_code} {r2.text}")
    return batch_id


def create_scheduled_and_get_archive(batch_id, creator_username, rule_id, ts_tag, note_suffix=""):
    cancel_current_candidate(creator_username)
    payload = {
        "batch_id": batch_id,
        "scheduled_time": iso_future_utc(7200),
        "change_description": f"PermDrift create {ts_tag} {note_suffix}",
        "operation_remark": f"PermDrift approval {ts_tag}",
        "release_note": f"PermDrift release note {ts_tag} {note_suffix}",
        "approval_remark": f"PermDrift approval remark {ts_tag}",
        "target_version": f"vPD-{ts_tag}",
        "execution_strategy": "auto",
        "set_by": creator_username,
    }
    r = post("/api/scheduled-releases", payload, ADMIN_H)
    if r.status_code != 200:
        raise RuntimeError(f"create scheduled failed (set_by={creator_username}): {r.status_code} {r.text}")
    sched = r.json()
    archive_id = sched.get("release_archive_id")
    assert archive_id, f"create should return archive_id, got: {sched}"
    detail = get(f"/api/release-archives/{archive_id}", ADMIN_H)
    if detail.status_code != 200:
        raise RuntimeError(f"get archive {archive_id} failed: {detail.status_code} {detail.text}")
    d = detail.json()
    assert d.get("triggered_by") == creator_username, \
        f"archive triggered_by mismatch: expected {creator_username}, got {d.get('triggered_by')}"
    return archive_id, d


def get_audit_logs(target_id=None, action=None, operator=None, limit=200):
    params = {"limit": limit}
    if action:
        params["action"] = action
    if operator:
        params["operator"] = operator
    params["target_type"] = "release_archive"
    r = get("/api/audit-logs", ADMIN_H, params=params)
    if r.status_code != 200:
        return []
    logs = r.json()
    if target_id is not None:
        logs = [l for l in logs if str(l.get("target_id", "")) == str(target_id)]
    return logs


def find_latest_audit(logs, action, operator=None, result=None):
    for log in logs:
        if log.get("action") != action:
            continue
        if operator and log.get("operator") != operator:
            continue
        if result and log.get("result") != result:
            continue
        return log
    return None


def snapshot_archive_state(archive_id, headers=ADMIN_H):
    detail = get(f"/api/release-archives/{archive_id}", headers)
    if detail.status_code != 200:
        return None
    d = detail.json()
    refs = get(f"/api/release-archives/{archive_id}", headers).json().get("references", [])
    return {
        "reference_count": d.get("reference_count", 0),
        "processing_log_len": len(d.get("processing_log", [])),
        "references_len": len(refs),
        "export_refs": [r for r in refs if r.get("reference_type") == "export"],
    }


def test_user_self_create_export_forbidden():
    section("CASE 1: 普通 user 自己创建档案后再导出 → 必须 403，无副作用，审计 forbidden")
    tag = _ts() + "u1"
    rule_id = get_default_rule_id()

    step("1.1", "用 admin 导入计算批次，以 user1 为 set_by 创建预约发布（档案 triggered_by=user1）")
    batch_id = setup_batch(rule_id, tag)
    archive_id, _ = create_scheduled_and_get_archive(batch_id, "user1", rule_id, tag, "user-self")

    step("1.2", "导出前捕获 reference_count / processing_log / references 快照")
    before = snapshot_archive_state(archive_id)
    assert before is not None, "Failed to snapshot before export"
    print(f"    before: ref_count={before['reference_count']}, plog_len={before['processing_log_len']}, "
          f"refs_len={before['references_len']}")

    step("1.3", "user1 尝试导出自己创建的档案 → 必须 HTTP 403")
    r_export = get(f"/api/release-archives/{archive_id}/export", USER1_H)
    check("HTTP 状态码 == 403", r_export.status_code == 403,
          f"got {r_export.status_code}: {r_export.text[:200]}")
    if r_export.status_code == 403:
        resp_text = r_export.text
        check("403 响应文案包含权限不足相关提示",
              "权限" in resp_text or "不足" in resp_text or "forbidden" in resp_text.lower(),
              f"resp text: {resp_text[:200]}")

    step("1.4", "导出后无副作用：reference_count 未增加，processing_log 未增加，无新 export 引用")
    after = snapshot_archive_state(archive_id)
    check(f"reference_count 不变 (before={before['reference_count']}, after={after['reference_count']})",
          after["reference_count"] == before["reference_count"])
    check(f"processing_log 条数不变 (before={before['processing_log_len']}, after={after['processing_log_len']})",
          after["processing_log_len"] == before["processing_log_len"])
    check(f"export 引用条数为 0 (无新增引用)",
          len(after["export_refs"]) == len(before["export_refs"]) and len(after["export_refs"]) == 0)

    step("1.5", "审计日志必须写入权限拒绝记录（archive_export forbidden 或 permission_denied）")
    logs = get_audit_logs(target_id=str(archive_id), action="archive_export")
    forbidden_log = find_latest_audit(logs, "archive_export", operator="user1", result="forbidden")
    found = forbidden_log is not None
    if not found:
        r_perm = get("/api/audit-logs", ADMIN_H, params={"action": "permission_denied", "limit": 100})
        if r_perm.status_code == 200:
            perm_logs = r_perm.json()
            found = any(
                l.get("operator") == "user1"
                and ("export" in (l.get("detail") or "").lower() or "403" in (l.get("detail") or ""))
                for l in perm_logs
            )
    check("存在权限拒绝审计记录（archive_export/forbidden 或 permission_denied）", found,
          f"archive_export logs: {[(l.get('operator'), l.get('result')) for l in logs if l.get('action')=='archive_export']}")

    step("1.6", "确认同一档案不存在 archive_export result=success 记录（防止业务先执行后鉴权）")
    success_log = find_latest_audit(logs, "archive_export", operator="user1", result="success")
    check("不存在 archive_export result=success 记录", success_log is None,
          f"unexpected success audit: {success_log}")
    return archive_id


def test_permission_matrix_view():
    section("CASE 2: 权限矩阵 - 查看档案（admin / approver非创建人 / 创建人本人 / user非创建人）")
    tag = _ts() + "view"
    rule_id = get_default_rule_id()

    step("2.1", "user1 创建档案（创建人=user1）")
    batch_id = setup_batch(rule_id, tag)
    archive_id, _ = create_scheduled_and_get_archive(batch_id, "user1", rule_id, tag, "matrix-view")

    step("2.2", "admin 查看 → 200")
    r = get(f"/api/release-archives/{archive_id}", ADMIN_H)
    check("admin 查看详情 HTTP 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        d = r.json()
        check("admin 查看到 created_by=user1", d.get("triggered_by") == "user1")

    step("2.3", "approver1 非创建人查看 → 200（README：admin/approver/user 均可查看）")
    r = get(f"/api/release-archives/{archive_id}", APPR_H)
    check("approver 非创建人查看详情 HTTP 200", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")

    step("2.4", "user1（创建人本人）查看 → 200")
    r = get(f"/api/release-archives/{archive_id}", USER1_H)
    check("创建人本人查看详情 HTTP 200", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")

    step("2.5", "列表接口：user1 列表应能看到自己创建的档案")
    r_list = get("/api/release-archives", USER1_H)
    check("user1 档案列表 HTTP 200", r_list.status_code == 200)
    if r_list.status_code == 200:
        ids = [a["id"] for a in r_list.json()]
        check("user1 列表中包含自己创建的档案", archive_id in ids, f"got ids={ids}")
    return archive_id


def test_permission_matrix_cancel():
    section("CASE 3: 权限矩阵 - 取消档案（admin / approver非创建人 / 创建人本人 / user非创建人）")
    tag = _ts() + "cancel"
    rule_id = get_default_rule_id()

    archives = {}

    step("3.1", "为每个角色准备档案（admin 导入计算批次，通过 set_by 指定档案 triggered_by）")
    for role_name, username in [("admin", "admin"), ("approver", "approver1"), ("user", "user1")]:
        bid = setup_batch(rule_id, tag + role_name)
        aid, _ = create_scheduled_and_get_archive(bid, username, rule_id, tag + role_name, f"by-{role_name}")
        archives[role_name] = aid
        print(f"    created archive_by_{role_name} (triggered_by={username}) = {aid}")

    step("3.2", "admin 取消 user1 创建的档案 → 200（admin 可取消任何人）")
    aid_user = archives["user"]
    r = post(f"/api/release-archives/{aid_user}/cancel", None, ADMIN_H, params={"reason": "perm-drift-admin-cancel"})
    check("admin 取消他人档案 HTTP 200", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")

    step("3.3", "approver1 取消 admin 创建的档案 → 403（README：仅 admin 或 创建人本人可取消）")
    aid_admin = archives["admin"]
    before = snapshot_archive_state(aid_admin)
    r = post(f"/api/release-archives/{aid_admin}/cancel", None, APPR_H, params={"reason": "perm-drift-approver-try-cancel"})
    check("approver 非创建人取消 HTTP 403", r.status_code == 403,
          f"{r.status_code} {r.text[:200]}")
    after = snapshot_archive_state(aid_admin)
    check("approver 取消失败后状态未被修改（reference_count 等不变）",
          after["reference_count"] == before["reference_count"]
          and after["processing_log_len"] == before["processing_log_len"])
    logs = get_audit_logs(target_id=str(aid_admin), action="archive_cancel")
    fb = find_latest_audit(logs, "archive_cancel", operator="approver1", result="forbidden")
    check("approver 非创建人取消被记入 forbidden 审计", fb is not None)

    step("3.4", "user1（创建人本人）取消自己创建的档案 → 200（创建人例外）")
    bid_self = setup_batch(rule_id, tag + "selfcancel")
    aid_self, _ = create_scheduled_and_get_archive(bid_self, "user1", rule_id, tag + "selfcancel", "self-cancel")
    r = post(f"/api/release-archives/{aid_self}/cancel", None, USER1_H, params={"reason": "perm-drift-self-cancel"})
    check("创建人本人取消 HTTP 200", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")


def test_permission_matrix_execute():
    section("CASE 4: 权限矩阵 - 手动接管执行（admin / approver非创建人 / 创建人本人）")
    tag = _ts() + "exec"
    rule_id = get_default_rule_id()

    step("4.1", "user1 创建档案（创建人=user1）")
    batch_id = setup_batch(rule_id, tag)
    archive_id, arch_detail = create_scheduled_and_get_archive(batch_id, "user1", rule_id, tag, "matrix-exec")
    check(f"档案初始状态=pending", arch_detail.get("status") == "pending")

    step("4.2", "approver1 非创建人手动接管 → 200（README：admin/approver 或 创建人本人均可执行）")
    r = post(f"/api/release-archives/{archive_id}/execute", None, APPR_H)
    check("approver 非创建人手动接管 HTTP 状态 200/400(若状态已变)",
          r.status_code in (200, 400),
          f"{r.status_code} {r.text[:200]}")

    step("4.3", "用新档案验证创建人本人手动接管权限")
    bid2 = setup_batch(rule_id, tag + "b2")
    aid2, _ = create_scheduled_and_get_archive(bid2, "user1", rule_id, tag + "b2", "creator-exec")
    r = post(f"/api/release-archives/{aid2}/execute", None, USER1_H)
    check("创建人本人手动接管 HTTP 200/400", r.status_code in (200, 400),
          f"{r.status_code} {r.text[:200]}")


def test_permission_matrix_export_creator_exception():
    section("CASE 5: 导出规则的创建人例外不被误伤 - 对比管理员/审批人/创建人")
    tag = _ts() + "exp"
    rule_id = get_default_rule_id()

    step("5.1", "user1 创建档案 A（triggered_by=user1），admin 创建档案 B（triggered_by=admin）")
    batch_a = setup_batch(rule_id, tag + "a")
    aid_a, _ = create_scheduled_and_get_archive(batch_a, "user1", rule_id, tag + "a", "by-user1")
    batch_b = setup_batch(rule_id, tag + "b")
    aid_b, _ = create_scheduled_and_get_archive(batch_b, "admin", rule_id, tag + "b", "by-admin")

    step("5.2", "admin 导出档案 B（自己创建的）→ 200 + 副作用正常")
    before = snapshot_archive_state(aid_b)
    r = get(f"/api/release-archives/{aid_b}/export", ADMIN_H)
    check("admin 导出自己创建的档案 HTTP 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    after = snapshot_archive_state(aid_b)
    if r.status_code == 200:
        check("admin 导出后 reference_count +1", after["reference_count"] == before["reference_count"] + 1)
        check("admin 导出后 processing_log 增加", after["processing_log_len"] > before["processing_log_len"])
        check("admin 导出后出现 export 引用记录", len(after["export_refs"]) > len(before["export_refs"]))

    step("5.3", "approver1 导出档案 A（非创建人）→ 200（README：admin/approver 均可导出，与创建人无关）")
    before_a = snapshot_archive_state(aid_a)
    r = get(f"/api/release-archives/{aid_a}/export", APPR_H)
    check("approver 非创建人导出 HTTP 200", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        after_a = snapshot_archive_state(aid_a)
        check("approver 非创建人导出 reference_count +1", after_a["reference_count"] == before_a["reference_count"] + 1)

    step("5.4", "再次验证 user1（创建人）导出自己的档案 A → 必须 403（README：导出只给 admin/approver，创建人无例外）")
    r = get(f"/api/release-archives/{aid_a}/export", USER1_H)
    check("user1 作为创建人导出自己档案仍返回 403", r.status_code == 403,
          f"{r.status_code} {r.text[:200]}")


def test_recover_after_restart_permission_consistency():
    section("CASE 6: 重启恢复后权限校验仍然一致")
    tag = _ts() + "rec"
    rule_id = get_default_rule_id()

    step("6.1", "user1 创建档案，先验证 user1 导出 403")
    batch_id = setup_batch(rule_id, tag)
    archive_id, _ = create_scheduled_and_get_archive(batch_id, "user1", rule_id, tag, "restart-recover")
    r1 = get(f"/api/release-archives/{archive_id}/export", USER1_H)
    check("重启前 user1 导出 = 403", r1.status_code == 403, f"{r1.status_code} {r1.text[:200]}")

    step("6.2", "触发重启恢复接口 POST /api/release-plans/recover（同时恢复 plans 和 archives")
    r_rec = post("/api/release-plans/recover", None, ADMIN_H)
    check("恢复接口 HTTP 200", r_rec.status_code == 200, f"{r_rec.status_code} {r_rec.text[:200]}")
    print(f"    recovery result: {r_rec.json()}")

    step("6.3", "恢复后再次校验：user1 导出仍然 403")
    r2 = get(f"/api/release-archives/{archive_id}/export", USER1_H)
    check("重启后 user1 导出仍然 = 403", r2.status_code == 403, f"{r2.status_code} {r2.text[:200]}")

    step("6.4", "恢复后 admin 导出仍正常 200，approver 导出仍正常 200")
    r_admin = get(f"/api/release-archives/{archive_id}/export", ADMIN_H)
    check("重启后 admin 导出 = 200", r_admin.status_code == 200, f"{r_admin.status_code} {r_admin.text[:200]}")

    bid2 = setup_batch(rule_id, tag + "b2")
    aid2, _ = create_scheduled_and_get_archive(bid2, "user1", rule_id, tag + "b2", "post-recover")
    r_appr = get(f"/api/release-archives/{aid2}/export", APPR_H)
    check("重启后 approver 非创建人导出 = 200", r_appr.status_code == 200,
          f"{r_appr.status_code} {r_appr.text[:200]}")


def test_audit_trail_admin_only():
    section("CASE 7: 审计链路 /audit-trail 仅 admin 可访问")
    tag = _ts() + "audit"
    rule_id = get_default_rule_id()

    batch_id = setup_batch(rule_id, tag)
    archive_id, _ = create_scheduled_and_get_archive(batch_id, "admin", rule_id, tag, "audit-check")

    r_admin = get(f"/api/release-archives/{archive_id}/audit-trail", ADMIN_H)
    check("admin 访问 audit-trail = 200", r_admin.status_code == 200,
          f"{r_admin.status_code} {r_admin.text[:200]}")

    r_appr = get(f"/api/release-archives/{archive_id}/audit-trail", APPR_H)
    check("approver 访问 audit-trail = 403", r_appr.status_code == 403,
          f"{r_appr.status_code} {r_appr.text[:200]}")

    r_user = get(f"/api/release-archives/{archive_id}/audit-trail", USER1_H)
    check("user 访问 audit-trail = 403", r_user.status_code == 403,
          f"{r_user.status_code} {r_user.text[:200]}")


def main():
    print(f"Target base URL: {BASE}")
    try:
        r = get("/", ADMIN_H)
        print(f"Server reachable: {r.status_code} {r.json()}")
    except Exception as e:
        print(f"Server UNREACHABLE at {BASE}: {e}")
        sys.exit(1)

    try:
        test_user_self_create_export_forbidden()
    except Exception as e:
        print(f"    [ERROR in CASE 1] {e}")
        import traceback
        traceback.print_exc()
        global FAIL
        FAIL += 1

    try:
        test_permission_matrix_view()
    except Exception as e:
        print(f"    [ERROR in CASE 2] {e}")
        FAIL += 1

    try:
        test_permission_matrix_cancel()
    except Exception as e:
        print(f"    [ERROR in CASE 3] {e}")
        FAIL += 1

    try:
        test_permission_matrix_execute()
    except Exception as e:
        print(f"    [ERROR in CASE 4] {e}")
        FAIL += 1

    try:
        test_permission_matrix_export_creator_exception()
    except Exception as e:
        print(f"    [ERROR in CASE 5] {e}")
        FAIL += 1

    try:
        test_recover_after_restart_permission_consistency()
    except Exception as e:
        print(f"    [ERROR in CASE 6] {e}")
        FAIL += 1

    try:
        test_audit_trail_admin_only()
    except Exception as e:
        print(f"    [ERROR in CASE 7] {e}")
        FAIL += 1

    print(f"\n{'='*60}")
    print(f"  SUMMARY: PASS={PASS}, FAIL={FAIL}")
    print(f"{'='*60}")
    if FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
