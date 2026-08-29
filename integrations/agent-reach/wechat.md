# 微信公众号 / WeChat Official Account

微信公众号文章优先使用专用 `wxcli`，不要先走 Jina Reader 或通用浏览器抓取。

## 关键词发现

当调用方把 Codex CLI 作为正式运行组件，并且任务是从关键词、公司名或公众号名寻找
微信文章时，Agent Reach 可以先使用 Exa 制定搜索词并发现候选 URL。Exa 命中始终只是
Candidate，必须交给 wxcli 严格校验并读取微信原文；不得把搜索摘要、日期提示或 Agent
判断当成 Article Evidence。

把 Exa 候选整理成 schema v1 Candidate Batch 后，一次性交给：

```powershell
wxcli --json discovery hydrate --input candidates.json
Get-Content candidates.json | wxcli --json discovery hydrate --input -
```

该命令会严格校验微信 URL、去重、记录候选历史并有限回读。Candidate Batch 最多 100 条、
2 MiB；默认前 10 条优先尝试，单次最多 20 条。批次中的来源名称和排名只是 Agent 报告的
非可信 provenance，不能替代微信原文证据。

已经实现的 Brave Direct Discovery 是不启动 Agent 时的可选路径：

```powershell
wxcli --json discovery search "关键词" --company "公司名" --account "公众号名"
```

需要微信原文证据时显式加 `--hydrate`。两种搜索路径都不是微信官方或全量索引。
Agent Reach 不读取或转交 Brave Key，wxcli 不读取或保存 Exa 凭证。wxcli 只处理微信文章，
不负责检索或访问公司官网、ATS，也不判断招聘批次和岗位。

Candidate Batch 不能携带 Cookie、API Key、认证头、Article Evidence、发布时间或身份核验
结论，也不能授权浏览器。只有用户明确授权后，调用方才可在本地命令追加 `--browser`。

## 公开文章

```powershell
wxcli --json article get "https://mp.weixin.qq.com/s/TOKEN"
```

成功标准是退出码 `0`、JSON `ok: true`，且 `data.content_markdown` 非空。
`data.images[]` 是图片 URL；终端不会渲染 Markdown 图片。

## 验证页重试链

1. 默认 HTTP 命令返回 `VERIFICATION_REQUIRED` 时停止。
2. 说明微信返回了验证页，不把它误诊为文章不存在。
3. 只有用户明确授权浏览器模式后，运行：

   ```powershell
   wxcli --json article get "URL" --browser
   ```

4. 如果 Chrome 中仍出现扫码、滑块或确认页面，让用户手工完成；不得绕过验证码。

## 安全边界

- wxcli 的发现和读取 Provider 保持只读；写入器只允许经明确确认的新建未发布草稿，
  或经过备份、diff、冻结计划和再次确认的已有草稿更新。
- 不发布、不群发、不删除，也不绕过冻结计划修改已有草稿。
- 不导出 Cookie，不把 AppSecret 或 Access Token 放入命令参数、日志或 JSON。
- 官方账号草稿和已发布内容只在用户明确要求时调用；Word 草稿上传还必须在本地
  预览之后再次取得明确确认。
- `auth test --allow-live-api` 与 `doctor --allow-live-api` 必须取得用户明确授权。

完整命令与模型契约由独立 `wxcli` Skill 提供；Agent Reach 负责搜索编排和生成候选批次，
但不能替代 wxcli 形成微信证据，也不能继续访问文章里的官网或 ATS 外链。
