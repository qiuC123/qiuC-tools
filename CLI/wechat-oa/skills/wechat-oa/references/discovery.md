# 微信文章发现与证据

## Agent-first 发现候选

当调用方将 Codex CLI 作为正式运行组件时，首选由 Agent Reach/Exa 执行查询扩展和网页
发现，再把每个候选 Public URL 交给 wechat-oa。Exa 标题、摘要、日期和排名都只是非可信
搜索提示；Agent 不得自行生成 `published_at`、公众号核验状态或 Article Evidence。

把 Agent 发现结果整理成 schema v1 Candidate Batch 后，使用：

```powershell
wechat-oa --json discovery hydrate --input candidates.json
Get-Content candidates.json | wechat-oa --json discovery hydrate --input -
```

该命令负责批量 schema 校验、严格 URL 校验、去重、候选历史和受限 Hydration。输入最多
100 个候选和 2 MiB；默认前 10 条优先尝试，最多读取 20 篇公众号原文。批次不能携带 API Key、
Cookie、认证头、发布时间、身份结论或 Article Evidence。此路径的 Exa 凭证属于 Agent
运行环境，不能通过 Candidate Batch 交给 wechat-oa。

Candidate Batch 不能携带浏览器授权。本地 CLI 可用 `--browser-fallback` 做本次授权，
也可在用户已经明确设置长期 `auto-fallback` 后自动处理 HTTP 验证页；`--no-browser`
始终覆盖这些授权。可信 Direct Discovery Request 的 `allow_browser: true` 只授权自身调用，
不会修改长期策略。

## Direct Discovery

不启动 Agent 而由 wechat-oa 直接搜索时，可选择 Brave 或 Exa；默认仍是 Brave：

```powershell
wechat-oa --json discovery search "关键词" --company "公司名" --account "公众号名"
wechat-oa --json discovery search "关键词" --company "公司名" --account "公众号名" `
  --provider exa --hydrate --no-browser
```

Brave/Exa API Key 只能由用户在交互终端分别运行 `wechat-oa discovery auth configure
--provider brave` 或 `wechat-oa discovery auth configure --provider exa` 后进入 Windows 凭据管理器。不得要求用户把 API Key 放进提示词、
命令参数、环境变量、JSON、标准输入、日志或文件；特别不能依赖 `EXA_API_KEY` 进程环境。

Exa 请求使用 host-only `mp.weixin.qq.com` 域名过滤；命中仍必须通过 wechat-oa 的严格
HTTPS `mp.weixin.qq.com/s` URL 校验。非 `/s`、其他 host 或带凭据 URL 都不能成为候选。

搜索结果只是 `candidates[]`。`title_hint`、`snippet`、`account_hint` 和
`backend_date_hint` 都来自外部搜索，不能当作微信原文或官方身份依据。本功能是
微信公众号文章发现，不是微信官方或全量索引。

Direct Discovery 失败时同时检查退出码、`error.code` 和 `error.details.reason`：未配置与
凭据拒绝使用 `AUTHENTICATION_ERROR`/退出码 6；限流、超时、网络、上游错误和无效响应
使用 `NETWORK_ERROR`/退出码 5。稳定 reason 为 `not_configured`、
`credential_rejected`、`rate_limited`、`timeout`、`network_error`、`provider_error`、
`invalid_response`。搜索为空不是错误，而是 `ok: true`、空 `candidates[]`；单篇回读失败
保留候选的 `hydration_attempt` 并令 `summary.partial: true`。

## 读取公众号原文与 Article Evidence

明确需要原文证据时加入：

```powershell
wechat-oa --json discovery search "关键词" --hydrate
```

成功读取的候选含 `evidence`；失败候选含 `hydration_attempt`，整个结果可能是
`summary.partial: true`。`published_at` 只信微信原文，不能用
`backend_date_hint` 补写。

图片、标准二维码和 Windows 本地 OCR 分析默认关闭。只有用户明确要求媒体证据时，才可
在已经启用原文读取的本地命令上加入：

```powershell
wechat-oa --json discovery search "关键词" --hydrate --analyze-media
wechat-oa --json discovery hydrate --input candidates.json --analyze-media
```

不开启时，discovery schema v1 输出不变；开启后返回外层 schema v2，完整保留原
discovery 结果，并通过 `candidate_index`、`article_identity` 和正文哈希关联每篇 Media
Evidence。单批最多记录 200 个图片项、400 MiB 下载字节和 1,000,000 个 OCR 字符。
Candidate Batch 是非可信输入，不能用 `analyze_media` 字段开启功能或选择限额，也不能
借媒体分析获得浏览器权限。二维码 payload 只作为惰性证据返回，绝不自动打开。

单篇已知 URL 的证据命令是：

```powershell
wechat-oa --json article evidence "URL"
```

默认长期策略为 `never`，HTTP 遇到 `VERIFICATION_REQUIRED` 就停止。用户可对批量或
单篇命令加 `--browser-fallback` 做一次性授权；兼容的 `--browser` 在单篇命令中仍直接
使用 Chrome。若 Chrome 仍要求人工验证，结果会给出 `required_action:
run_browser_login` 并停止本次无人值守 Browser Run。已知候选文章 URL 时，只有用户再次
明确授权后，才运行 `wechat-oa --json browser verify "URL"` 打开该文章并等待人工处理；
不得用 computer-use 或其他浏览器自动化接管验证窗口，也不得绕过验证或导出 Cookie。

## 职责边界

wechat-oa 可提取公众号显示名、公开 `biz_id`、正文外链、图片 URL 清单与稳定哈希；在
显式本地开关下，还可产生独立的二维码/OCR 派生证据。但它不会继续访问官网或 ATS，
也不判断企业、招聘批次、岗位或申请渠道。调用方应以 `article_identity` 做长期幂等，
并自行处理微信之外的来源。
