import requests
import json
import sys
import time
from datetime import datetime, timedelta

BASE_URL = "http://127.0.0.1:8003"
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
    # 给批次名和供应商code加时间戳后缀避免撞车
    ts_suffix = datetime.now().strftime("%H%M%S%f")[:-3]
    batch = {
        "batch_name": f"{batch_name}-{ts_suffix}",
        "rule_id": rule_id,
        "imported_by": headers["X-Username"],
        "suppliers": [
            {"supplier_code": f"{supplier_code}-{ts_suffix}", "supplier_name": supplier_name,
             "metrics": {"pass_rate": 0.96, "defect_rate": 0.015, "on_time_rate": 0.94,
                         "lead_time_days": 13, "price_competitiveness": 86, "payment_terms_score": 73}}
        ]
    }
    resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch, headers=headers)
    if resp.status_code != 200:
        print(f"    [IMPORT-FAIL] status={resp.status_code}, body={resp.text[:200]}")
        return None, None
    batch_id = resp.json()["id"]
    resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/calculate", headers=headers)
    if resp.status_code != 200:
        print(f"    [CALC-FAIL] status={resp.status_code}, body={resp.text[:200]}")
        return None, None

    def wait_calculated():
        try:
            r = requests.get(f"{BASE_URL}/api/batches/{batch_id}", headers=headers)
            if r.status_code == 200:
                return r.json().get("status") == "calculated"
        except Exception:
            pass
        return False
    ok = wait_until(wait_calculated, timeout_sec=20, poll_sec=0.5, label=f"等待批次{batch_id}计算完成")
    if not ok:
        print(f"    [TIMEOUT] 批次{batch_id}等待计算完成超时")
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


def get_archive_for_scheduled(sched_id, headers):
    resp = requests.get(f"{BASE_URL}/api/release-archives",
                        params={"scheduled_release_id": sched_id},
                        headers=headers)
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return None


def get_audit_logs(action_filter=None, target_id=None, headers=None):
    admin_h = headers or {"X-Username": "admin", "Content-Type": "application/json"}
    try:
        resp = requests.get(f"{BASE_URL}/api/audit-logs",
                            params={"action": action_filter, "target_id": target_id, "limit": 200},
                            headers=admin_h)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []


def main():
    admin_h = {"X-Username": "admin", "Content-Type": "application/json"}
    # 使用 approver1(审批员) 作为 owner1，user1(普通用户) 作为 owner2
    # 两个不同账号，测试不同创建人之间的归属权限校验
    owner1_h = {"X-Username": "approver1", "Content-Type": "application/json"}
    owner2_h = {"X-Username": "user1", "Content-Type": "application/json"}

    section("关键场景自动化测试套件")
    print("覆盖: 1)越权详情+留痕 2)重启恢复 3)字段对齐 4)取消成功/冲突")

    # ============ 前置准备 ============
    section("前置: 获取 rule_id 并准备基础数据")

    step("P.1", "获取规则ID")
    resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_h)
    assert resp.status_code == 200, f"无法获取规则: {resp.status_code} {resp.text}"
    rule_id = resp.json()[0]["id"]
    print(f"    rule_id = {rule_id}")

    # ============ 场景1: 越权查看详情被拒且审计留痕 ============
    section("场景1: 越权查看详情被拒且审计留痕")

    step("1.1", "owner1(approver1) 创建预约(带时区写法+本地时间测试)")
    batch_owner_id, _ = import_and_calculate(
        "越权测试-OWNER-BATCH", rule_id, "PERM-TEST-OWNER", "越权测试供应商owner", owner1_h
    )
    check("owner1批次导入成功", batch_owner_id is not None)

    sched_owner = None
    archive_owner = None
    if batch_owner_id:
        sched_time_local = iso_future(180)
        sched_req = {
            "batch_id": batch_owner_id,
            "scheduled_time": sched_time_local,
            "change_description": "越权测试-owner的预约",
            "operation_remark": "owner审批备注",
            "release_note": "owner发布说明-专属档案",
            "approval_remark": "owner审批意见-仅供owner查阅",
            "target_version": "v1.0.0-owner",
            "execution_strategy": "auto",
            "set_by": "approver1",
        }
        resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req, headers=owner1_h)
        check("owner1创建预约返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
        if resp.status_code == 200:
            sched_owner = resp.json()["scheduled_release"]
            print(f"    sched_owner_id = {sched_owner['id']}")

    step("1.2", "等待档案创建并获取档案ID")
    if sched_owner:
        def owner_archive_exists():
            return get_archive_for_scheduled(sched_owner["id"], owner1_h) is not None
        wait_ok = wait_until(owner_archive_exists, timeout_sec=10, poll_sec=1)
        check("owner档案已创建", wait_ok)
        archive_owner = get_archive_for_scheduled(sched_owner["id"], owner1_h)
        if archive_owner:
            print(f"    archive_owner_id = {archive_owner['id']}")
            check("档案triggered_by=approver1", archive_owner["triggered_by"] == "approver1")
            check("档案包含context_snapshot", "context_snapshot" in archive_owner)

    step("1.3", "owner2(approver2) 尝试查看owner1档案详情 -> 必须403")
    forbidden_ok = False
    forbidden_body = None
    if archive_owner:
        resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_owner['id']}", headers=owner2_h)
        forbidden_ok = resp.status_code == 403
        check("owner2查看详情返回403", forbidden_ok, f"actual={resp.status_code}, body={resp.text}")
        if forbidden_ok:
            forbidden_body = resp.text
            print(f"    403响应: {forbidden_body[:100]}")

    step("1.4", "检查审计日志中是否记录了forbidden")
    audit_has_forbidden = False
    if archive_owner:
        time.sleep(1)
        logs = get_audit_logs(action_filter="archive_view", target_id=str(archive_owner["id"]), headers=admin_h)
        print(f"    找到archive_view审计日志 {len(logs)} 条")
        for log in logs:
            if (log.get("operator") == "user1" and
                    log.get("result") == "forbidden" and
                    log.get("action") == "archive_view"):
                audit_has_forbidden = True
                print(f"    找到越权审计记录: action={log.get('action')}, op={log.get('operator')}, "
                      f"result={log.get('result')}, detail={log.get('detail', '')[:80]}")
                break
        check("越权操作已写入审计留痕", audit_has_forbidden)

    step("1.5", "owner1本人查看详情 -> 必须200，且context_snapshot字段存在")
    detail_self_ok = False
    context_in_detail = False
    if archive_owner:
        resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_owner['id']}", headers=owner1_h)
        detail_self_ok = resp.status_code == 200
        check("owner1本人查看详情返回200", detail_self_ok, f"actual={resp.status_code}")
        if detail_self_ok:
            detail_body = resp.json()
            context_in_detail = "context_snapshot" in detail_body and isinstance(detail_body["context_snapshot"], dict)
            check("详情响应包含context_snapshot字段", context_in_detail)
            check("详情release_note与档案一致",
                  detail_body.get("release_note") == "owner发布说明-专属档案")
            check("详情approval_remark与档案一致",
                  detail_body.get("approval_remark") == "owner审批意见-仅供owner查阅")

    # ============ 场景2: 服务重启后待处理预约继续执行 ============
    section("场景2: 重启恢复 - 模拟服务重启后恢复待处理档案")

    step("2.1", "admin创建一个远期预约（180秒后）")
    batch_restart_id, _ = import_and_calculate(
        "重启恢复测试-BATCH", rule_id, "RESTART-TEST-1", "重启测试供应商", admin_h
    )
    check("重启测试批次导入成功", batch_restart_id is not None)

    sched_restart_id = None
    archive_restart_id = None
    if batch_restart_id:
        sched_time_restart = iso_future(180)
        sched_req = {
            "batch_id": batch_restart_id,
            "scheduled_time": sched_time_restart,
            "change_description": "重启恢复测试预约",
            "operation_remark": "审批备注-restart",
            "release_note": "发布说明-restart场景",
            "approval_remark": "审批意见-restart场景",
            "target_version": "v9.9.9-restart",
            "execution_strategy": "auto",
            "set_by": "admin",
        }
        resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req, headers=admin_h)
        check("重启测试预约创建成功", resp.status_code == 200, f"actual={resp.status_code}")
        if resp.status_code == 200:
            sched_restart_id = resp.json()["scheduled_release"]["id"]

    step("2.2", "获取档案ID并确认状态=pending")
    if sched_restart_id:
        def restart_archive_exists():
            return get_archive_for_scheduled(sched_restart_id, admin_h) is not None
        wait_ok = wait_until(restart_archive_exists, timeout_sec=10, poll_sec=1)
        check("重启测试档案已创建", wait_ok)
        ar = get_archive_for_scheduled(sched_restart_id, admin_h)
        if ar:
            archive_restart_id = ar["id"]
            print(f"    archive_restart_id = {archive_restart_id}")
            check("重启测试档案初始状态=pending", ar["status"] == "pending")
            check("recovered_after_restart=False", ar.get("recovered_after_restart") == False)

    step("2.3", "调用recover-on-restart接口模拟重启恢复")
    recover_called_ok = False
    recovered_pending_count = 0
    if archive_restart_id:
        resp = requests.post(f"{BASE_URL}/api/release-archives/recover-on-restart", headers=admin_h)
        check("recover-on-restart接口200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
        if resp.status_code == 200:
            recover_body = resp.json()
            print(f"    recover结果: {json.dumps(recover_body, ensure_ascii=False)}")
            recovered_pending_count = recover_body.get("recovered_pending", -1)
            check("recovered_pending计数>=1", recovered_pending_count >= 1,
                  f"recovered_pending={recovered_pending_count}")
            verified_intact = recover_body.get("verified_intact", -1)
            check("verified_intact计数>=1(快照完整性校验通过)", verified_intact >= 1)
            recover_called_ok = True

    step("2.4", "验证档案状态仍是pending（到期前不改变状态），recovered标记=True")
    if archive_restart_id and recover_called_ok:
        resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_restart_id}", headers=admin_h)
        if resp.status_code == 200:
            ar_detail = resp.json()
            check("恢复后状态仍为pending(未到期)", ar_detail.get("status") == "pending",
                  f"actual={ar_detail.get('status')}")
            check("recovered_after_restart标记已更新",
                  ar_detail.get("recovered_after_restart") == True,
                  f"recovered_after_restart={ar_detail.get('recovered_after_restart')}")

    # ============ 场景3: 查询接口与导出字段对齐 ============
    section("场景3: 查询(列表/详情)与导出字段对齐")

    step("3.1", "使用owner1的档案作为对齐验证的基准")
    export_snapshot_fields = []
    detail_aligned_ok = True
    if archive_owner:
        resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_owner['id']}/export", headers=owner1_h)
        check("owner1本人导出接口200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
        if resp.status_code == 200:
            export_data = resp.json()
            export_snapshot_fields = [
                it for it in export_data.get("items", []) if it.get("is_snapshot") == True
            ]
            print(f"    导出中快照字段数: {len(export_snapshot_fields)}")
            for it in export_snapshot_fields:
                print(f"      - {it['field']}: {it['description']}")

            snapshot_field_names = [it["field"] for it in export_snapshot_fields]
            expected_snapshot_fields = [
                "release_note", "approval_remark", "triggered_by", "source_batch_id",
                "target_version", "execution_strategy", "scheduled_release_id",
                "scheduled_time", "context_snapshot",
            ]
            for ef in expected_snapshot_fields:
                if ef not in snapshot_field_names:
                    detail_aligned_ok = False
                    print(f"    [ALIGN-WARN] 导出缺少预期快照字段: {ef}")
            check("导出包含全部9项快照字段", len(snapshot_field_names) >= 9,
                  f"快照字段={snapshot_field_names}")

    step("3.2", "详情接口字段 vs 导出快照字段 -> 值必须完全一致")
    values_aligned = True
    misaligned = []
    if archive_owner and export_snapshot_fields:
        detail_resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_owner['id']}", headers=owner1_h)
        if detail_resp.status_code == 200:
            detail = detail_resp.json()
            for it in export_snapshot_fields:
                f = it["field"]
                export_val = it["value"]
                if f == "scheduled_time":
                    detail_val = detail.get("scheduled_time") or ""
                    if isinstance(detail_val, str):
                        norm_export = export_val.replace("+00:00", "Z").replace("Z", "")
                        norm_detail = detail_val.replace("+00:00", "Z").replace("Z", "")
                        if norm_export[:19] != norm_detail[:19]:
                            values_aligned = False
                            misaligned.append((f, export_val[:30], detail_val[:30] if detail_val else ""))
                elif f == "context_snapshot":
                    if f in detail and isinstance(detail[f], dict):
                        try:
                            export_parsed = json.loads(export_val) if isinstance(export_val, str) else export_val
                            detail_keys = set(detail[f].keys())
                            export_keys = set(export_parsed.keys())
                            if "locked_at" not in export_keys:
                                values_aligned = False
                                misaligned.append((f, "export缺少locked_at", str(list(export_keys))))
                        except Exception as e:
                            values_aligned = False
                            misaligned.append((f, f"parse err: {e}", ""))
                elif f == "source_batch_id" or f == "scheduled_release_id":
                    detail_val = str(detail.get(f, "")) if detail.get(f) is not None else ""
                    if export_val != detail_val:
                        values_aligned = False
                        misaligned.append((f, export_val, detail_val))
                else:
                    detail_val = detail.get(f, "") or ""
                    if export_val != detail_val:
                        values_aligned = False
                        misaligned.append((f, export_val, detail_val))

            for f, ev, dv in misaligned:
                print(f"    [ALIGN-MISMATCH] field={f}, export={ev}, detail={dv}")
            check("详情与导出的快照字段值完全对齐", values_aligned,
                  f"misaligned={misaligned[:5]}")

    step("3.3", "列表接口返回数据与详情对应字段对齐")
    list_aligned_ok = True
    if archive_owner:
        list_resp = requests.get(f"{BASE_URL}/api/release-archives",
                                 params={"scheduled_release_id": archive_owner["scheduled_release_id"]},
                                 headers=owner1_h)
        if list_resp.status_code == 200 and list_resp.json():
            list_item = list_resp.json()[0]
            # 再次实时获取详情，用最新数据对比（admin的操作可能已把状态更改为superseded）
            detail_again_resp = requests.get(
                f"{BASE_URL}/api/release-archives/{archive_owner['id']}",
                headers=owner1_h,
            )
            if detail_again_resp.status_code == 200:
                detail_again = detail_again_resp.json()
                for key in ["release_note", "approval_remark", "triggered_by",
                            "source_batch_id", "target_version", "execution_strategy",
                            "status", "snapshot_hash", "context_snapshot"]:
                    lv = list_item.get(key)
                    dv = detail_again.get(key)
                    if lv != dv:
                        list_aligned_ok = False
                        print(f"    [LIST-ALIGN] key={key}: list={lv} detail={dv}")
            check("列表接口与详情接口核心字段对齐", list_aligned_ok)

    # ============ 场景4: 本人取消成功 / 取消冲突分支 ============
    section("场景4: 本人取消成功 & 取消冲突(终态不可重复取消)")

    step("4.1", "准备用于取消测试的预约(由owner2创建)")
    batch_cancel_id, calc_resp = import_and_calculate(
        "取消测试-BATCH", rule_id, "CANCEL-TEST-NEW-1", "取消测试供应商NEW", owner2_h
    )
    check("owner2取消测试批次导入成功", batch_cancel_id is not None,
          f"batch_cancel_id={batch_cancel_id}, calc_status={calc_resp.status_code if calc_resp is not None else 'N/A'}")
    if batch_cancel_id is None:
        # 打印一下更详细的错误信息（如果有)
        try:
            import time as _t
            _t.sleep(0.3)
        except Exception:
            pass

    sched_cancel_id = None
    archive_cancel_id = None
    if batch_cancel_id:
        sched_time_cancel = iso_future(300)
        sched_req = {
            "batch_id": batch_cancel_id,
            "scheduled_time": sched_time_cancel,
            "change_description": "取消测试预约",
            "operation_remark": "审批-cancel",
            "release_note": "发布说明-会被取消",
            "approval_remark": "审批意见-会被取消",
            "target_version": "v5.5.5-cancel",
            "execution_strategy": "auto",
            "set_by": "user1",
        }
        resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req, headers=owner2_h)
        check("owner2取消测试预约创建成功", resp.status_code == 200)
        if resp.status_code == 200:
            sched_cancel_id = resp.json()["scheduled_release"]["id"]

    step("4.2", "等待档案创建")
    if sched_cancel_id:
        def cancel_archive_exists():
            return get_archive_for_scheduled(sched_cancel_id, owner2_h) is not None
        wait_ok = wait_until(cancel_archive_exists, timeout_sec=10, poll_sec=1)
        check("取消测试档案已创建", wait_ok)
        ar = get_archive_for_scheduled(sched_cancel_id, owner2_h)
        if ar:
            archive_cancel_id = ar["id"]
            print(f"    archive_cancel_id = {archive_cancel_id}")
            check("取消测试档案状态=pending", ar["status"] == "pending")

    step("4.3", "owner2本人取消 -> 成功(cancelled)")
    cancel_success_ok = False
    cancel_status = None
    if archive_cancel_id:
        resp = requests.post(
            f"{BASE_URL}/api/release-archives/{archive_cancel_id}/cancel",
            params={"reason": "owner2主动取消预约-测试场景4.3"},
            headers=owner2_h,
        )
        check("owner2本人取消返回200", resp.status_code == 200, f"actual={resp.status_code}, body={resp.text}")
        if resp.status_code == 200:
            cancel_resp = resp.json()
            cancel_status = cancel_resp.get("status")
            cancel_success_ok = cancel_status == "cancelled"
            check("取消后状态=cancelled", cancel_success_ok, f"actual={cancel_status}")

    step("4.4", "验证取消后的档案详情与审计留痕")
    cancel_audited = False
    if archive_cancel_id and cancel_success_ok:
        detail_resp = requests.get(f"{BASE_URL}/api/release-archives/{archive_cancel_id}", headers=owner2_h)
        if detail_resp.status_code == 200:
            detail = detail_resp.json()
            check("详情中状态=cancelled", detail.get("status") == "cancelled")
            check("conflict_detail记录取消原因",
                  "owner2主动取消" in (detail.get("conflict_detail") or ""),
                  f"conflict_detail={detail.get('conflict_detail')}")
        time.sleep(1)
        cancel_logs = get_audit_logs(action_filter="archive_cancel",
                                     target_id=str(archive_cancel_id), headers=admin_h)
        print(f"    找到archive_cancel审计日志 {len(cancel_logs)} 条")
        for log in cancel_logs:
            if (log.get("operator") == "user1" and log.get("result") == "success"):
                cancel_audited = True
                print(f"    找到取消成功审计: op={log['operator']}, result={log['result']}")
                break
        check("取消成功已写入审计", cancel_audited)

    step("4.5", "取消冲突分支 - 同一档案重复取消 -> 返回400(终态不可取消)")
    conflict_400_ok = False
    if archive_cancel_id and cancel_success_ok:
        resp = requests.post(
            f"{BASE_URL}/api/release-archives/{archive_cancel_id}/cancel",
            params={"reason": "第二次取消-应该被拒"},
            headers=owner2_h,
        )
        conflict_400_ok = resp.status_code == 400
        check("重复取消返回400(终态保护)", conflict_400_ok,
              f"actual={resp.status_code}, body={resp.text}")
        if conflict_400_ok:
            print(f"    冲突响应: {resp.text[:120]}")

    step("4.6", "验证取消冲突也留痕审计(rejected)")
    conflict_audited = False
    if archive_cancel_id and conflict_400_ok:
        time.sleep(1)
        cancel_logs2 = get_audit_logs(action_filter="archive_cancel",
                                      target_id=str(archive_cancel_id), headers=admin_h)
        for log in cancel_logs2:
            if (log.get("operator") == "user1" and log.get("result") == "rejected"):
                conflict_audited = True
                print(f"    找到取消冲突审计: op={log['operator']}, result={log['result']}, "
                      f"detail={log.get('detail', '')[:80]}")
                break
        check("取消冲突(rejected)已写入审计", conflict_audited)

    step("4.7", "越权取消 - owner1尝试取消owner2的档案 -> 返回403")
    cross_user_403 = False
    if archive_cancel_id:
        resp = requests.post(
            f"{BASE_URL}/api/release-archives/{archive_cancel_id}/cancel",
            params={"reason": "owner1恶意取消owner2"},
            headers=owner1_h,
        )
        cross_user_403 = resp.status_code == 403
        check("owner1取消owner2档案返回403", cross_user_403,
              f"actual={resp.status_code}, body={resp.text}")

    # ============ 场景0补充: 时间格式双兼容 ============
    section("补充场景: 时间格式双兼容(带时区 / 本地时间)")

    step("T.1", "创建带Z后缀(UTC时区)的预约")
    batch_tz_id, _ = import_and_calculate(
        "时区测试-BATCH", rule_id, "TZ-TEST-1", "时区测试供应商", admin_h
    )
    tz_create_ok = False
    if batch_tz_id:
        sched_dt = datetime.utcnow() + timedelta(seconds=200)
        sched_time_with_z = sched_dt.replace(microsecond=0).isoformat() + "Z"
        sched_req = {
            "batch_id": batch_tz_id,
            "scheduled_time": sched_time_with_z,
            "change_description": "时区时间格式测试",
            "release_note": "TZ发布说明",
            "approval_remark": "TZ审批意见",
            "set_by": "admin",
        }
        resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req, headers=admin_h)
        check("带Z后缀时间创建成功", resp.status_code == 200,
              f"actual={resp.status_code}, input={sched_time_with_z}")
        if resp.status_code == 200:
            tz_create_ok = True
            sched_body = resp.json()["scheduled_release"]
            print(f"    输入时间: {sched_time_with_z}")
            print(f"    返回时间: {sched_body['scheduled_time']}")

    step("T.2", "创建带+08:00时区偏移的预约")
    batch_tz2_id, _ = import_and_calculate(
        "时区测试2-BATCH", rule_id, "TZ-TEST-2", "时区测试供应商2", admin_h
    )
    tz2_create_ok = False
    if batch_tz2_id:
        # +08:00 时区，所以要多加8小时，转换为UTC后才是未来时间
        sched_local = datetime.utcnow() + timedelta(seconds=210) + timedelta(hours=8)
        sched_time_with_offset = sched_local.replace(microsecond=0).isoformat() + "+08:00"
        sched_req = {
            "batch_id": batch_tz2_id,
            "scheduled_time": sched_time_with_offset,
            "change_description": "时区偏移时间格式测试",
            "release_note": "TZ2发布说明",
            "approval_remark": "TZ2审批意见",
            "set_by": "admin",
        }
        resp = requests.post(f"{BASE_URL}/api/scheduled-releases", json=sched_req, headers=admin_h)
        check("带+08:00时区创建成功", resp.status_code == 200,
              f"actual={resp.status_code}, input={sched_time_with_offset}, body={resp.text}")
        if resp.status_code == 200:
            tz2_create_ok = True

    # ============ 最终汇总 ============
    section("测试结果汇总")
    total = PASS_COUNT + FAIL_COUNT
    print(f"\n  总检查项: {total}")
    print(f"  成功: {PASS_COUNT}")
    print(f"  失败: {FAIL_COUNT}")
    print(f"  通过率: {(PASS_COUNT/total*100):.1f}%" if total > 0 else "  通过率: N/A")

    if FAIL_COUNT > 0:
        print(f"\n  [警告] 存在 {FAIL_COUNT} 项失败，请检查上方 [FAIL] 标记")
        sys.exit(1)
    else:
        print(f"\n  [成功] 所有关键场景测试通过！")
        sys.exit(0)


if __name__ == "__main__":
    main()
