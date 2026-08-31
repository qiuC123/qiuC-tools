# 微信公众号 / WeChat Official Account

微信公众号任务优先使用专用 **WeChat OA**，不要先走 Jina Reader 或通用浏览器抓取。
新安装使用 `wechat-oa`；仅当该命令不存在时使用兼容命令 `wxcli`，两者的参数和 JSON
契约相同。

## 关键词发现

当调用方把 Codex CLI 作为正式运行组件，并且任务是从关键词、公司名或公众号名寻找
微信文章时，Agent Reach 可以先使用 Exa 制定搜索词并发现候选 URL。Exa 命中始终只是
Candidate，必须交给 WeChat OA 严格校验并读取微信原文；不得把搜索摘要、日期提示或
Agent 判断当成 Article Evidence。

把 Exa 候选整理成 schema v1 Candidate Batch 后，一次性交给：

```powershell
wechat-oa --json discovery hydrate --input candidates.json
Get-Content candidates.json | wechat-oa --json discovery hydrate --input -
```

Candidate Batch 最多 100 条、2 MiB，不能携带 Cookie、API Key、认证头、Article
Evidence、发布时间、身份结论或浏览器授权。需要一次性浏览器兜底时，用户必须在本地
命令之外明确授权，然后由调用方追加 `--browser-fallback`；`--no-browser` 始终禁止
本次调用打开 Chrome。

Direct Discovery 使用 WeChat OA 的 Brave 后端：

```powershell
wechat-oa --json discovery search "关键词" --company "公司名" --account "公众号名"
```

搜索结果只是候选。需要微信原文证据时显式加入 `--hydrate`。Agent Reach 不读取或
转交 Brave Key，WeChat OA 不读取或保存 Exa 凭证。两种路径都不是微信官方或全量索引。

## 公开文章

```powershell
wechat-oa --json article get "https://mp.weixin.qq.com/s/TOKEN"
```

成功标准是退出码 `0`、JSON `ok: true`，且 `data.content_markdown` 非空。
`data.images[]` 是图片 URL；终端不会渲染 Markdown 图片。

## 验证页重试链

1. 默认 HTTP 命令返回 `VERIFICATION_REQUIRED` 时停止。
2. 说明微信返回了验证页，不把它误诊为文章不存在。
3. 只有用户明确授权浏览器模式后，运行：

   ```powershell
   wechat-oa --json article get "URL" --browser-fallback
   ```

4. 如果 Chrome 中仍出现扫码、滑块或确认页面，让用户手工完成；不得绕过验证码。

## 安全边界

- WeChat OA 的发现和读取 Provider 保持只读；写入器只允许经明确确认的新建未发布草稿，
  或经过备份、diff、冻结计划和再次确认的已有草稿更新。
- 不发布、不群发、不删除，也不绕过冻结计划修改已有草稿。
- 不导出 Cookie，不把 AppSecret 或 Access Token 放入命令参数、日志或 JSON。
- `auth test --allow-live-api` 与 `doctor --allow-live-api` 必须取得用户明确授权。
- 不访问文章里的官网、ATS 或二维码目标，也不判断企业、招聘批次、岗位或申请渠道。

完整命令与模型契约由独立 `wechat-oa` Skill 提供；Agent Reach 只负责搜索编排和生成候选。
