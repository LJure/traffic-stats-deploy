# 协作与发布工作流

## 分支责任

| 操作者 | 分支格式 | 用途 |
| --- | --- | --- |
| Codex | `codex/feat/*`、`codex/fix/*`、`codex/chore/*` | AI 协助实现、修复与维护 |
| LJure | `user/feat/*`、`user/fix/*`、`user/hotfix/*` | 人工实现、修复与紧急处理 |
| 共享 | `main` | 只保存已审阅、可部署的合并结果 |

## 提交身份

本仓库仅在分支名匹配 `codex/**` 时自动启用以下本地 Git 身份：

```text
Codex (AI-assisted) <52845776+LJure@users.noreply.github.com>
```

这让提交历史的作者名称明确标明 Codex，同时仍使用 LJure 已关联的 noreply 邮箱。`main` 和 `user/**` 分支会回退到 LJure 的全局 Git 身份，避免人工提交被误标为 Codex。它不会创建独立 GitHub Contributors 头像；该需求需要单独的 Bot 账号与独立凭据。

每个 Codex 提交正文应包含：

```text
Actor: Codex
Validation: <实际执行的验证命令或检查>
```

人工提交使用 `Actor: LJure`。不要伪造对方的提交身份。

## 合并规则

1. 不直接推送 `main`。
2. Codex 先创建并推送 `codex/*` 分支，再创建 Draft PR。
3. PR 必须写明改动、原因、影响范围和验证结果。
4. 由 LJure 将 Draft 标为 Ready、审阅并合并到 `main`。
5. 紧急修复也使用 `user/hotfix/*` 与 PR；只有明确记录的例外才允许绕过。

本仓库是公开仓库，`main` 已在 GitHub 启用服务器侧分支保护：必须通过 PR、必须通过“Go 后端验证”和“Vue 前端构建”、合并前必须基于最新 `main`。当前未要求额外人工审批，以便单人维护时仍可合并。工作目录也保留 pre-push 钩子，阻止从本地直接推送 `main`。

## PR 标签

- `actor:codex`：Codex 创建或主要实现的改动。
- `actor:user`：LJure 创建或主要实现的改动。
- `type:feature`、`type:fix`、`type:chore`：改动性质。
