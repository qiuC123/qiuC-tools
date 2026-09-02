# 文章读取

## 公开链接

只接受以下两类 HTTPS 链接：

```text
https://mp.weixin.qq.com/s/<token>
https://mp.weixin.qq.com/s?__biz=...&mid=...
```

默认先使用 HTTP Provider：

```powershell
wechat-oa --json article get "URL"
```

需要实时绕过成功缓存时，使用：

```powershell
wechat-oa --json article get "URL" --no-cache
```

`--no-cache` 同时禁止读写缓存。缓存只保存公共 URL 的成功结果，默认 TTL
为一小时；失败和验证页不会缓存。

## 显式图片分析

只有用户明确要求下载、识别文章图片、二维码或 OCR 时，才加入：

```powershell
wechat-oa --json article get "URL" --analyze-media
wechat-oa --json article evidence "URL" --analyze-media
```

启用后返回外层 schema v2，其中分别保留 Article Evidence schema v1 和 Media
Evidence schema v1。不加开关时输出结构不变，也不读取图片缓存或运行分析器。
媒体模式下同时使用 `--no-cache` 会禁止文章和图片缓存的读写。

二维码结果只作为惰性文本证据，绝不打开目标。OCR 只使用本地 Windows 能力，
不上传图片、不切换到云 OCR；`unavailable`、单图失败和部分结果必须原样报告。

## Evidence Bundle

只有用户明确要求长期保存单篇文章证据时，才指定一个尚不存在的目录：

```powershell
wechat-oa --json article evidence "URL" --analyze-media --bundle "C:\evidence\article-001"
```

目标目录不能已经存在，父目录必须已经存在，且路径不能经过符号链接、junction、
mount point 或其他 reparse point。命令不会覆盖或合并目录。Bundle 默认保存证据 JSON、
文章 Markdown、外链和图片清单、manifest、哈希，以及每个成功分析图片位置对应的原始
图片字节。

如果用户只需要元数据，不需要图片文件：

```powershell
wechat-oa --json article evidence "URL" --analyze-media `
  --bundle "C:\evidence\article-001" --bundle-metadata-only
```

metadata-only 仍会执行显式媒体分析，只是不把原始图片写入 Bundle；不会为 Bundle 再次
下载图片。Candidate Batch 不能选择 Bundle 路径，Discovery Bundle 当前未实现。
Bundle 是用户持久文件，不是缓存；不得自动打开其中链接，也不得通过缓存清理删除。

## 验证页

`VERIFICATION_REQUIRED` 表示微信返回了验证页，不表示文章不存在，也不表示
解析器崩溃。只有用户明确授权打开浏览器后，才能直接执行：

```powershell
wechat-oa --json article get "URL" --browser
```

希望仍然先尝试 HTTP、仅在验证页后启动一次可见 Chrome 时，使用：

```powershell
wechat-oa --json article get "URL" --browser-fallback
```

`--no-browser` 会禁止本次调用使用 Chrome，即使用户级长期策略已经设为
`auto-fallback`。长期策略默认是 `never`。

如果可见 Chrome 中出现扫码、滑块或确认页面，让用户手工完成。禁止自动破解或
绕过验证。wechat-oa 使用独立持久 profile，不使用或导出用户日常 Chrome Cookie。

## 本地文件

读取 UTF-8 HTML 或 Markdown：

```powershell
wechat-oa --json article local "C:\path\article.html"
wechat-oa --json article local "C:\path\article.md"
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
