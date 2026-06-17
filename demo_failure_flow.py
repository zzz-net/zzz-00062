import requests
import sys

BASE_URL = "http://127.0.0.1:8000"


def print_section(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_step(step, desc, expect_fail=True):
    print(f"\n  [{step}] {desc}")
    print(f"  预期结果: {'失败并返回错误' if expect_fail else '成功'}")
    print("  " + "-" * 50)


def verify_condition(condition, pass_msg, fail_msg):
    if condition:
        print(f"  [PASS] {pass_msg}")
        return True
    else:
        print(f"  [FAIL] {fail_msg}")
        return False


def main():
    admin_headers = {"X-Username": "admin", "Content-Type": "application/json"}
    approver_headers = {"X-Username": "approver1", "Content-Type": "application/json"}
    user_headers = {"X-Username": "user1", "Content-Type": "application/json"}

    all_passed = True

    print_section("失败链路验证脚本")
    print("验证场景:")
    print("  1. 缺少供应商编号的导入文件必须拒绝")
    print("  2. 导入失败时不能覆盖当前已发布版本")
    print("  3. 同一草稿重复发布不能写出两个版本")
    print("  4. 普通角色不能执行发布")
    print("  5. 普通角色不能执行回滚")

    try:
        print_step("1", "获取活跃规则和当前活动版本信息")
        resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_headers)
        rules = resp.json()
        rule_id = rules[0]["id"]
        print(f"  使用规则 ID: {rule_id}")

        resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_headers)
        if resp.status_code == 200:
            active_before = resp.json()
            print(f"  测试前活动版本: {active_before['version']}")
        else:
            active_before = None
            print(f"  测试前无活动版本")

        print_section("场景一: 缺少供应商编号的导入文件必须拒绝")

        print_step("1.1", "导入缺少供应商编号的批次", expect_fail=True)
        bad_batch_data = {
            "batch_name": "无效测试批次-缺编号",
            "rule_id": rule_id,
            "imported_by": "admin",
            "remark": "用于验证缺少供应商编号的拒绝逻辑",
            "suppliers": [
                {
                    "supplier_code": "",
                    "supplier_name": "测试供应商A",
                    "metrics": {"pass_rate": 0.9}
                },
                {
                    "supplier_code": "SUP-VALID",
                    "supplier_name": "有效供应商",
                    "metrics": {"pass_rate": 0.95}
                }
            ]
        }
        resp = requests.post(f"{BASE_URL}/api/batches/import", json=bad_batch_data, headers=admin_headers)
        status_ok = verify_condition(
            resp.status_code == 400,
            f"返回 HTTP 400 状态码 (实际: {resp.status_code})",
            f"返回 HTTP {resp.status_code}，预期 400"
        )
        all_passed = all_passed and status_ok

        if resp.status_code == 400:
            detail = resp.json().get("detail", "")
            has_error_msg = verify_condition(
                "缺少供应商编号" in detail,
                f"错误信息包含'缺少供应商编号': {detail}",
                f"错误信息未包含预期内容: {detail}"
            )
            all_passed = all_passed and has_error_msg

        print_step("1.2", "全部供应商都缺少编号的情况", expect_fail=True)
        all_bad_batch = {
            "batch_name": "无效测试批次-全缺编号",
            "rule_id": rule_id,
            "imported_by": "admin",
            "suppliers": [
                {"supplier_code": "", "supplier_name": "坏供应商1", "metrics": {"pass_rate": 0.9}},
                {"supplier_code": "", "supplier_name": "坏供应商2", "metrics": {"pass_rate": 0.8}},
            ]
        }
        resp = requests.post(f"{BASE_URL}/api/batches/import", json=all_bad_batch, headers=admin_headers)
        status_ok = verify_condition(
            resp.status_code == 400,
            f"返回 HTTP 400 状态码 (实际: {resp.status_code})",
            f"返回 HTTP {resp.status_code}，预期 400"
        )
        all_passed = all_passed and status_ok

        print_section("场景二: 导入失败时不能覆盖当前已发布版本")

        print_step("2.1", "确认导入失败后活动版本未变")
        resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_headers)
        if active_before:
            if resp.status_code == 200:
                active_after = resp.json()
                version_same = verify_condition(
                    active_after["version"] == active_before["version"],
                    f"活动版本保持不变: {active_after['version']}",
                    f"活动版本被意外改变: {active_before['version']} → {active_after['version']}"
                )
                all_passed = all_passed and version_same
            else:
                version_same = verify_condition(
                    False,
                    "",
                    "活动版本消失了！"
                )
                all_passed = all_passed and version_same
        else:
            print("  (测试前无活动版本，跳过此验证)")

        print_step("2.2", "确认无效批次未被保存到批次列表")
        resp = requests.get(f"{BASE_URL}/api/batches", headers=admin_headers)
        batches = resp.json()
        bad_batches = [b for b in batches if "无效测试" in b["batch_name"]]
        no_bad_batches = verify_condition(
            len(bad_batches) == 0,
            f"无效批次未被保存，共 {len(batches)} 个有效批次",
            f"发现 {len(bad_batches)} 个无效测试批次被意外保存"
        )
        all_passed = all_passed and no_bad_batches

        print_section("场景三: 同一草稿重复发布不能写出两个版本")

        print_step("3.1", "先创建一个有效批次并计算、发布一次")
        valid_batch = {
            "batch_name": "去重测试批次",
            "rule_id": rule_id,
            "imported_by": "admin",
            "remark": "用于验证重复发布拦截",
            "suppliers": [
                {
                    "supplier_code": "SUP-DEDUPE-001",
                    "supplier_name": "去重测试供应商",
                    "metrics": {
                        "pass_rate": 0.95,
                        "defect_rate": 0.02,
                        "on_time_rate": 0.93,
                        "lead_time_days": 14,
                        "price_competitiveness": 85,
                        "payment_terms_score": 70
                    }
                }
            ]
        }
        resp = requests.post(f"{BASE_URL}/api/batches/import", json=valid_batch, headers=admin_headers)
        batch = resp.json()
        batch_id = batch["id"]
        print(f"  批次创建成功，ID: {batch_id}")

        resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/calculate", headers=approver_headers)
        print(f"  计算完成，状态码: {resp.status_code}")

        approve_data = {
            "approved_by": "approver1",
            "approval_remark": "首次发布",
            "release_note": "去重测试版本"
        }
        resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/release", json=approve_data, headers=approver_headers)
        first_release = resp.json()
        first_version = first_release["version"]
        print(f"  首次发布成功，版本: {first_version}")

        print_step("3.2", "尝试对同一批次进行第二次发布", expect_fail=True)
        resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_headers)
        releases_before = resp.json()
        release_count_before = len(releases_before)
        print(f"  第二次发布前版本数量: {release_count_before}")

        approve_data2 = {
            "approved_by": "approver1",
            "approval_remark": "第二次发布尝试",
            "release_note": "重复发布测试"
        }
        resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/release", json=approve_data2, headers=approver_headers)
        status_ok = verify_condition(
            resp.status_code == 400,
            f"返回 HTTP 400 状态码 (实际: {resp.status_code})",
            f"返回 HTTP {resp.status_code}，预期 400"
        )
        all_passed = all_passed and status_ok

        if resp.status_code == 400:
            detail = resp.json().get("detail", "")
            has_error_msg = verify_condition(
                "已发布" in detail or "重复" in detail,
                f"错误信息正确: {detail}",
                f"错误信息未包含预期内容: {detail}"
            )
            all_passed = all_passed and has_error_msg

        print_step("3.3", "验证版本数量没有增加")
        resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_headers)
        releases_after = resp.json()
        release_count_after = len(releases_after)
        count_same = verify_condition(
            release_count_after == release_count_before,
            f"版本数量保持不变: {release_count_after}",
            f"版本数量增加了: {release_count_before} → {release_count_after}"
        )
        all_passed = all_passed and count_same

        print_section("场景四: 普通角色不能执行发布")

        print_step("4.1", "先创建一个新的有效批次用于权限测试")
        perm_batch = {
            "batch_name": "权限测试批次",
            "rule_id": rule_id,
            "imported_by": "user1",
            "remark": "用于验证普通用户发布权限",
            "suppliers": [
                {
                    "supplier_code": "SUP-PERM-001",
                    "supplier_name": "权限测试供应商",
                    "metrics": {
                        "pass_rate": 0.9,
                        "defect_rate": 0.03,
                        "on_time_rate": 0.9,
                        "lead_time_days": 16,
                        "price_competitiveness": 80,
                        "payment_terms_score": 65
                    }
                }
            ]
        }
        resp = requests.post(f"{BASE_URL}/api/batches/import", json=perm_batch, headers=user_headers)
        if resp.status_code == 200:
            batch = resp.json()
            perm_batch_id = batch["id"]
            print(f"  批次创建成功，ID: {perm_batch_id}")

            resp = requests.post(f"{BASE_URL}/api/batches/{perm_batch_id}/calculate", headers=admin_headers)
            print(f"  管理员计算完成，状态码: {resp.status_code}")
        else:
            print(f"  批次创建失败: {resp.status_code} - {resp.json().get('detail', '')}")
            perm_batch_id = None

        print_step("4.2", "普通用户尝试发布", expect_fail=True)
        if perm_batch_id:
            approve_user = {
                "approved_by": "user1",
                "approval_remark": "普通用户尝试发布",
                "release_note": "权限测试"
            }
            resp = requests.post(
                f"{BASE_URL}/api/batches/{perm_batch_id}/release",
                json=approve_user,
                headers=user_headers
            )
            status_ok = verify_condition(
                resp.status_code == 403,
                f"返回 HTTP 403 禁止访问 (实际: {resp.status_code})",
                f"返回 HTTP {resp.status_code}，预期 403"
            )
            all_passed = all_passed and status_ok

            if resp.status_code == 403:
                detail = resp.json().get("detail", "")
                has_perm_msg = verify_condition(
                    "权限" in detail or "角色" in detail,
                    f"权限错误信息正确: {detail}",
                    f"错误信息未提及权限: {detail}"
                )
                all_passed = all_passed and has_perm_msg

            print_step("4.3", "验证普通用户发布失败后版本未增加")
            resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_headers)
            releases_after_perm = resp.json()
            count_still_same = verify_condition(
                len(releases_after_perm) == release_count_before,
                f"版本数量仍为: {len(releases_after_perm)}",
                f"版本数量被意外改变"
            )
            all_passed = all_passed and count_still_same
        else:
            print("  跳过：批次创建失败")

        print_section("场景五: 普通角色不能执行回滚")

        print_step("5.1", "获取历史版本列表用于回滚测试")
        resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_headers)
        all_releases = resp.json()
        if len(all_releases) >= 2:
            current_active = [r for r in all_releases if r["is_active"]][0]
            older_versions = [r for r in all_releases if not r["is_active"]]
            target_version = older_versions[0]["version"]
            print(f"  当前活动版本: {current_active['version']}")
            print(f"  回滚目标版本: {target_version}")

            print_step("5.2", "普通用户尝试回滚", expect_fail=True)
            rollback_data = {
                "target_version": target_version,
                "reason": "普通用户尝试回滚",
                "operated_by": "user1"
            }
            resp = requests.post(f"{BASE_URL}/api/rollback", json=rollback_data, headers=user_headers)
            status_ok = verify_condition(
                resp.status_code == 403,
                f"返回 HTTP 403 禁止访问 (实际: {resp.status_code})",
                f"返回 HTTP {resp.status_code}，预期 403"
            )
            all_passed = all_passed and status_ok

            print_step("5.3", "验证回滚失败后活动版本未变")
            resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_headers)
            still_active = resp.json()
            active_unchanged = verify_condition(
                still_active["version"] == current_active["version"],
                f"活动版本保持不变: {still_active['version']}",
                f"活动版本被意外改变"
            )
            all_passed = all_passed and active_unchanged
        else:
            print("  跳过：版本数量不足2个，无法进行回滚测试")

        print_section("验证结果汇总")
        if all_passed:
            print("\n  [PASS] 所有失败链路验证通过!")
            print("\n验证总结:")
            print("  [PASS] 缺少供应商编号的导入被正确拒绝")
            print("  [PASS] 导入失败时不影响已发布版本")
            print("  [PASS] 同一草稿不能重复发布")
            print("  [PASS] 普通角色不能执行发布")
            print("  [PASS] 普通角色不能执行回滚")
        else:
            print("\n  [FAIL] 部分验证未通过，请检查上面的错误信息")
            sys.exit(1)

    except requests.exceptions.ConnectionError:
        print("\n错误: 无法连接到服务器，请先启动服务:")
        print("  python -m uvicorn app.main:app --reload")
        sys.exit(1)
    except Exception as e:
        print(f"\n发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
