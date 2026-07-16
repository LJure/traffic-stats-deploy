# Gin API

Gin API 读取现有 Python `collector.py` 与 systemd timer 持续写入的 SQLite 数据库；采集器、timer 与数据库表结构是当前生产方案的一部分。

## 本地运行

`C:\\path\\to\\traffic.sqlite3` 是示例占位路径，不是仓库自带文件。生产统计数据库不纳入 Git；要运行完整仪表盘，先复制一份数据库快照到本机并把环境变量指向该实际文件：

```powershell
$devDb = "$env:USERPROFILE\traffic-stats-dev\traffic.sqlite3"
New-Item -ItemType Directory -Force (Split-Path $devDb) | Out-Null
scp -i "<SSH_PRIVATE_KEY_PATH>" root@<VPS_HOST>:/var/lib/traffic-stats/traffic.sqlite3 $devDb
Test-Path $devDb
```

随后在 `backend/` 目录运行：

```powershell
$env:TRAFFIC_STATS_DB = "$env:USERPROFILE\traffic-stats-dev\traffic.sqlite3"
$env:TRAFFIC_STATS_LISTEN = "127.0.0.1:8788"
go run -buildvcs=false ./cmd/server
```

本地文件是复制时刻的只读数据快照；要查看新采集数据，重新执行一次 `scp`。如果只需验证后端逻辑而不启动页面，运行 `go test ./...` 即可，测试会自动创建脱敏临时数据库。

接口：

- `GET /api/v1/status`
- `GET /api/v1/devices`
- `GET /api/v1/dashboard?range=today|week|month|custom&device=<client_id>:<inbound_id>&start=YYYY-MM-DD&end=YYYY-MM-DD&snapshot=<epoch>`

`custom` 必须同时提供 `start` 与 `end`，按北京时间的自然日计算，包含首尾两日；范围不得超过最近 30 个自然日，且不能选择未来日期。`today` 和仅含一天的 `custom` 返回 24 个小时桶；`week`、`month` 与多天 `custom` 返回按日桶。响应中的 `deviceSeries` 同时提供每台设备在相同时间桶内的上、下行数据，供前端的设备曲线视图使用。

`/api/v1/status` 在每次请求时读取采集器落盘的 SQLite 数据库文件元数据，返回 `databaseBytes` 与 `databaseAvailable`，不写回数据库。`healthy` 仅在数据库文件可用且最近一次采集不超过三分钟时为 `true`。

数据库以 SQLite `mode=ro` 打开，并设置 5 秒 busy timeout。生产部署仅监听 `127.0.0.1`，由前置代理提供外部访问。

## systemd 单元

[`systemd/traffic-stats-go.service`](systemd/traffic-stats-go.service) 是生产服务单元，固定监听 `127.0.0.1:8788`，依赖采集服务提供的 SQLite 数据库。
