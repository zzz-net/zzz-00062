# 供应商评分重算服务 - 预约发布档案（Release Archive）关键行为说明

## 预约发布档案（Release Archive）核心特性

### 1. 快照固化机制（Immutable Snapshot）
创建预约发布时，以下字段会**立即固化为不可变快照**，后续任何操作（详情查询、导出、撤销、手动接管、自动执行）都**只读快照**，不受候选描述变更、运行时备注修改或系统配置调整的影响：

- `release_note`（发布说明）
- `approval_remark`（审批备注）
- `triggered_by`（触发人）
- `source_batch_id`（来源批次）
- `target_version`（目标版本，可选）
- `execution_strategy`（执行策略：`auto`/`manual`/`force`）
- `scheduled_time`（预约时间，统一规范化为UTC存储）
- `scheduled_release_id`（关联预约ID）

快照完整性通过 `snapshot_hash`（SHA-256）校验，可通过 `/api/release-archives/{id}/verify` 接口验证。

### 2. 时区处理规范
时间字段 `scheduled_time` **同时支持带时区和不带时区**的输入，不会因为时区格式直接 500：

- **输入带时区**（如 `2025-06-18T10:00:00+08:00` 或 `...Z`）：自动转换为 UTC 后存储
- **输入不带时区**（如 `2025-06-18T02:00:00`）：直接视为 UTC 存储
- **输出统一**：详情、导出、哈希计算均以 UTC 规范化格式（`isoformat()+"+00:00"`）呈现
- 对同一条预约，不论输入采用何种时区格式，只要绝对时间一致，`snapshot_hash` 必然一致（幂等）

### 3. 状态流转与处理日志
每条档案都具备以下终态/非终态，状态变更会同步写入 `processing_log` 和 `ReleaseArchiveReference`，构成完整审计链路：

| 状态 | 说明 | 是否终态 |
|---|---|---|
| `pending` | 待执行 | 否 |
| `executing` | 执行中 | 否 |
| `executed` | 执行成功 | 是 |
| `cancelled` | 已取消（手动/联动） | 是 |
| `superseded` | 被顶替（导入新批次/手动发布其他批次/回滚） | 是 |
| `failed` | 执行失败 | 是 |

状态流转审计可通过 `/api/release-archives/{id}/audit-trail` 查看，也可通过 `/api/audit-logs?target_type=release_archive` 查询。

### 4. 幂等性保护
- **重复创建预约**：同一 `scheduled_release_id` 只会生成一条档案，后续重复创建幂等跳过
- **重复手动接管执行**：若批次已发布，自动对齐为 `executed` 并标记 `idempotent_aligned=True`，不重复报错
- **终态拒绝二次变更**：已进入终态的档案，再次尝试取消/执行会返回明确错误，并记录审计

### 5. 权限校验与拒绝审计
档案操作权限按角色分层，**无权限操作会记录 `audit_logs`（action=对应操作，result=forbidden）**，并返回 403：

- **查看档案（列表/详情）**：`admin` / `approver` / `user`
- **导出档案**：`admin` / `approver`
- **撤销档案（及联动取消预约）**：仅 `admin` 或 **档案创建人本人**
- **手动接管执行**：`admin` / `approver` 或 **档案创建人本人**
- **审计链路/快照校验**：仅 `admin`

撤销已执行（终态）档案会返回 `400` 并明确提示"档案已处于终态X，不能取消"，同时写入审计日志（result=rejected）。

### 6. 服务重启恢复
服务启动时自动执行 `/api/release-plans/recover` 同等逻辑：
- `pending` / `executing` 档案：校验快照完整性 → 对齐 `scheduled_releases` 状态 → 标记 `recovered_after_restart=True`
- 若预约已执行/已取消：档案自动对齐为对应终态
- 未执行且状态合法的预约：调度器重启后继续扫描，不丢失、不重复

### 7. 导出一致性
`/api/release-archives/{id}/export` 返回结果中：
- `is_snapshot=True` 的 8 个字段（含 `scheduled_time`、`target_version`、`execution_strategy`）**严格等于创建时快照**
- `snapshot_hash` 与档案主记录一致，可独立校验
- 导出后 `reference_count` +1 并写入引用记录
- `processing_log` 包含完整的状态流转和处理日志（含创建、恢复、取消等事件），与详情接口一致

### 8. 手动接管（Manual Takeover）
接口：`POST /api/release-archives/{archive_id}/execute`

- 读取档案快照（而非候选）的 `release_note`、`approval_remark`、`triggered_by` 执行发布
- 支持三种策略：
  - `auto`（默认）：仅当预约状态为 `pending` 且候选有效时执行
  - `manual`：放宽候选有效性校验
  - `force`：无视候选状态、批次状态，强制触发发布
- 执行后版本号、状态、处理日志全部落库并可审计

### 9. 导入冲突识别
接口：`GET /api/release-archives/check-conflict/import?rule_id=&new_batch_id=&imported_by=`

- 导入同规则新批次前，可预检是否存在待执行的档案冲突
- 返回受影响档案的ID、快照哈希、目标版本、执行策略等关键信息
- 实际导入时，冲突档案会被自动标记为 `superseded`（冲突结果=`import_conflict`）

### 10. 时间格式容错
`scheduled_time` 字段同时接受以下输入格式，不会因格式差异返回 500：
- ISO 8601 带时区：`2025-06-18T10:00:00+08:00`
- ISO 8601 Z 后缀：`2025-06-18T02:00:00Z`
- ISO 8601 不带时区（视为 UTC）：`2025-06-18T02:00:00`
- 空格分隔格式：`2025-06-18 02:00:00`
- 无效格式返回 400（含明确错误提示），绝不返回 500

---

**启动方式**：`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`

**自动化验证**：`python test_scheduled_release_archive_full.py`（连接 8002 端口运行）
