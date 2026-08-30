# wxcli

`wxcli` 是一个仅支持 Windows 的微信公众号命令行工具。它可以通过外部搜索发现微信公众号文章，再回读微信原文并生成通用证据；也可以读取已知公开文章、本地 HTML/Markdown 文件、草稿箱和已发布图文。它还支持把 Word 正文和单独封面映射成未发布草稿。已有草稿只能经过“备份、比较、生成计划、显式确认”流程安全替换；它不会发布、群发或删除内容。

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

# 对一篇真实微信原文生成身份、外链和稳定哈希证据
wxcli --json article evidence "https://mp.weixin.qq.com/s/example"

# 首次使用发现功能时，交互式把 Brave API Key 存入 Windows 凭据管理器
wxcli discovery auth configure --provider brave
wxcli --json discovery auth status --provider brave

# 只发现候选，不访问候选微信页面
wxcli --json discovery search "2027 校园招聘" --company "示例公司" --account "示例招聘"

# 显式回读分级选中的候选；一次性回退只在 HTTP 验证页后打开 Chrome
wxcli --json discovery search "2027 校园招聘" --hydrate
wxcli --json discovery search "2027 校园招聘" --hydrate --browser-fallback

# 版本化 JSON 可来自文件或标准输入；二者都不能携带 API Key
wxcli --json discovery search --input .\request.json
Get-Content .\request.json | wxcli --json discovery search --input -

# Agent Reach/Exa 只负责生成候选；wxcli 校验、去重并回读微信原文
wxcli --json discovery hydrate --input .\candidates.json
Get-Content .\candidates.json | wxcli --json discovery hydrate --input -

# 只清理发现缓存、候选历史和 checkpoint 状态
wxcli --json discovery cache clear

# --browser 保留直接使用 Chrome 的兼容语义；--browser-fallback 先尝试 HTTP
wxcli article get "https://mp.weixin.qq.com/s/example" --browser
wxcli article get "https://mp.weixin.qq.com/s/example" --browser-fallback
wxcli article get "https://mp.weixin.qq.com/s/example" --no-browser

# 管理浏览器专用资料目录；status 绝不会启动 Chrome
wxcli browser login
wxcli browser status
wxcli browser clear

# 默认长期策略为 never；用户可以明确启用、撤销或仅禁止本次调用
wxcli browser policy set auto-fallback
wxcli browser policy set never
wxcli --json browser policy status

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
wxcli --json account draft import-word ".\正文.docx" --cover ".\封面.png" --output ".\草稿预览" --confirm

# 单独备份已有草稿（不会覆盖已有文件）
wxcli --json account draft backup MEDIA_ID --output ".\backup.json"

# 比较 Word 与第 0 篇草稿，保存备份、预览和冻结的更新计划；此步不写微信
wxcli --json account draft diff MEDIA_ID ".\正文.docx" --cover ".\封面.png" --index 0 --output ".\更新计划"

# 人工检查 plan.json、backup.json 和 prepared\preview.html 后显式应用
wxcli --json account draft update ".\更新计划" --confirm

# 环境诊断；默认跳过真实网络和账号检查
wxcli doctor
wxcli doctor --allow-live-api
```

`browser status` 只报告本地专用 profile、旧版迁移的 `legacy_last_verified_at` 和真实文章成功回读产生的 `last_successful_read_at`。`browser login` 正常结束只代表可见窗口流程完成，不证明远端会话有效。状态命令绝不会打开 Chrome。

## 微信文章发现与证据

- Agent-first 路径由 Codex CLI 配合 Agent Reach/Exa 生成 Candidate Batch，再交给 `discovery hydrate`。wxcli 不读取 Exa 凭证，也不把 Agent 的判断当成证据。Candidate Batch 最多 100 条、2 MiB，未知字段一律拒绝。
- Candidate Batch 不能授权浏览器。默认长期策略为 `never`；用户可以本地追加 `--browser-fallback`、使用可信 Direct Discovery Request 的本次授权，或明确设置长期 `auto-fallback`。`--no-browser` 对本次调用拥有最高优先级。
- `discovery search` 使用 Brave Web Search，并强制加入 `site:mp.weixin.qq.com/s`。它是“微信公众号文章发现”，不是微信官方搜索，也不承诺全微信、全量或实时无延迟。
- 搜索命中始终只是 Candidate。只有 `--hydrate` 成功读取真实微信原文后才会产生 Article Evidence；失败会保留为安全的 `hydration_attempt`，不会用搜索摘要伪造正文。
- 默认最多返回 50 个候选。启用回读后，排序前 10 条必须尝试，其余按通用理由选择，单次最多尝试 20 条。单篇失败会令 `partial: true`，但不会让整个成功搜索使用非零退出码。
- `published_at` 只来自微信原文；搜索后端的日期只写入 `backend_date_hint`。`discovered_at`、`published_at`、`last_successful_read_at` 和旧版迁移时间含义不同。
- `next_cursor` 只续下一页；`checkpoint` 与 `--new-only` 用于同一规范查询的增量发现。搜索响应缓存 15 分钟，候选历史保留 180 天，原文章缓存仍为 1 小时。
- wxcli 提取公众号显示名、公开稳定 `biz_id`、正文外链、图片 URL 清单和稳定哈希，但不访问正文中的官网或 ATS，也不判断企业、招聘批次或岗位。二维码、OCR 和 Media Evidence 计划在 0.6.0，0.5.0 尚不提供。
- 搜索文本禁止控制字符，单个企业/账号名称提示最多 200 字符，发往搜索后端的组合查询最多 2,000 字符。正文里的文字或链接不能冒充页面级 `biz_id` 和发布时间。

完整 schema、部分成功语义和验收口径见 [文章发现说明](docs/article-discovery.md)。

## 支持的公开文章 URL

只接受 HTTPS 且主机严格为 `mp.weixin.qq.com` 的以下两种形式：

```text
https://mp.weixin.qq.com/s/<token>
https://mp.weixin.qq.com/s?__biz=...&mid=...
```

遇到验证页时，未授权浏览器的非交互环境会立即返回 `VERIFICATION_REQUIRED`。获准自动回退时，一个批次最多启动一次可见 Chrome；如果 Chrome 中仍出现扫码、滑块或确认页，wxcli 会立即停止该 Browser Run，返回 `verification_stage: browser` 和 `required_action: run_browser_login`，不会等待或绕过验证。

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

- 默认只做本地转换，生成 `preview.html`、`manifest.json`、带哈希的 `package.json` 和压缩后的图片副本；原 Word 与原图片不会被改写。
- Word 标题会成为公众号标题；正文段落、一至三级标题、粗体、斜体、下划线、文字颜色、对齐、列表、引用、题注、HTTP(S) 超链接、简单表格和行内图片按原顺序映射。页眉、页脚和页码不会进入正文。
- 合并单元格、嵌套表格、表格内图片和浮动图片等无法可靠映射的结构会明确报错，避免静默丢失版式。
- 正文图片会压缩为小于 1 MB 的 JPEG；封面会压缩为小于 64 KB 的 JPEG。上传后的正文图片 URL 来自微信素材接口。
- 只有加入 `--confirm` 才会上传图片并创建一个新草稿。上传检查点按图片 SHA-256 去重，每成功一张就原子保存，失败重试不会重复上传相同图片；检查点不含凭证。
- 创建或更新后会立即读取草稿，核对标题、正文文本以及正文图片数量和顺序。若不一致，错误仍会包含已经创建或修改的 `media_id`，便于人工检查。

## 安全修改已有草稿

1. `draft backup` 保存微信返回的草稿快照，且绝不覆盖已有文件。
2. `draft diff` 只读获取远端草稿，准备 Word，保存 `backup.json`、`plan.json`、预览和差异摘要；不会上传图片或修改草稿。
3. 人工查看备份、差异和预览后，才运行 `draft update PLAN_DIR --confirm`。
4. 更新前会重新读取草稿；如果远端指纹与计划不一致，说明期间草稿已被别人改动，wxcli 会拒绝覆盖。准备包中的正文或图片被改动也会拒绝。
5. 更新只替换指定 `index` 的一篇图文，不发布、不群发、不删除草稿。

## 凭证与安全边界

- 所有 Provider 都是只读的；写操作只存在于独立草稿写入器中，并且必须是显式确认的新建，或经过冻结计划与远端指纹复核的草稿替换。
- 不发布、不群发、不删除草稿或公众号内容，也不点赞或评论。
- 不绕过验证码，不导出 Cookie，也不会把浏览器 Cookie 放入命令输出或缓存。
- AppID 存在普通本地配置中；AppSecret、Access Token 和 discovery 专用 Brave API Key 存在 Windows 凭据管理器中。发现状态库不保存凭证或认证头。
- 不要把 AppSecret、Token 或 Cookie 放在命令参数、日志、问题报告或 Git 文件中。
- `auth test` 不会强制刷新尚未过期的 Token。
- `doctor` 和 `auth test` 只有收到 `--allow-live-api` 明确授权后，才会执行真实微信网络/账号检查。
- 公共文章缓存只保存成功结果，HTTP 与 Chrome 共用规范化 URL 键；失败页不会缓存。
- 浏览器长期策略保存在非敏感的 `%LOCALAPPDATA%\wxcli\browser-policy.json`，独立 profile 自行保存会话 Cookie；wxcli 不读取、导入或导出 Cookie。策略损坏时安全降级为 `never`。
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
.\dist\release\wxcli-0.5.0-windows-x64\wxcli.exe --version
Get-FileHash .\dist\release\wxcli-0.5.0-windows-x64.zip -Algorithm SHA256
```

正式构建要求 Git 工作树干净。安装、升级和彻底清理步骤见 [Windows 发布说明](docs/release-windows.md)。本项目不会自动上传 GitHub、PyPI 或创建外部 Release。

构建后的版本通过稳定目录安装，用户 PATH 永远指向 `%LOCALAPPDATA%\Programs\wxcli\current`：

```powershell
pwsh .\scripts\install-release.ps1
```

安装脚本会在临时目录完成离线冒烟测试，再原子切换 `current`/`previous`；使用 `pwsh .\scripts\install-release.ps1 -Rollback` 可以双向交换当前版和上一版。
