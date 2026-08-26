# wxcli

`wxcli` 是一个仅支持 Windows 的微信公众号命令行工具。它可以读取公开文章、本地 HTML/Markdown 文件、草稿箱和已发布图文，也可以把 Word 正文和单独的封面映射成一个新的未发布草稿。它不会发布、群发、删除或修改已有内容。

## 环境与开发安装

- Windows 11 x64
- Python 3.12
- Google Chrome（默认路径：`C:\Program Files\Google\Chrome\Application\chrome.exe`）

在 PowerShell 中执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
pytest
mypy src
wxcli --version
```

## 常用命令

全局 `--json` 要放在子命令之前，例如 `wxcli --json article local .\article.md`。

```powershell
# 读取本地 UTF-8 HTML 或 Markdown
wxcli article local .\article.md

# 读取支持的微信公众号公开文章；成功结果缓存 1 小时
wxcli article get "https://mp.weixin.qq.com/s/example"
wxcli article get "https://mp.weixin.qq.com/s/example" --no-cache

# 只有显式指定 --browser 才会打开可见 Chrome
wxcli article get "https://mp.weixin.qq.com/s/example" --browser

# 管理浏览器专用资料目录；status 绝不会启动 Chrome
wxcli browser login
wxcli browser status
wxcli browser clear

# 清除公共文章成功缓存
wxcli cache clear

# 交互式录入官方接口凭证，以及只报告“是否存在”的状态
wxcli auth configure
wxcli auth status

# 默认不调用真实接口；显式授权后才执行只读检查
wxcli auth test
wxcli auth test --allow-live-api

# 草稿与已发布消息；list 会保留多图文中的全部 articles 和 index
wxcli account draft list --offset 0 --count 20
wxcli account draft get MEDIA_ID
wxcli account published list --offset 0 --count 20
wxcli account published get ARTICLE_ID

# 先在本地生成预览，不联网、不读取公众号凭证
wxcli --json account draft import-word ".\正文.docx" --cover ".\封面.png" --output ".\草稿预览"

# 预览确认无误后，才显式创建一个未发布草稿；不会发布或群发
wxcli --json account draft import-word ".\正文.docx" --cover ".\封面.png" --confirm

# 环境诊断；默认跳过真实网络和账号检查
wxcli doctor
wxcli doctor --allow-live-api
```

`browser status` 只报告本地专用 profile 是否存在，以及本地记录的 `last_verified_at`。它不会打开 Chrome，也不能据此断言当前微信会话仍然有效。

## 支持的公开文章 URL

只接受 HTTPS 且主机严格为 `mp.weixin.qq.com` 的以下两种形式：

```text
https://mp.weixin.qq.com/s/<token>
https://mp.weixin.qq.com/s?__biz=...&mid=...
```

遇到验证页时，非交互环境会立即返回 `VERIFICATION_REQUIRED`，不会停在那里等待输入，也不会尝试绕过验证码。只有用户显式传入 `--browser` 时才会打开 Chrome。

## JSON、标准输出与退出码

使用 `--json` 时，标准输出只包含一个 UTF-8 JSON 文档：成功形如 `{"ok":true,"data":...}`，失败形如 `{"ok":false,"error":...}`。日志、提示和进度只写到标准错误，因此可以安全接入 PowerShell 管道。输出层会再次脱敏常见的 Token、AppSecret、Cookie 和 Authorization 字段。

| 退出码 | 含义 |
| ---: | --- |
| 0 | 成功 |
| 1 | 通用错误 |
| 2 | 输入或非法 CLI 参数 |
| 3 | 数据验证失败 |
| 4 | 内容不存在 |
| 5 | 网络错误 |
| 6 | 认证或权限错误（也包括需要人工验证） |
| 7 | Chrome 错误 |
| 8 | 页面或接口解析错误 |
| 9 | 本地配置错误 |

## Word 草稿导入

- 默认只做本地转换，生成 `preview.html`、`manifest.json` 和压缩后的图片副本；原 Word 与原图片不会被改写。
- Word 标题会成为公众号标题；正文段落、一级标题、粗体、斜体、下划线、文字颜色和行内图片按原顺序映射。页眉、页脚和页码不会进入正文。
- 当前不接受 Word 表格或同一段中混排文字与图片，遇到时会明确报错，避免静默丢失版式。
- 正文图片会压缩为小于 1 MB 的 JPEG；封面会压缩为小于 64 KB 的 JPEG。上传后的正文图片 URL 来自微信素材接口。
- 只有加入 `--confirm` 才会上传图片并创建一个新草稿。微信的图片上传不能回滚；若后续创建草稿失败，错误结果会报告已上传的图片数量。

## 凭证与安全边界

- 除显式的 `account draft import-word --confirm` 可新建一个未发布草稿外，其余 Provider 都是只读的。
- 不发布、不群发、不删除、不修改已有草稿或公众号内容，也不点赞或评论。
- 不绕过验证码，不导出 Cookie，也不会把浏览器 Cookie 放入命令输出或缓存。
- AppID 存在普通本地配置中；AppSecret 和 Access Token 存在 Windows 凭据管理器中；状态文件只记录 Token 到期时间。
- 不要把 AppSecret、Token 或 Cookie 放在命令参数、日志、问题报告或 Git 文件中。
- `auth test` 不会强制刷新尚未过期的 Token。
- `doctor` 和 `auth test` 只有收到 `--allow-live-api` 明确授权后，才会执行真实微信网络/账号检查。
- 公共文章缓存只保存成功结果，HTTP 与 Chrome 共用规范化 URL 键；失败页不会缓存。
- 微信 `qpic.cn` 图片在结果中只保留 URL。图片服务可能有防盗链，离开微信页面后不保证能直接显示。

运行目录、缓存、专用 Chrome profile 和凭据都在仓库之外，并由 `.gitignore` 排除。

## doctor 结果

每项检查只会返回 `pass`、`fail`、`skip` 或 `warn`：

- Chrome 是否存在
- wxcli 运行目录是否可写
- 专用 profile 是否被其他进程锁定
- AppID/AppSecret 是否已配置（绝不输出值）
- 是否存在未过期的本地 Token 缓存
- 网络、stable token、IP 白名单、草稿权限和已发布权限（默认全部跳过）

只要出现 `fail`，doctor 总结果就是失败；`warn` 用于提示但不会单独令总结果失败。

## Windows 打包

项目默认发布方式是 Windows x64 的 PyInstaller `onedir` 目录加 ZIP，不制作单文件 EXE。正式构建会在 `build` 下创建隔离的 Python 3.12 环境，完成脱离开发 venv 的冒烟测试，然后生成目录、ZIP 和 SHA-256：

```powershell
.\scripts\build-release.ps1
.\dist\release\wxcli-0.2.0-windows-x64\wxcli.exe --version
Get-FileHash .\dist\release\wxcli-0.2.0-windows-x64.zip -Algorithm SHA256
```

正式构建要求 Git 工作树干净。安装、升级和彻底清理步骤见 [Windows 发布说明](docs/release-windows.md)。本项目不会自动上传 GitHub、PyPI 或创建外部 Release。
