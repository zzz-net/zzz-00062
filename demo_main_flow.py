import requests
import json
import sys

BASE_URL = "http://127.0.0.1:8000"


def print_section(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_step(step, desc):
    print(f"\n  [{step}] {desc}")
    print("  " + "-" * 40)


def main():
    admin_headers = {"X-Username": "admin", "Content-Type": "application/json"}
    approver_headers = {"X-Username": "approver1", "Content-Type": "application/json"}

    print_section("供应商评分重算服务 - 主流程演示")
    print("演示流程: 查看规则 → 导入批次 → 计算草稿 → 审批发布 → 导出结果 → 回滚验证")

    try:
        print_step("1", "查看当前评分规则")
        resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_headers)
        rules = resp.json()
        print(f"  规则数量: {len(rules)}")
        rule_id = rules[0]["id"]
        print(f"  当前活跃规则 ID: {rule_id}, 名称: {rules[0]['name']}")
        print(f"  规则维度: {list(rules[0]['weight_config']['dimensions'].keys())}")

        print_step("2", "导入供应商批次")
        batch_data = {
            "batch_name": "2024年Q4供应商评分批次",
            "rule_id": rule_id,
            "imported_by": "admin",
            "remark": "第四季度供应商综合评估",
            "suppliers": [
                {
                    "supplier_code": "SUP-001",
                    "supplier_name": "北京华科科技有限公司",
                    "metrics": {
                        "pass_rate": 0.98,
                        "defect_rate": 0.012,
                        "on_time_rate": 0.97,
                        "lead_time_days": 12,
                        "price_competitiveness": 88,
                        "payment_terms_score": 75
                    }
                },
                {
                    "supplier_code": "SUP-002",
                    "supplier_name": "上海盛达实业集团",
                    "metrics": {
                        "pass_rate": 0.92,
                        "defect_rate": 0.025,
                        "on_time_rate": 0.89,
                        "lead_time_days": 18,
                        "price_competitiveness": 92,
                        "payment_terms_score": 80
                    }
                },
                {
                    "supplier_code": "SUP-003",
                    "supplier_name": "深圳创新电子有限公司",
                    "metrics": {
                        "pass_rate": 0.99,
                        "defect_rate": 0.008,
                        "on_time_rate": 0.99,
                        "lead_time_days": 10,
                        "price_competitiveness": 78,
                        "payment_terms_score": 65
                    }
                },
                {
                    "supplier_code": "SUP-004",
                    "supplier_name": "广州明辉制造股份有限公司",
                    "metrics": {
                        "pass_rate": 0.85,
                        "defect_rate": 0.04,
                        "on_time_rate": 0.82,
                        "lead_time_days": 22,
                        "price_competitiveness": 95,
                        "payment_terms_score": 85
                    }
                },
                {
                    "supplier_code": "SUP-005",
                    "supplier_name": "杭州恒信材料科技",
                    "metrics": {
                        "pass_rate": 0.95,
                        "defect_rate": 0.018,
                        "on_time_rate": 0.94,
                        "lead_time_days": 14,
                        "price_competitiveness": 85,
                        "payment_terms_score": 72
                    }
                }
            ]
        }
        resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch_data, headers=admin_headers)
        if resp.status_code != 200:
            print(f"  导入失败: {resp.status_code} - {resp.json()['detail']}")
            sys.exit(1)
        batch = resp.json()
        batch_id = batch["id"]
        print(f"  批次ID: {batch_id}")
        print(f"  批次名称: {batch['batch_name']}")
        print(f"  供应商数量: {batch['supplier_count']}")
        print(f"  状态: {batch['status']}")

        print_step("3", "计算草稿分数")
        resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/calculate", headers=approver_headers)
        if resp.status_code != 200:
            print(f"  计算失败: {resp.status_code} - {resp.json()['detail']}")
            sys.exit(1)
        drafts = resp.json()
        print(f"  计算完成，共 {len(drafts)} 条草稿分数")
        print(f"  {'供应商编号':<12} {'供应商名称':<22} {'总分':<8} {'等级':<6}")
        print(f"  {'-'*12} {'-'*22} {'-'*8} {'-'*6}")
        for d in drafts:
            print(f"  {d['supplier_code']:<12} {d['supplier_name']:<22} {d['total_score']:<8.2f} {d['grade']:<6}")

        print_step("4", "审批并发布版本")
        approve_data = {
            "approved_by": "approver1",
            "approval_remark": "数据完整，计算逻辑正确，同意发布",
            "release_note": "2024年Q4供应商评分正式发布，共5家供应商参与评估"
        }
        resp = requests.post(f"{BASE_URL}/api/batches/{batch_id}/release", json=approve_data, headers=approver_headers)
        if resp.status_code != 200:
            print(f"  发布失败: {resp.status_code} - {resp.json()['detail']}")
            sys.exit(1)
        release = resp.json()
        print(f"  发布成功!")
        print(f"  版本号: {release['version']}")
        print(f"  审批人: {release['approved_by']}")
        print(f"  发布说明: {release['release_note']}")
        print(f"  审批备注: {release['approval_remark']}")
        print(f"  是否活动版本: {release['is_active']}")
        print(f"  发布时间: {release['released_at']}")

        print_step("5", "导出当前生效的评分结果")
        resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_headers)
        if resp.status_code != 200:
            print(f"  导出失败: {resp.status_code} - {resp.json()['detail']}")
            sys.exit(1)
        export_data = resp.json()
        print(f"  导出成功!")
        print(f"  版本: {export_data['version']}")
        print(f"  发布时间: {export_data['released_at']}")
        print(f"  审批人: {export_data['approved_by']}")
        print(f"  供应商数量: {export_data['supplier_count']}")
        print(f"\n  详细评分结果:")
        print(f"  {'供应商编号':<12} {'供应商名称':<22} {'总分':<8} {'等级':<6}")
        print(f"  {'-'*12} {'-'*22} {'-'*8} {'-'*6}")
        for s in export_data["scores"]:
            print(f"  {s['supplier_code']:<12} {s['supplier_name']:<22} {s['total_score']:<8.2f} {s['grade']:<6}")

        print_step("6", "查看版本历史")
        resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_headers)
        releases = resp.json()
        print(f"  历史版本数量: {len(releases)}")
        for r in releases:
            status = "[活动]" if r["is_active"] else "      "
            print(f"    {status} {r['version']} - 批次{r['batch_id']} - {r['approved_by']} - {r['released_at']}")

        print_step("7", "导入第二个批次并发布（用于演示回滚）")
        batch2_data = {
            "batch_name": "2024年Q4供应商评分批次-修正版",
            "rule_id": rule_id,
            "imported_by": "admin",
            "remark": "修正SUP-002数据后的重新评估",
            "suppliers": [
                {
                    "supplier_code": "SUP-001",
                    "supplier_name": "北京华科科技有限公司",
                    "metrics": {
                        "pass_rate": 0.98,
                        "defect_rate": 0.012,
                        "on_time_rate": 0.97,
                        "lead_time_days": 12,
                        "price_competitiveness": 88,
                        "payment_terms_score": 75
                    }
                },
                {
                    "supplier_code": "SUP-002",
                    "supplier_name": "上海盛达实业集团",
                    "metrics": {
                        "pass_rate": 0.96,
                        "defect_rate": 0.015,
                        "on_time_rate": 0.95,
                        "lead_time_days": 15,
                        "price_competitiveness": 92,
                        "payment_terms_score": 80
                    }
                },
                {
                    "supplier_code": "SUP-003",
                    "supplier_name": "深圳创新电子有限公司",
                    "metrics": {
                        "pass_rate": 0.99,
                        "defect_rate": 0.008,
                        "on_time_rate": 0.99,
                        "lead_time_days": 10,
                        "price_competitiveness": 78,
                        "payment_terms_score": 65
                    }
                }
            ]
        }
        resp = requests.post(f"{BASE_URL}/api/batches/import", json=batch2_data, headers=admin_headers)
        batch2 = resp.json()
        batch2_id = batch2["id"]
        print(f"  第二批次导入成功，批次ID: {batch2_id}")

        resp = requests.post(f"{BASE_URL}/api/batches/{batch2_id}/calculate", headers=approver_headers)
        drafts2 = resp.json()
        print(f"  第二批次计算完成，共 {len(drafts2)} 条")

        approve2_data = {
            "approved_by": "approver1",
            "approval_remark": "修正版数据准确，同意发布",
            "release_note": "SUP-002数据修正后的发布版本"
        }
        resp = requests.post(f"{BASE_URL}/api/batches/{batch2_id}/release", json=approve2_data, headers=approver_headers)
        release2 = resp.json()
        version2 = release2["version"]
        print(f"  第二批次发布成功，版本号: {version2}")
        print(f"  当前活动版本: {version2}")

        print_step("8", "回滚到上一版本")
        rollback_data = {
            "target_version": release["version"],
            "reason": "发现修正版数据有误，回滚到原始版本",
            "operated_by": "admin"
        }
        resp = requests.post(f"{BASE_URL}/api/rollback", json=rollback_data, headers=admin_headers)
        if resp.status_code != 200:
            print(f"  回滚失败: {resp.status_code} - {resp.json()['detail']}")
            sys.exit(1)
        rollback_result = resp.json()
        print(f"  回滚成功!")
        print(f"  从版本: {release2['version']}")
        print(f"  到版本: {rollback_result['active_release']['version']}")
        print(f"  回滚原因: {rollback_result['rollback_record']['reason']}")
        print(f"  操作人: {rollback_result['rollback_record']['operated_by']}")

        print_step("9", "验证回滚后导出结果")
        resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_headers)
        export_after = resp.json()
        print(f"  当前活动版本: {export_after['version']}")
        print(f"  供应商数量: {export_after['supplier_count']}")
        print(f"  发布说明: {export_after['release_note']}")

        print_step("10", "查看回滚历史记录")
        resp = requests.get(f"{BASE_URL}/api/rollback-records", headers=admin_headers)
        records = resp.json()
        print(f"  回滚记录数量: {len(records)}")
        for rec in records:
            print(f"    {rec['operated_at']} - {rec['from_version']} → {rec['to_version']}")
            print(f"      原因: {rec['reason']}")
            print(f"      操作人: {rec['operated_by']}")

        print_section("主流程演示完成!")
        print("\n所有步骤均已成功执行:")
        print("  [OK] 评分规则配置")
        print("  [OK] 供应商批次导入")
        print("  [OK] 草稿分数计算")
        print("  [OK] 审批发布")
        print("  [OK] 版本历史")
        print("  [OK] 导出结果")
        print("  [OK] 版本回滚")
        print("  [OK] 回滚记录")
        print("\n数据已持久化保存在 SQLite 数据库中，服务重启后数据保持一致。")

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
