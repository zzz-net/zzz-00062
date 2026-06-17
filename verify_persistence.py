import requests
import sys

BASE_URL = "http://127.0.0.1:8000"


def print_section(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def main():
    admin_headers = {"X-Username": "admin", "Content-Type": "application/json"}

    print_section("服务重启数据一致性验证")
    print("验证: 活动版本、审批备注、历史记录、导出分数在重启后保持一致")

    try:
        print("\n  [1] 获取当前活动版本")
        resp = requests.get(f"{BASE_URL}/api/releases/active", headers=admin_headers)
        if resp.status_code == 200:
            active_release = resp.json()
            print(f"    版本号: {active_release['version']}")
            print(f"    审批人: {active_release['approved_by']}")
            print(f"    审批备注: {active_release['approval_remark']}")
            print(f"    发布说明: {active_release['release_note']}")
            print(f"    供应商数量: {active_release['supplier_count']}")
        else:
            print("    当前没有活动版本")
            active_release = None

        print("\n  [2] 获取版本历史记录")
        resp = requests.get(f"{BASE_URL}/api/releases", headers=admin_headers)
        releases = resp.json()
        print(f"    历史版本数量: {len(releases)}")
        for r in releases:
            status = "[活动]" if r["is_active"] else "      "
            print(f"    {status} {r['version']} - {r['approved_by']} - {r['released_at']}")

        print("\n  [3] 获取回滚历史记录")
        resp = requests.get(f"{BASE_URL}/api/rollback-records", headers=admin_headers)
        records = resp.json()
        print(f"    回滚记录数量: {len(records)}")
        for rec in records:
            print(f"    {rec['operated_at']}")
            print(f"      {rec['from_version']} → {rec['to_version']}")
            print(f"      原因: {rec['reason']}")
            print(f"      操作人: {rec['operated_by']}")

        print("\n  [4] 导出现有评分结果")
        resp = requests.get(f"{BASE_URL}/api/export/active", headers=admin_headers)
        if resp.status_code == 200:
            export_data = resp.json()
            print(f"    导出版本: {export_data['version']}")
            print(f"    供应商数量: {export_data['supplier_count']}")
            print(f"    评分数据:")
            for s in export_data["scores"]:
                print(f"      {s['supplier_code']} - {s['supplier_name']} - {s['total_score']}分 - {s['grade']}级")
        else:
            print("    没有可导出的活动版本")

        print("\n  [5] 查看用户列表")
        resp = requests.get(f"{BASE_URL}/api/users", headers=admin_headers)
        users = resp.json()
        print(f"    用户数量: {len(users)}")
        for u in users:
            print(f"      {u['username']} - 角色: {u['role']}")

        print("\n  [6] 查看评分规则")
        resp = requests.get(f"{BASE_URL}/api/rules", headers=admin_headers)
        rules = resp.json()
        print(f"    规则数量: {len(rules)}")
        for r in rules:
            status = "[启用]" if r['is_active'] else "      "
            print(f"    {status} {r['name']} (ID: {r['id']})")

        print_section("数据一致性验证完成")
        print("\n提示: 请记录以上数据，重启服务后再次运行此脚本")
        print("      对比两次输出，确认数据完全一致。")
        print("\n数据持久化位置: data/supplier_scoring.db")

    except requests.exceptions.ConnectionError:
        print("\n错误: 无法连接到服务器，请先启动服务:")
        print("  python -m uvicorn app.main:app --reload")
        sys.exit(1)


if __name__ == "__main__":
    main()
