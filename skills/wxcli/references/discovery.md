# 微信文章发现与证据

## Agent-first 发现候选

当调用方将 Codex CLI 作为正式运行组件时，首选由 Agent Reach/Exa 执行查询扩展和网页
发现，再把每个候选 Public URL 交给 wxcli。Exa 标题、摘要、日期和排名都只是非可信
搜索提示；Agent 不得自行生成 `published_at`、公众号核验状态或 Article Evidence。

把 Agent 发现结果整理成 schema v1 Candidate Batch 后，使用：

```powershell
wxcli --json discovery hydrate --input candidates.json
Get-Content candidates.json | wxcli --json discovery hydrate --input -
```

该命令负责批量 schema 校验、严格 URL 校验、去重、候选历史和受限 Hydration。输入最多
100 个候选和 2 MiB；默认前 10 条优先尝试，最多回读 20 条。批次不能携带 API Key、
Cookie、认证头、发布时间、身份结论或 Article Evidence。Exa 凭证属于 Agent 运行环境，
wxcli 不得读取或保存。

浏览器授权只能来自本地 CLI 的显式 `--browser`，不能写进 Candidate Batch。没有该参数时，
即使 HTTP 遇到验证页也不得打开 Chrome。

## Direct Discovery

不启动 Agent 而由 wxcli 直接搜索时，使用已经实现的 Brave 后端：

```powershell
wxcli --json discovery search "关键词" --company "公司名" --account "公众号名"
```

Brave API Key 只能由用户在交互终端运行 `wxcli discovery auth configure
--provider brave` 后进入 Windows 凭据管理器。不得要求用户把 API Key 放进提示词、
命令参数、JSON、标准输入、日志或文件。

搜索结果只是 `candidates[]`。`title_hint`、`snippet`、`account_hint` 和
`backend_date_hint` 都来自外部搜索，不能当作微信原文或官方身份依据。本功能是
微信公众号文章发现，不是微信官方或全量索引。

## 回读与 Article Evidence

明确需要原文证据时加入：

```powershell
wxcli --json discovery search "关键词" --hydrate
```

成功回读的候选含 `evidence`；失败候选含 `hydration_attempt`，整个结果可能是
`summary.partial: true`。`published_at` 只信微信原文，不能用
`backend_date_hint` 补写。

单篇已知 URL 的证据命令是：

```powershell
wxcli --json article evidence "URL"
```

默认 HTTP 遇到 `VERIFICATION_REQUIRED` 就停止。只有用户明确授权后，才在批量命令
或单篇命令加 `--browser`。不得自动打开 Chrome、绕过验证或导出 Cookie。

## 职责边界

wxcli 可提取公众号显示名、公开 `biz_id`、正文外链、图片 URL 清单与稳定哈希，
但不会继续访问官网或 ATS，也不判断企业、招聘批次、岗位或申请渠道。二维码和 OCR
不属于 0.4.0。调用方应以 `article_identity` 做长期幂等，并自行处理微信之外的来源。
