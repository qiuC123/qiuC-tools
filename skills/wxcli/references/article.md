# 文章读取

## 公开链接

只接受以下两类 HTTPS 链接：

```text
https://mp.weixin.qq.com/s/<token>
https://mp.weixin.qq.com/s?__biz=...&mid=...
```

默认先使用 HTTP Provider：

```powershell
wxcli --json article get "URL"
```

需要实时绕过成功缓存时，使用：

```powershell
wxcli --json article get "URL" --no-cache
```

`--no-cache` 同时禁止读写缓存。缓存只保存公共 URL 的成功结果，默认 TTL
为一小时；失败和验证页不会缓存。

## 验证页

`VERIFICATION_REQUIRED` 表示微信返回了验证页，不表示文章不存在，也不表示
解析器崩溃。只有用户明确授权打开浏览器后，才能直接执行：

```powershell
wxcli --json article get "URL" --browser
```

希望仍然先尝试 HTTP、仅在验证页后启动一次可见 Chrome 时，使用：

```powershell
wxcli --json article get "URL" --browser-fallback
```

`--no-browser` 会禁止本次调用使用 Chrome，即使用户级长期策略已经设为
`auto-fallback`。长期策略默认是 `never`。

如果可见 Chrome 中出现扫码、滑块或确认页面，让用户手工完成。禁止自动破解或
绕过验证。wxcli 使用独立持久 profile，不使用或导出用户日常 Chrome Cookie。

## 本地文件

读取 UTF-8 HTML 或 Markdown：

```powershell
wxcli --json article local "C:\path\article.html"
wxcli --json article local "C:\path\article.md"
```

## Article 结果

成功数据包含：

```text
index
title
content_markdown
source_url
author
published_at
images[]
provider
```

图片优先来自 HTML 的 `data-src`，其次为 `src`。`images[]` 只包含 URL；
`mmbiz.qpic.cn` 等地址可能有防盗链。终端只显示 Markdown 语法，不负责渲染或
下载图片。
