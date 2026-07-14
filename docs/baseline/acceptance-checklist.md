# Gin 迁移：行为验收清单

基准快照 `online-2026-07-14T1049Z` 是本地保留的受限数据，不纳入 Git 仓库。每项应在 Gin 监听 `127.0.0.1:8788` 的灰度阶段自动验证。

## API 契约

| 端点 | 必需行为 |
| --- | --- |
| `GET /api/v1/status` | 返回最近成功采集的 epoch、下次采集 epoch 与服务可用状态。 |
| `GET /api/v1/devices` | 每个 `(client_id, inbound_id)` 返回一项，并携带最新 `email` 和 `label`。 |
| `GET /api/v1/dashboard` | 接受 `range=today|week|month`、可选 `device=<client_id>:<inbound_id>`；返回结构化 JSON，不返回 HTML。 |

## 必须保持一致的计算

- 全部设备时，区间上传、下载和总量等于相同日期范围内 `daily_usage` 的总和；当天实时部分可由分钟样本回算。
- 单设备筛选仅影响对应 `(client_id, inbound_id)`，不能按名称模糊匹配。
- `today` 返回 24 个北京时间小时桶；`week` 为 7 个日期桶；`month` 为 30 个日期桶，缺失日期补零。
- 当前速率只由每个已选设备最近两次成功分钟样本的正向字节增量除以时间差计算。
- 计数器回退时，增量是新计数值，不得出现负流量。
- 非法时间范围或未知设备遵循旧版回退语义：回退到当天或全部设备。

## 灰度验收

1. 以快照数据库运行 Gin 单元/集成测试，并对照快照 README 的日总量。
2. 对线上同一 `snapshot` 时间点分别请求旧 `8787` 和新 `8788`，比较区间总量、上传、下载、设备排行和图表桶。
3. 采集器执行前后各检查一次：Gin 返回 200、无 `database is locked`、`last_success` 能更新。
4. 前端通过 Cloudflare Access 登录后，检查根页面、范围切换、设备筛选和移动端布局。
5. 切换 Tunnel 后保留旧 `traffic-stats-web.service`，直到至少一个完整采集周期和公网验证均通过。
