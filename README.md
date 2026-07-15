# Traffic 流量统计仪表盘

这是一个面向自托管代理服务的设备流量统计项目。采集器以只读方式读取本地管理面板数据，Gin API 与 Vue 前端提供按设备和时间范围查看流量的仪表盘。

## 项目组成

- `collector.py`：Python 采集器，写入本地 SQLite 统计库。
- `backend/`：当前 Gin API、统计聚合逻辑与 systemd 服务单元。
- `frontend/`：Vue 仪表盘前端。
- `deploy/`：版本化发布、健康检查与自动回滚脚本。
- `traffic-stats-collect.service`、`traffic-stats-collect.timer`：采集器 systemd 配置。

## 开发与部署

前端与后端的本地开发说明分别位于 [frontend/README.md](frontend/README.md) 与 [backend/README.md](backend/README.md)。自动发布与回滚机制见 [docs/AUTOMATED_DEPLOYMENT.md](docs/AUTOMATED_DEPLOYMENT.md)。

## 公开仓库边界

- 不提交生产数据库、采集快照、SSH 私钥、令牌、主机地址或真实设备名称。
- 自动部署使用 GitHub Actions Secrets 与仓库变量；敏感值不写入工作流或源码。
- 示例、测试夹具和文档中的设备与流量数据均为脱敏或合成数据。
