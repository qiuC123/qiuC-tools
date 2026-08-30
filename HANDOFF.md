# wxcli 项目交接说明

## 0.5.0 开发更新（2026-08-30）

0.5.0 的浏览器兜底可靠性功能已完成源码实现、离线验证和稳定目录安装，并快进合并到本地 `main`。本机 `current` 现为 0.5.0，`previous` 为已验收的 0.4.0；下面的 0.4.0 内容保留为历史验收记录和回滚依据。

本轮实现包括：

- 默认关闭、可持久设置的 Browser Fallback Policy；`--browser-fallback` 单次授权与最高优先级的 `--no-browser`。
- HTTP 始终优先，只有微信验证页才进入可见 Chrome；原有单篇 `--browser` 继续表示直接使用 Chrome。
- 一个批次最多创建一个 Chrome persistent context，每篇候选使用新标签页，结束后关闭；独立 profile 中的浏览器会话继续保留，但 wxcli 不导入、导出或显示 Cookie。
- 浏览器锁最多等待五秒；浏览器挑战、崩溃、超时和 `BROWSER_BUSY` 都形成安全的结构化结果，不自动重启 Chrome，也不丢弃已完成的 Evidence。
- `browser login` 不再把“窗口正常结束”误报为登录有效；只有成功的真实 Chrome 文章回读才记录 `last_successful_read_at`。
- 0.6.0 的图片下载、二维码和 OCR 仍只有批准设计，本轮没有实现。

最新离线验证：

```text
pytest: 235 passed
mypy: Success: no issues found in 36 source files
coverage: repository 88%; browser_policy 98%; hydration 96%; Chrome Provider 91%
```

已完成 0.5.0 的独立 PyInstaller onedir 构建和脚本内置离线冒烟，产物为 `dist\release\wxcli-0.5.0-windows-x64.zip`，SHA-256 为 `41bd019295566279bb9a5356c88945aa319ca098c4fb7940002c98fc5de0ef00`。原子安装、回滚到 0.4.0、重新安装以及双向回滚均已成功，最终 `current` 为 0.5.0，`previous` 为 0.4.0。用户级 Skill 的 7 个仓库管理文件与源码哈希一致，安装目录另有 `.wxcli-version` 版本标记文件。

`wxcli --json browser policy status` 已实际返回 `never / configured:false / valid:true`；策略文件不存在，因此当前使用安全缺省值。招聘雷达也已通过 PATH 实际启动 0.5.0 完成空候选、零网络的 schema-v1 管道验收。招聘雷达当前完整 477 项测试通过，660 秒 wxcli 等待上限已经包含在提交 `9d65d03 feat(radar): enforce official announcement admission` 中。

2026-08-30 已在用户明确授权后完成安装版 0.5.0 的真实 Agent-first / Exa / 微信 / Chrome 验收：外部搜索产生 18 个严格 Public URL 候选，wxcli 接受 18 个、尝试回读 17 个并验证成功 17 个，`partial:false`；批次使用 CLI 单次 `auto-fallback`，一个 Browser Run 回读 17 个候选，`user_action_required:0`。另用未回读候选验证单篇路径：`--no-browser` 正确以退出码 6 返回 `VERIFICATION_REQUIRED`，随后 `--browser-fallback` 以退出码 0 成功提取非空正文和图片。验收结束后无 wxcli Chrome 进程残留，长期策略仍为 `never / configured:false / valid:true`。真实 URL、标题和正文未写入 Git。

---

以下内容是 2026-08-29 的 0.4.0 已安装版本快照。

> 快照日期：2026-08-29
>
> 当前版本：0.4.0
>
> 当前代码状态：0.4.0 已合并到本地 `main`，尚未打 tag、配置远程仓库或发布外部 Release

## 1. 当前结论

wxcli 0.4.0 的开发、离线测试、Windows onedir 打包、稳定目录安装、回滚验证和真实 Agent-first 微信文章发现验收均已完成。本机当前服役版本是 0.4.0，保留 0.3.0 作为一键回滚版本。

0.4.0 已形成以下闭环：

```text
Codex CLI
→ Agent Reach / Exa 发现候选
→ schema v1 Candidate Batch
→ wxcli 严格校验、去重和分级回读
→ HTTP Provider
→ 用户明确授权时才使用可见 Chrome
→ Article Evidence
```

wxcli 仍然是 Windows CLI。网页访问是 CLI 背后的 Provider 能力，不会把产品变成桌面 GUI，也不会改变 JSON 管道接口。

## 2. Git、版本和安装状态

- 当前分支：`main`
- 当前提交：`2b9c53a fix: reject empty public article evidence`
- 原功能分支：`codex/safe-resumable-draft-updates`，目前与 `main` 指向同一提交
- 工作区在生成本交接文件前是干净的
- 项目版本：`pyproject.toml` 与 `src/wxcli/__init__.py` 均为 0.4.0
- Git remote：未配置
- Git tag：尚未创建
- 外部 push / PR / Release：均未执行

本机稳定安装布局：

```text
%LOCALAPPDATA%\Programs\wxcli\
  current\       # 当前 0.4.0，PATH 永远指向这里
  previous\      # 上一个 0.3.0，可回滚
  skills\<版本>\ # 对应版本的 wxcli Skill 快照
```

当前命令解析到：

```text
C:\Users\Mayn\AppData\Local\Programs\wxcli\current\wxcli.exe
```

用户级 wxcli Skill 已同步为 0.4.0。

## 3. 已实现能力

### 3.1 公开微信文章读取

- 严格接受合法的 `https://mp.weixin.qq.com/s/...` 文章 URL。
- 默认使用 Public HTTP Provider；遇到验证页时返回结构化错误。
- 只有用户明确添加 `--browser`，才允许启动 wxcli 独立、可见、持久的 Chrome profile。
- 提取标题、作者、公众号名称、发布时间、正文 Markdown 和图片 URL。
- 支持成功缓存、页面分类、验证页、文章不存在和解析失败等结构化状态。
- 支持本地 UTF-8 HTML / Markdown 文件读取。

常用命令：

```powershell
wxcli --json article get "WECHAT_URL"
wxcli --json article get "WECHAT_URL" --browser
wxcli --json article local ".\article.md"
```

### 3.2 微信文章发现和 Article Evidence

0.4.0 新增了通用 discovery 层。搜索命中始终只是 `ArticleCandidate`，只有成功回读微信原文后才能生成 `ArticleEvidence`。

正式集成推荐 Agent-first：

```text
Codex CLI / Agent Reach / Exa
→ Candidate Batch
→ wxcli discovery hydrate
```

```powershell
wxcli --json discovery hydrate --input candidates.json
Get-Content candidates.json | wxcli --json discovery hydrate --input -
```

Candidate Batch 最多接收 100 个候选和 2 MiB 输入。默认前 10 条优先尝试，其余候选按稳定理由选择，单批最多回读 20 条。批次本身不能授权浏览器，也不能携带 API Key、Cookie、认证头、公众号身份结论或伪造的发布时间。

wxcli 内也实现了可选的 Brave Direct Discovery：

```powershell
wxcli --json discovery search "关键词" --company "公司名" --account "公众号名"
```

Brave 只用于不经过 Agent 的直接搜索，不是 Agent-first 正式路径的必需配置。最终真实验收没有使用 Brave，也没有使用匿名搜索额度。

单篇证据：

```powershell
wxcli --json article evidence "WECHAT_URL"
```

Article Evidence 包含：

- 微信原文 `Article`
- 公众号显示名和公开 `biz_id` 身份证据
- 正文外链证据，但不继续访问目标
- 图片 URL 清单
- `content_sha256` 和 `evidence_sha256` 稳定哈希
- `last_verified_at`
- 回读失败时的结构化 `HydrationAttempt`

`published_at` 只允许来自微信原文。Exa 或 Brave 返回的日期只能作为 `backend_date_hint`，不能补写成文章发布时间。

### 3.3 公众号官方内容

- 只读访问公众号已发布内容。
- 只读访问公众号草稿列表和详情。
- AppSecret 和 Access Token 使用系统 keyring，不通过参数、JSON、日志或 Git 暴露。
- 默认 `doctor` 不访问真实 API；真实检查必须显式使用 `--allow-live-api`。

### 3.4 受控草稿写入

所有 Provider 保持只读。允许的写入只存在于独立 Draft Writer，并且必须经过显式确认：

1. Word 生成本地预览，用户检查后才能 `import-word --confirm` 新建一个未发布草稿。
2. 修改已有草稿必须先 `backup`，再生成只读 `diff` 和冻结计划；用户检查计划后，才能单独执行 `update PLAN_DIR --confirm`。

wxcli 不提供发布、群发、删除、点赞或评论能力。

### 3.5 Windows 构建、安装和回滚

- `scripts/build-release.ps1` 使用 staging 构建，失败不会删除正在服役的旧版本。
- 默认拒绝覆盖同版本产物；只有显式 `-Force` 才清理该版本自己的目录和 ZIP。
- `scripts/install-release.ps1` 将产物复制到 `current.new` 并先做离线冒烟测试，再通过 NTFS 目录改名原子切换。
- 安装失败会恢复原版本。
- `-Rollback` 对称交换 `current` 和 `previous`，并恢复对应 Skill 快照。

## 4. wxcli 与招聘雷达的职责边界

wxcli 负责所有“微信内容证据”相关工作：

- 微信文章候选接入或可选的 Direct Discovery
- 微信 URL 严格校验、规范化和去重
- 微信原文回读
- 公众号显示名和公开 `biz_id` 提取与比对证据
- 正文、图片 URL、外链和稳定哈希
- 验证状态、失败原因和回读时间

`official-campus-radar` 或其他调用方负责：

- 搜索企业官网、招聘官网和 ATS
- 维护企业与公众号白名单或映射关系
- 判断文章属于哪个企业、招聘批次和岗位
- 提取并合并申请渠道
- 将微信证据与官网、ATS 等其他来源合并
- 保存招聘业务数据库模型

wxcli 可以提取微信正文中的官网或 ATS 链接，但不会继续访问这些链接。

## 5. 安全契约

- 不宣称“全微信索引”或实时、全量覆盖；产品术语是 WeChat Article Discovery / 微信公众号文章发现。
- 不绕过验证码、滑块或微信验证页。
- 不读取、导出或复用用户日常 Chrome Cookie。
- 不自动启动 Chrome；必须由本地 CLI 的显式 `--browser` 授权。
- API Key、AppSecret、Token 和 Cookie 不进入命令参数、Candidate Batch、stdout、日志、缓存或 Git。
- JSON 模式下 stdout 只输出一个 UTF-8 JSON；诊断信息进入 stderr。
- 同时检查进程退出码和 JSON envelope 的 `ok` 字段。
- 搜索摘要、标题提示和日期提示不能伪装成微信原文。

## 6. 测试和真实验收

合并到本地 `main` 后，于 2026-08-29 重新验证：

```text
pytest: 208 passed in 20.09s
mypy: Success: no issues found in 35 source files
```

普通测试使用 fixture、模拟 Discovery Provider、模拟文章输出和临时 SQLite，不访问真实 Brave、Exa、微信或 Chrome。

Windows 0.4.0 发布包：

```text
dist\release\wxcli-0.4.0-windows-x64.zip
SHA-256: 83d1c8e3a19ce4f00e2d5c8a806169da45589b756f19f93d100a0b1fb22dda40
```

ZIP 与 `.sha256` 文件已重新核对，一致。

打包 EXE 的真实 Agent-first 测试使用搜索词“校园招聘”：

- 编排器：Codex CLI
- 搜索 Provider：Exa
- Exa 授权：个人 API Key 通过 Agent Reach / mcporter 的环境请求头使用
- 匿名 fallback：没有
- Brave：没有使用
- 收到候选：20
- 按策略尝试回读：15
- 成功 Article Evidence：15
- 空正文且无图片却被误判为成功的 Evidence：0
- 内容 Provider：用户明确授权后的 Chrome
- 测试结束后残留 wxcli Chrome 进程：0

此前发现的空文章壳误判已由提交 `2b9c53a` 修复：空正文且无图片的页面现在返回 `PARSING_ERROR`，不会生成成功 Evidence；图片型文章仍可正常生成 Evidence。

本地验收记录位于被 Git 忽略的目录：

```text
build\packaged-live-acceptance-0.4.0\acceptance-summary.json
build\packaged-live-acceptance-0.4.0\agent-end-to-end-browser-fixed.json
```

这些文件可能包含真实文章标题、URL 和正文证据，不应直接提交到 Git。

## 7. 当前限制和未完成事项

### 产品限制

- 微信没有开放完整公众号文章搜索 API，发现覆盖率取决于外部搜索 Provider 的收录和延迟。
- HTTP 回读经常遇到微信验证页；Chrome 也不能保证每篇文章都可读取。
- wxcli 0.4.0 不做持久浏览器自动兜底、二维码识别、二维码目标提取或图片 OCR。
- 不下载完整证据包，不继续访问外链，不处理官网或 ATS 内容。
- 不进行企业、招聘公告、批次、岗位或申请渠道的业务分类。

### 发布工作尚未完成

- 尚未配置 Git remote。
- 尚未 push 本地 `main`。
- 尚未创建 `v0.4.0` tag。
- 尚未发布外部 Release。
- 原功能分支尚未删除；它与 `main` 当前指向同一提交，保留不会影响运行。

### 文档提示

`docs/release-windows.md` 中“首次使用文章发现时配置 Brave”的描述只适用于 Direct Discovery。Agent-first 使用 Exa 时不需要 Brave Key，后续文档整理时可进一步把这一点写得更醒目。

## 8. 建议的下一步

1. 人工审阅本交接文件和本地 `main`。
2. 确定远程仓库后配置 remote，并单独授权 push。
3. 从已验收的 `2b9c53a` 创建 `v0.4.0` tag，再生成或核对正式 Release 资产。
4. 在 `official-campus-radar` 中接入 Agent-first Candidate Batch → `wxcli discovery hydrate`，不要直接依赖 wxcli 内部 Pydantic 类型或招聘业务模型。
5. 用冻结企业和公众号基准集持续计算发现延迟、受控召回率、严格身份误报率和搜索失败率。
6. 按 ADR-0006 将持久浏览器自动兜底和单批次 Chrome 复用作为 0.5.0；按 ADR-0007 将显式、受限的图片下载、二维码、本地 OCR、独立 Media Evidence 和原子 Evidence Bundle 作为 0.6.0，保持 Article Evidence schema v1 不变。

## 9. 关键入口

- 领域边界：[`CONTEXT.md`](CONTEXT.md)
- 总体使用说明：[`README.md`](README.md)
- Discovery 契约：[`docs/article-discovery.md`](docs/article-discovery.md)
- 0.5.0 Browser Fallback设计：[`docs/browser-fallback.md`](docs/browser-fallback.md)
- 0.6.0 Media Evidence 设计：[`docs/media-evidence.md`](docs/media-evidence.md)
- 0.4.0 开发复盘与经验：[`docs/0.4.0-development-retrospective.md`](docs/0.4.0-development-retrospective.md)
- Discovery / Evidence 分离：[`docs/adr/0004-separate-discovery-candidates-from-article-evidence.md`](docs/adr/0004-separate-discovery-candidates-from-article-evidence.md)
- Agent-first 决策：[`docs/adr/0005-use-agent-orchestrated-discovery-as-primary-integration.md`](docs/adr/0005-use-agent-orchestrated-discovery-as-primary-integration.md)
- 持久浏览器兜底授权：[`docs/adr/0006-persist-browser-fallback-authorization-outside-candidate-input.md`](docs/adr/0006-persist-browser-fallback-authorization-outside-candidate-input.md)
- 媒体分析与核心证据分离：[`docs/adr/0007-keep-media-analysis-separate-from-core-article-evidence.md`](docs/adr/0007-keep-media-analysis-separate-from-core-article-evidence.md)
- Windows 发布和回滚：[`docs/release-windows.md`](docs/release-windows.md)
- wxcli Skill：[`skills/wxcli/SKILL.md`](skills/wxcli/SKILL.md)
- Discovery 实现：[`src/wxcli/discovery/`](src/wxcli/discovery/)
- Evidence 实现：[`src/wxcli/evidence.py`](src/wxcli/evidence.py)
- 公开文章解析：[`src/wxcli/public_article.py`](src/wxcli/public_article.py)
- 原子安装脚本：[`scripts/install-release.ps1`](scripts/install-release.ps1)
- Agent-first 人工测试：[`scripts/live-agent-discovery-smoke.ps1`](scripts/live-agent-discovery-smoke.ps1)

## 10. 接手时的最小检查

```powershell
git status -sb
git log -1 --oneline
wxcli --version
wxcli --json doctor
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m mypy src
```

预期结果：分支为 `main`，版本为 0.5.0，Doctor 默认不执行 live checks，测试 235 项通过，mypy 零问题。正式发布前还应确认 `v0.5.0` tag 指向当前发布提交，并完成上文单独授权的真实微信/Chrome live smoke。
