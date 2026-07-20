# Traffic 流量统计仪表盘

这是一个面向自托管代理服务的设备流量统计项目。采集器以只读方式读取本地管理面板数据，Gin API 与 Vue 前端提供按设备和时间范围查看流量的仪表盘。

## 项目组成

- `collector.py`：Python 采集器，读取 sing-box 出站的 nftables 双向累计字节，写入本地 SQLite 统计库。
- `backend/`：当前 Gin API、统计聚合逻辑与 systemd 服务单元。
- `frontend/`：Vue 仪表盘前端。
- `deploy/`：版本化发布、健康检查与自动回滚脚本。
- `traffic-stats-collect.service`、`traffic-stats-collect.timer`：采集器 systemd 配置。
- `traffic-stats-nft.service`、`singbox-metering.nft`：为每个 sing-box 认证用户建立独立流量标记与双向计数器。

## sing-box 统计原理

sing-box 1.13 不提供可持久读取的 VLESS 用户累计字节。部署时为每台设备配置一个只用于内部计量的 `direct` 出站，并用 `auth_user` 路由到该出站；这些出站的 Linux `routing_mark` 由 nftables 分别统计。这样不增加端口、不变更客户端链接，也不会按公网 IP 混合统计同一网络下的设备。

历史 3x-ui 的设备复合身份被保留，因此迁移前后的仪表盘数据会继续汇总到相同设备卡片。nftables 计数器在重启或规则重载后从零开始，采集器会将其识别为新计数段，不重复累加旧值。

## 开发与部署

前端与后端的本地开发说明分别位于 [frontend/README.md](frontend/README.md) 与 [backend/README.md](backend/README.md)。自动发布与回滚机制见 [docs/AUTOMATED_DEPLOYMENT.md](docs/AUTOMATED_DEPLOYMENT.md)。

## 公开仓库边界

- 不提交生产数据库、采集快照、SSH 私钥、令牌、主机地址或真实设备名称。
- 自动部署使用 GitHub Actions Secrets 与仓库变量；敏感值不写入工作流或源码。
- 示例、测试夹具和文档中的设备与流量数据均为脱敏或合成数据。
