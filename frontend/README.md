# Traffic 仪表盘前端

Vue 3 + TypeScript + Vite + ECharts 的设备流量统计界面。开发时通过 Vite 代理访问本地 Gin API，因此前端始终请求相对路径 `/api/v1/...`。

## 本地联调

先启动 Gin API，并指向本地受限的基线数据库：

```powershell
cd ../backend
$env:TRAFFIC_STATS_DB = "../docs/baseline/online-2026-07-14T1049Z/files/tmp/traffic-stats-baseline.sqlite3"
$env:TRAFFIC_STATS_LISTEN = "127.0.0.1:8788"
go run -buildvcs=false ./cmd/server
```

另开终端启动前端：

```powershell
npm run dev
```

浏览器访问 `http://127.0.0.1:5173`。如果 Gin API 使用不同端口，可在启动 Vite 前设置 `VITE_API_PROXY`，例如 `http://127.0.0.1:18788`。

## 生产构建

```powershell
npm run build
```

`dist/` 仅用于部署，不纳入 Git；后续由 Gin 在同一回环地址托管。
