# 自动发布准备说明

本项目的自动发布分为两个边界：GitHub Actions 只构建并上传产物；VPS 上的根属主脚本负责安装、切换和回滚。这样 GitHub 的专用 SSH 用户不拥有根登录或任意写入程序目录的权限。

## 服务器目录

```text
/var/lib/traffic-stats-deploy/incoming/<commit-sha>/  # trafficdeploy 可写的上传区
/usr/local/lib/traffic-stats-go/releases/<commit-sha>/ # root 管理的不可变发布版本
/usr/local/lib/traffic-stats-go/current                # 当前运行版本的软链接
/usr/local/lib/traffic-stats-go/bin/deploy-release     # 受限 sudo 可调用的发布脚本
```

`traffic-stats-go.service` 始终从 `current` 读取二进制与静态前端。发布脚本先完整解包到新的 release 目录，再原子替换 `current`，重启服务后最多等待十秒，再请求 `/api/v1/status` 确认 API 与数据库文件可用。若启动或该检查失败，脚本会原子地切回前一个 release 并重启服务。采集器短暂滞后不会单独触发代码回滚。

## 首次服务器迁移

在合并包含本文件的 PR 后，以 root 执行以下操作。操作前保留当前已运行的 8788 版本；迁移脚本会将其复制为 `legacy-*` release，因此可回退。

```bash
install -o root -g root -m 0755 deploy/server/deploy-release /usr/local/lib/traffic-stats-go/bin/deploy-release
install -o root -g root -m 0755 deploy/server/bootstrap-release-layout /usr/local/lib/traffic-stats-go/bin/bootstrap-release-layout
install -o root -g root -m 0644 backend/systemd/traffic-stats-go.service /etc/systemd/system/traffic-stats-go.service
visudo -cf deploy/server/traffic-stats-deploy.sudoers
install -o root -g root -m 0440 deploy/server/traffic-stats-deploy.sudoers /etc/sudoers.d/traffic-stats-deploy
/usr/local/lib/traffic-stats-go/bin/bootstrap-release-layout
```

迁移后应检查：

```bash
systemctl is-active traffic-stats-go
curl -fsS http://127.0.0.1:8788/api/v1/status
readlink -f /usr/local/lib/traffic-stats-go/current
```

## 权限边界

- `trafficdeploy` 仅能写入 `incoming`，不能写入 `/usr/local/lib/traffic-stats-go`。
- 该用户仅通过 sudo 调用 `deploy-release <40 位 commit SHA>`；脚本会拒绝其他参数和缺失产物。
- GitHub 保存专用私钥及固定主机公钥，不使用现有 root 私钥。
- 发布脚本保留历史 `releases`，人工回退可将 `current` 指回所需版本后重启 `traffic-stats-go`。

## GitHub Actions 自动发布

`.github/workflows/deploy.yml` 仅在 `main` 收到合并提交时运行；PR 只执行 CI，不会获得部署密钥或连接服务器。工作流会重新运行后端测试、静态检查和前端构建，再将以下短期产物上传至 `incoming/<commit-sha>`：

- `traffic-stats-api`：Linux amd64 Gin 二进制；
- `frontend.tar.gz`：已构建的 Vue 静态文件；
- `checksums.txt`：服务器在 sudo 调用前校验的 SHA-256 清单。

仓库需要下列 Secrets；它们只能在 GitHub 的仓库 Secrets 中保存，不能写入源码、Actions Variables 或日志：

| 类型 | 名称 | 用途 |
| --- | --- | --- |
| Secret | `DEPLOY_SSH_PRIVATE_KEY` | `trafficdeploy` 的专用 Ed25519 私钥 |
| Secret | `DEPLOY_KNOWN_HOSTS` | 已验证的 VPS ED25519 主机公钥 |
| Secret | `DEPLOY_HOST` | VPS 地址 |
| Secret | `DEPLOY_PORT` | SSH 端口 |
| Secret | `DEPLOY_USER` | 固定为 `trafficdeploy` |

工作流使用仓库级并发锁：已开始的部署不会被取消，后续 main 提交会排队，避免两个 release 同时切换 `current`。
