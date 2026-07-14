# Gin API（第二阶段）

本目录只替换统计 Web 层；现有 Python `collector.py`、systemd timer 与 SQLite 表结构保持不变。

## 本地运行

```powershell
$env:TRAFFIC_STATS_DB = "..\\docs\\baseline\\online-2026-07-14T1049Z\\files\\tmp\\traffic-stats-baseline.sqlite3"
$env:TRAFFIC_STATS_LISTEN = "127.0.0.1:8788"
go run -buildvcs=false ./cmd/server
```

接口：

- `GET /api/v1/status`
- `GET /api/v1/devices`
- `GET /api/v1/dashboard?range=today|week|month|custom&device=<client_id>:<inbound_id>&start=YYYY-MM-DD&end=YYYY-MM-DD&snapshot=<epoch>`

`custom` 必须同时提供 `start` 与 `end`，按北京时间的自然日计算，包含首尾两日；范围不得超过最近 30 个自然日，且不能选择未来日期。`today` 和仅含一天的 `custom` 返回 24 个小时桶；`week`、`month` 与多天 `custom` 返回按日桶。响应中的 `deviceSeries` 同时提供每台设备在相同时间桶内的上、下行数据，供前端的设备曲线视图使用。

`/api/v1/status` 在每次请求时读取采集器落盘的 SQLite 数据库文件元数据，返回 `databaseBytes` 与 `databaseAvailable`，不写回数据库。`healthy` 仅在数据库文件可用且最近一次采集不超过三分钟时为 `true`。

数据库以 SQLite `mode=ro` 打开，并设置 5 秒 busy timeout。生产部署仍须只监听 `127.0.0.1`；静态前端托管和 systemd 单元将在后续阶段加入。

## 预备 systemd 单元

[`systemd/traffic-stats-go.service`](systemd/traffic-stats-go.service) 是灰度服务单元：它固定监听 `127.0.0.1:8788`，不会占用现有 Python 页面使用的 `8787`。本阶段只提交该文件，不会安装、启用或重启任何 VPS 服务。
