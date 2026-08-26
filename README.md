# wxcli

`wxcli` 是一个仅支持 Windows 的只读微信公众号命令行工具。它可以读取公开文章、本地 HTML/Markdown 文件、草稿箱和已发布图文，但不会发布、删除或修改任何内容。

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

## 凭证与安全边界

- 这是只读工具：不发布、不删除、不修改公众号内容。
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

项目默认发布方式是 Windows x64 的 PyInstaller `onedir` 目录加 ZIP，不强制制作单文件 EXE。当前打包脚本用于最小可运行验证：

```powershell
.\scripts\build-spike.ps1
.\dist\spike\wxcli\wxcli.exe --version
```

正式发布物将在最终清洁构建步骤生成，并附带 SHA-256；本项目不会自动上传 GitHub、PyPI 或创建外部 Release。
