# 微信公众号 / WeChat Official Account

微信公众号文章优先使用专用 `wxcli`，不要先走 Jina Reader 或通用浏览器抓取。

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

- wxcli 的读取 Provider 保持只读；唯一写操作是用户检查本地预览并明确授权后，
  从 Word 新建一个未发布草稿。
- 不发布、不群发、不删除，也不修改已有内容。
- 不导出 Cookie，不把 AppSecret 或 Access Token 放入命令参数、日志或 JSON。
- 官方账号草稿和已发布内容只在用户明确要求时调用；Word 草稿上传还必须在本地
  预览之后再次取得明确确认。
- `auth test --allow-live-api` 与 `doctor --allow-live-api` 必须取得用户明确授权。

完整命令与模型契约由独立 `wxcli` Skill 提供；Agent Reach 只负责把微信任务路由到
该专用后端。
