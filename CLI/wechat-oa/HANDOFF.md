# WeChat OA 项目交接说明

## 0.7.3 Exa deep 多查询召回修复（发布准备，2026-09-02）

- 招聘雷达对 0.7.2 做了第二轮真实复验：整页重排成功把 Exa 原始 rank 74/80/88 的腾讯候选提升进前 20，证明本地排序修复有效；但已知腾讯原文仍不在 Exa 返回的完整 100 条结果中，因此本轮问题是 Provider 召回而不是候选截断或 Hydration。
- Exa 改为一次有界 `deep` 搜索。结构化自然语言主查询包含目标公众号/公司、原查询、校招简称和软发布日期窗口；最多 10 条补充查询覆盖原始措辞、简称、人才计划和发布年份；固定搜索规划提示优先直接来源并降低其他公司、聚合、转载、面经和汇总内容。
- 日期仍不下推成 `startPublishedDate`/`endPublishedDate` 硬过滤；所有 Exa 标题、作者、日期和排序仍只是 Candidate 提示，不能生成公众号身份、可信 `published_at` 或 Article Evidence。
- Exa 搜索策略版本进入私有 query fingerprint。安装新版本后不会复用 0.7.2 的旧 Exa 页面缓存，也无需执行破坏性的 `discovery cache clear`；Brave 缓存指纹和对外 schema 不变。
- 已知腾讯 URL 的 MockTransport 回归冻结完整 deep 请求体，并继续验证 HTTP `VERIFICATION_REQUIRED`、不自动打开 Chrome、候选保留为 partial 的边界。本轮未执行新的真实 Exa、微信或 Chrome。
- Direct Discovery schema v1、单 JSON envelope、provider failure reasons、Windows 凭据所有权、严格 URL 校验、原始 Exa rank provenance 和 Brave 默认兼容契约均不变。
- 0.7.3 发布准备验证为 430 项 pytest 全绿、mypy 检查 48 个源文件无错误；PyInstaller 双命令包和全部离线冒烟通过。候选 ZIP SHA-256 为 `be6c85c404f431af654ca6f1da7d07e622e9ea9a66947811315043a3c2b8daaf`。

## 0.7.2 Exa 候选质量与 Hydration 排序修复（发布收口，2026-09-02）

- 招聘雷达对 0.7.1 做了明确授权的真实复验：零候选缺陷已解除，但已知腾讯原文不在对外 50 条中；前 20 条 Hydration 含大量第三方或其他公司内容，均按契约停在 `VERIFICATION_REQUIRED`，没有打开 Chrome 或生成 Article Evidence。
- 根因是 Exa 已返回最多 100 条，但 Discovery Service 在完整结果页做本地相关性排序前先截成 50 条；同时旧排序会让“第三方作者、标题提到腾讯和秋招”的内容领先于账号提示未知但标题为腾讯的内容。
- 修复保持一次 `auto` 搜索：查询先放去重后的公司/账号提示并表达官方公众号偏好，再加入原查询及有界校招词形变体；完整 Provider 结果页在对外候选数量截断前重排。
- 排序优先账号提示与查询的组合命中；精确或相关账号提示加权，明确不同的账号提示只降权而不删除。原始 Exa rank 继续进入 provenance，任何搜索提示都不成为公众号身份、发布日期或 Article Evidence。
- 脱敏质量回归模拟真实噪声分布，并把已知腾讯 URL 放在 Exa 原始 rank 80；目标在整页重排后进入前 20 Hydration，第三方结果不消耗该批次的 Hydration 预算。测试不调用真实 Exa、微信或 Chrome。
- Direct Discovery schema v1、单 JSON envelope、错误码、Windows 凭据所有权、严格 URL 校验、Brave 默认兼容和 `--no-browser` 语义均不变。
- PR #23 已 squash 合入 `main`，合并提交为 `6d5a6ad`；远端 Tests and type checks 与 Windows package smoke 均通过。
- 正式标签 `wechat-oa-v0.7.2` 和 GitHub Release 已发布。0.7.2 验证为 429 项 pytest 全绿、mypy 检查 48 个源文件无错误；PyInstaller 双命令包和全部离线冒烟通过。ZIP SHA-256 为 `fd739fd0846effb0f0de02063cc5d60c3e1335a1cc65b476b3d6bffbe5c6cfaa`。
- 已从公开 Release 重新下载 ZIP 和校验文件，实算哈希、发布校验值和固定哈希三者一致。稳定安装与 0.7.2 → 0.7.1 → 0.7.2 双向回滚通过，最终为 `current=0.7.2`、`previous=0.7.1`，canonical Skill 已同步，Exa 凭据状态仍为 `configured=true`。
- 0.7.2 构建后没有再次执行真实 Exa、微信文章回读或 Chrome；真实质量复验仍由招聘雷达按冻结命令另行执行。

## 0.7.1 Exa Direct Discovery 召回修复（发布收口，2026-09-02）

- 招聘雷达使用 `query="2027届 秋招"`、company/account=`腾讯` 和 2026-06-01 至 2026-09-02 窗口时，0.7.0 Exa 返回成功空结果，但已知存在严格且可回读的公众号文章 URL。
- 根因是 0.7.0 同时把 company/account 重复编码为引号短语，并把证据日期窗口下推成 Exa `startPublishedDate`/`endPublishedDate` 硬过滤。Exa 官方契约明确日期过滤只返回带有且满足 provider 发布时间的链接，因此索引缺少日期的微信文章会被排除。
- 修复将 company/account 作为去重的自然语义提示，不再使用强制引号短语；日期窗口不再下推给 Exa，仍参与本地候选排序，并只在成功 Hydration 后依据微信原文 `published_at` 做可信过滤。
- 模拟回归使用已知严格 URL、标题、账号提示和无 Exa 日期元数据的响应；在 `hydrate=true`、`allow_browser=false` 且 HTTP 返回 `VERIFICATION_REQUIRED` 时，候选仍被保留并返回 `summary.partial=true`，不会自动打开 Chrome或伪造 Article Evidence。
- Direct Discovery schema v1、单 UTF-8 JSON envelope、provider failure reasons、Windows 凭据所有权、严格 URL 校验及 Brave 默认兼容契约均不变。本轮测试不调用真实 Exa、微信或 Chrome。
- PR #21 已 squash 合入 `main`，合并提交为 `887868e`；远端 Tests and type checks 与 Windows package smoke 均通过。
- 正式标签 `wechat-oa-v0.7.1` 和 GitHub Release 已发布。0.7.1 验证为 427 项 pytest 全绿、mypy 检查 48 个源文件无错误；PyInstaller 双命令包和全部离线冒烟通过。ZIP SHA-256 为 `abf741a67b0c01a2bb7364e092f927b13065ff92b57c70c9ed65fa4c20240ee1`。
- 已从公开 Release 重新下载 ZIP 和校验文件，实算哈希、发布校验值和固定哈希三者一致。稳定安装与 0.7.1 → 0.7.0 → 0.7.1 双向回滚通过，最终为 `current=0.7.1`、`previous=0.7.0`，canonical Skill 已同步，Exa 凭据状态仍为 `configured=true`。
- 本轮没有执行真实 Exa 搜索、微信文章回读或 Chrome；已知腾讯文章的召回由脱敏 MockTransport 回归覆盖，真实服务端召回仍需另行明确授权 live smoke 后确认。

## 0.7.0 原生 Exa Direct Discovery 发布收口（2026-09-02）

- 招聘雷达会直接调用 `wechat-oa --json discovery search ... --provider exa --hydrate --no-browser`，不会传递 Exa Key，并会从子进程环境移除 `EXA_API_KEY`。
- wechat-oa 已增加原生 Exa Provider；Brave 仍是默认值和兼容路径。两个 Key 分别保存在 Windows 凭据管理器，命令参数、JSON、环境、stdout、缓存和 Git 都不承载凭据。
- Exa 请求使用 host-only `includeDomains: ["mp.weixin.qq.com"]`。命中仍只是候选，随后必须通过严格 HTTPS `mp.weixin.qq.com/s` URL 校验；非 `/s`、其他 host 和带凭据 URL 均被丢弃。
- Direct Discovery 输出继续使用 schema v1，外层继续是单个 UTF-8 JSON envelope。候选 provenance 明确包含 `provider: "exa"`、`rank` 和脱敏稳定 `result_id`。
- 错误兼容现有顶层类别：未配置和凭据拒绝为 `AUTHENTICATION_ERROR`/退出码 6；限流、超时、网络、上游错误和无效响应为 `NETWORK_ERROR`/退出码 5。调用方用 `error.details.reason` 稳定区分 `not_configured`、`credential_rejected`、`rate_limited`、`timeout`、`network_error`、`provider_error`、`invalid_response`。
- 空搜索是 `ok: true` 的空候选结果；微信原文回读失败保留 per-candidate `hydration_attempt` 并令 `summary.partial: true`。`published_at`、公众号身份和 Article Evidence 仍只能来自微信原文回读。
- 普通测试仅使用 MockTransport、fixture、临时 SQLite 和假凭据存储；本轮未调用真实 Exa、微信、Chrome 或账号 API。live smoke 必须由用户另行明确授权。
- PR #18 已 squash 合入 `main`，合并提交为 `f40ddd7`；远端 Tests and type checks 与 Windows package smoke 均通过。
- 正式标签 `wechat-oa-v0.7.0` 和 GitHub Release 已发布。0.7.0 验证为 426 项 pytest 全绿、mypy 检查 48 个源文件无错误；PyInstaller 双命令包和全部离线冒烟通过。ZIP SHA-256 为 `f8fb9ae666cf8d72179ba93d974af50231740929509e5dd632371ed51408ea4b`。
- 已从公开 Release 重新下载 ZIP 和校验文件，实算哈希、发布校验值和固定哈希三者一致。稳定安装与 0.7.0 → 0.6.0 → 0.7.0 双向回滚通过，最终为 `current=0.7.0`、`previous=0.6.0`，canonical Skill 已同步。

## 0.6.0 发布收口（2026-09-02）

- canonical `main` 已通过 PR #15 合入单篇文章原子 Evidence Bundle，合并提交为
  `fc76831`。
- PR #16 已完成 0.6.0 发布准备并 squash 合入 `main`，合并提交为
  `2fa396b`。
- 0.6.0 范围包含 Media Evidence、安全图片下载与缓存、标准二维码、本地 Windows OCR、
  单篇和发现批次的显式媒体分析、Media Doctor，以及单篇 Article Evidence Bundle。
- schema-v2 Direct Discovery Request JSON 输入、Discovery Bundle 和派生 QR/OCR 缓存明确
  延期，不阻塞 0.6.0。
- 正式标签 `wechat-oa-v0.6.0` 和 GitHub Release 已发布；Windows x64 ZIP 的固定
  SHA-256 为 `2a9e584ca83b21e69ed2ed83ddc3e033f773c06c92b1d2301355448111e52e96`。
- 已从公开 Release 重新下载 ZIP 和 SHA-256 文件，下载内容实算哈希、校验
  文件和发布前候选哈希三者一致。
- 发布准备验证为 402 项 pytest 全绿、mypy 检查 47 个源文件无错误、canonical Skill
  校验通过、PyInstaller spike 与完整 Windows 双命令构建通过。默认 Doctor 的五项 live
  check 均保持 `skip`，Media Doctor 为 `overall: pass`。
- canonical Skill 的安装来源固定到 0.6.0 Release 及上述哈希；本机稳定安装与
  升级/回滚验收仍未执行，需用户另行明确授权。

## 历史项目接续摘要（2026-09-01）

### 当前工作位置

- canonical 源码现位于 monorepo 的 `E:\devlop\qiuC-tools\CLI\wechat-oa`。
- 原独立仓库工作区 `E:\devlop\Wechat-develop\wechat-oa` 暂时保留用于迁移兼容和既有发布构建，不再作为后续开发目标。
- 旧目录 `E:\devlop\WxCLi` 已经清空，但当时被原 Codex 任务占用，尚未创建目录联接。关闭旧任务后需明确二选一：不再兼容旧对话就删除该空目录；仍需兼容旧工作目录就先删除空目录，再创建指向新目录的目录联接。
- 旧对话记录的工作目录仍是旧路径，不能自动改写。后续开发应在 Codex 的 `wechat-oa` 新项目中创建新任务，并先阅读本文件。

### 已经完成

- 0.5.0 的浏览器兜底可靠性、真实外部搜索、微信原文回读和一次性可见 Chrome 验收已经完成。
- 产品在 0.5.1 正式命名为 **WeChat OA / 微信公众号助手**，首选命令和首选 Skill 均为 `wechat-oa`。
- 旧 `wxcli` 命令、Python 包名、运行状态目录、凭据标识和证据 provenance 全部保留兼容，因此招聘雷达不需要修改。
- Windows 0.5.1 发布包已经构建、安装并完成 0.5.1 → 0.5.0 → 0.5.1 双向回滚验收。
- Agent Reach 已优先探测 `wechat-oa`，缺失时回退到 `wxcli`；doctor 实测微信公众号后端为 0.5.1、状态正常。
- 招聘雷达保持原代码不变，实测通过 `wxcli` 获取 0.5.1，并完成 schema-v1 空候选回读管道。
- `qiuC123/qiuC-skills` 的 PR #3 已合并。远程仓库同时保留正式 `wechat-oa` Skill 和兼容 `wxcli` Skill。

### 当前 Git 与发布状态

- 本轮开发分支：`codex/browser-verification-navigation`，基于已经合并 PR #13 的 `main`。
- 0.5.1 验收提交：`af04cb2 docs: record WeChat OA 0.5.1 acceptance`。
- 原独立仓库的 `v0.5.1` 指向上述验收提交；monorepo 使用带工具名的发布标签，避免未来多个 CLI 的版本标签冲突。
- monorepo 发布标签：`wechat-oa-v0.5.1`。该标签指向迁移和发布元数据提交；运行时代码仍对应已经验收的 0.5.1 基线。
- 本地 `main` 已包含 0.5.1 命名更新、验收记录和本交接文档修正。
- canonical 源码已迁入 `https://github.com/qiuC123/qiuC-tools/tree/main/CLI/wechat-oa`；原 `qiuC123/wechat-oa` 暂时保留为迁移兼容仓库，不再作为后续开发目标。
- 正式发布包通过 GitHub Release 分发；本地 `dist\` 仍被 Git 忽略，不会随源码 push 或 clone 自动传输。
- 已发布 0.5.1 Release 的 SHA-256：`bb348471aea7dac2c1f4e80e4c6a815a509ec584ba09305999f1c08014bd360a`。
- `main` 已包含 0.6.0 安全图片下载器、原始字节 Media Cache、文章级数量/总字节编排、独立缓存清理命令和打包内置的本地标准二维码分析器，并已处理 PR #2/#3 的全部审阅项。
- `main` 的 PR #6 已修复 Chrome 在 `domcontentloaded` 后立即截图、静态解析器只识别 `<img>` 导致正文海报漏检的问题；真实文章复验从 4 个页尾图片 URL 提升到 33 个正文/页尾图片 URL，33 张图片全部通过安全下载器下载，Windows 本地 OCR 对 20 张产生非空文本且无引擎失败，真实图片和文字未写入 Git。
- PR #7 已合并可替换的 `OCRProvider`/`OCRRuntime` 与默认 `WindowsOCRProvider`：重新校验图片字节/哈希/尺寸/像素，最多 64 个重叠长图切片，稀疏结果最多一次受限放大、灰度和自动对比度重试，每次 Windows 进程限时 30 秒；不联网、不纠错，选择的预处理步骤进入 OCR Evidence。固定 PowerShell 桥接使用短期 PNG 和结构化 stdin，Base64 包装 UTF-8 JSON 防止控制台编码损坏，缺少引擎/语言与执行失败保持独立状态。合并前本地完整验证为 344 项 pytest 全绿，mypy 检查 43 个源文件无错误；真实本机合成中文图片字符码核对通过，PyInstaller 双命令发布包及离线冒烟通过。
- PR #8 已合并 QR/OCR 分析编排：对下载结果做防御性复验，相同图片字节按 SHA-256 只分析一次并映射回每个文章图片位置；QR 与 OCR 失败互相隔离，错误哈希/语言关联被替换为稳定失败证据；调用方降低后的图片、QR、OCR 和文章限额由编排层再次强制执行；下载省略数进入 Media Evidence、摘要和稳定哈希。合并前本地完整验证为 356 项 pytest 全绿，mypy 检查 44 个源文件无错误；PyInstaller 与远端 Windows 包冒烟通过。
- PR #9 已合并 `article get` 和 `article evidence` 的默认关闭 `--analyze-media`。显式启用后先生成 Article Evidence，再安全下载图片并返回外层 schema v2；不开启时不构造 Media Cache、下载器或分析器，原输出不变。媒体模式下的 `--no-cache` 同时禁用文章和图片缓存。合并前 362 项 pytest、mypy、Skill 校验和 Windows 打包 CI 通过。
- PR #10 已合并纯本地 `media doctor`：检查 JPEG/PNG/WebP/GIF，执行内存标准 QR 往返自检，通过受限 PowerShell 查询 Windows OCR、安装语言和默认 `zh-Hans` 可用性；不联网、不读凭据/缓存、不启动 Chrome、不安装组件。图片或 QR 缺失令整体失败，Windows OCR 缺失只保留可选能力状态。本地完整验证为 373 项 pytest 全绿，mypy 检查 45 个源文件无错误，canonical Skill 校验通过；开发环境和临时 PyInstaller onedir 中的 `wechat-oa`/`wxcli` 双命令均实测 `overall: pass`，OCR 语言为 `en-US`、`zh-Hans-CN`，默认语言可用。
- PR #11 已合并发现批次的显式媒体分析：`discovery search --hydrate --analyze-media` 和 `discovery hydrate --input ... --analyze-media` 只分析已经成功生成 Article Evidence 的候选；不开启时原 schema v1 输出与资源构造均不变。开启后返回外层 schema v2，完整保留 Direct Discovery 或 Candidate Ingestion schema v1，并按 `candidate_index`、`article_identity` 和 `content_sha256` 关联独立 Media Evidence。Candidate Batch 不能自行开启媒体分析、选择限额或授权浏览器；批次限额为最多 200 个图片项、400 MiB 下载字节和 1,000,000 个 OCR 字符。本地完整验证为 382 项 pytest 全绿，mypy 检查 46 个源文件无错误，canonical Skill 校验、PyInstaller spike 和完整 Windows 双命令发布包验收通过。
- 本轮新增 `browser verify ARTICLE_URL` 交互式验证流程，修复自动 fallback 识别验证码后立即关闭 Chrome、调用 Agent 误以为窗口仍在等待的问题。无人值守 fallback 继续立即返回 `VERIFICATION_REQUIRED`；只有用户另行明确授权时，新命令才在专用可见 profile 中打开确切文章并最多等待 5 分钟，只观察页面分类、不点击或绕过验证，成功后直接返回 Article Evidence。canonical Skill 明确禁止使用 computer-use、窗口激活或通用浏览器自动化接管验证窗口。本地完整验证为 387 项 pytest 全绿，mypy 检查 46 个源文件无错误，Skill 校验、PyInstaller spike 和完整 Windows 双命令发布包验收通过。
- 2026-09-02 的真实文章复验发现微信会把部分验证请求重定向到 `https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha`，该页面可能没有原有文本验证码标记，导致 PR #12 构建误报 `PARSING_ERROR` 并立即关闭 Chrome。本轮热修只把精确 HTTPS 主机 `mp.weixin.qq.com`、无凭据/显式端口、精确路径 `/mp/wappoc_appmsgcaptcha` 识别为验证页；交互命令继续等待用户手动处理，无人值守读取返回 `VERIFICATION_REQUIRED`。本地完整验证为 389 项 pytest 全绿，mypy 检查 46 个源文件无错误，PyInstaller spike 和完整 Windows 双命令发布包通过。未发布的 0.5.1 热修开发包 SHA-256 为 `e95e2a4973d8b119fb032e4a36dc3c94e796cf9afefe0690612711006c979586`；正式发版时应升版本并生成新的 Release，不应覆盖现有 0.5.1 资产。
- 同日安装 PR #13 热修包再做真实复验时，Playwright 在验证码页导航回文章页的瞬间调用 `Page.content`，返回“page is navigating and changing the content”，导致交互会话误报 `CHROME_ERROR`。第二个热修仅重试这一条明确的瞬时导航错误，其他 Playwright 错误仍立即失败，且重试继续受 5 分钟总截止时间约束。源码真实复验不再立即失败，完整等待 301.7 秒后因微信验证码提示“操作过于频繁，请稍后再试”而按设计返回 `VERIFICATION_REQUIRED`、`verification_outcome=timeout`；未绕过或自动操作验证码，也未取得文章证据。本地完整验证为 390 项 pytest 全绿，mypy 检查 46 个源文件无错误，PyInstaller spike 和完整 Windows 双命令发布包通过。最新未发布 0.5.1 热修开发包 SHA-256 为 `f5eb52df6736b3c42d141aab4938b9ba73c20c0f1f4d7e73d4378a0229fe638e`。

### 下一步计划

按以下顺序推进，避免同时做版本治理和新功能：

1. 0.6.0 的媒体模型、安全下载、原始字节缓存、二维码、Windows OCR、分析编排、单篇文章和发现批次的显式 CLI 控制、能力 Doctor 与单篇 Evidence Bundle 已完成；下一步完成版本、构建、Release 和安装/回滚收口。
2. WeChat OA 只输出稳定的 Article/Media Evidence，不负责其他项目如何消费、导入或同步这些数据。
3. 0.6.0 完成后重复离线测试、PyInstaller 构建、安装/回滚和显式授权的真实微信验收。

### 必须保持的边界

- WeChat OA 负责微信公众号文章、图片和草稿证据，不负责企业、招聘批次、岗位、ATS 或投递判断。
- 二维码内容只作为惰性证据返回，绝不自动打开、跳转或执行。
- OCR 默认使用本地能力，不把公众号图片上传到远程 OCR 服务。
- 不发布、不群发、不删除内容；已有草稿仍必须经过备份、差异、冻结计划、显式确认和回读验证。
- 不迁移 `%LOCALAPPDATA%\wxcli`、`%LOCALAPPDATA%\Programs\wxcli`、Windows 凭据键或历史 `wxcli` provenance；这些是有意保留的兼容接口。

## 0.5.1 命名更新（2026-08-30）

产品正式命名为 **WeChat OA / 微信公众号助手**，首选 CLI 和 Agent Skill 标识均为 `wechat-oa`。旧 `wxcli` 可执行命令继续保留相同参数、JSON、退出码和运行状态；Python 包名、`%LOCALAPPDATA%\wxcli` 数据目录、Windows 凭据标识与历史证据 provenance 也保持不变。因此招聘雷达和既有自动化无需修改。

本轮已完成：

- 版本升至 0.5.1，Python 安装同时生成 `wechat-oa` 和 `wxcli` 两个命令。
- Windows 发布目录同时包含 `wechat-oa.exe` 和 `wxcli.exe`；稳定安装根目录仍为 `%LOCALAPPDATA%\Programs\wxcli`。
- 新增 canonical `wechat-oa` Skill；`wxcli` Skill 缩为兼容入口，并引导新任务使用 canonical Skill。
- Agent Reach 微信渠道优先探测 `wechat-oa`，不存在时才回退到 `wxcli`。
- 新增 ADR-0008，冻结产品名称及兼容边界。

验证结果：

```text
pytest: 237 passed
mypy: Success: no issues found in 36 source files
Skill validation: wechat-oa and wxcli both valid
Agent Reach doctor: wechat status ok, active_backend wechat-oa, version 0.5.1
Recruitment Radar: unchanged WxCliClient reported (0, 5, 1) and completed schema-v1 empty-candidate hydration
```

正式 PyInstaller onedir 产物为 `dist\release\wechat-oa-0.5.1-windows-x64.zip`，SHA-256 为 `bb348471aea7dac2c1f4e80e4c6a815a509ec584ba09305999f1c08014bd360a`。安装 0.5.1、回滚到 0.5.0、再双向切回 0.5.1 均已验证；最终 `current` 为 0.5.1，`previous` 为 0.5.0，两个命令都返回 0.5.1，两个用户级 Skill 均已同步。

0.5.1 只改变产品命名和分发入口，没有改变 0.5.0 的网络、浏览器、文章证据或草稿安全契约；下面保留 0.5.0 的真实外部验收记录。

## 0.5.0 开发更新（2026-08-30）

> 本节是 2026-08-30 的历史验收记录，不代表当前分支、安装版本或待办状态；当前状态以文档顶部“新项目接续摘要”为准。

0.5.0 的浏览器兜底可靠性功能已完成源码实现、离线验证和稳定目录安装，并快进合并到当时的本地 `main`。当时本机 `current` 为 0.5.0，`previous` 为已验收的 0.4.0；下面的 0.4.0 内容保留为历史验收记录和回滚依据。

本轮实现包括：

- 默认关闭、可持久设置的 Browser Fallback Policy；`--browser-fallback` 单次授权与最高优先级的 `--no-browser`。
- HTTP 始终优先，只有微信验证页才进入可见 Chrome；原有单篇 `--browser` 继续表示直接使用 Chrome。
- 一个批次最多创建一个 Chrome persistent context，每篇候选使用新标签页，结束后关闭；独立 profile 中的浏览器会话继续保留，但 wxcli 不导入、导出或显示 Cookie。
- 浏览器锁最多等待五秒；浏览器挑战、崩溃、超时和 `BROWSER_BUSY` 都形成安全的结构化结果，不自动重启 Chrome，也不丢弃已完成的 Evidence。
- `browser login` 不再把“窗口正常结束”误报为登录有效；只有成功的真实 Chrome 文章回读才记录 `last_successful_read_at`。
- 0.6.0 的版本化 Media Evidence、Media Item、QR、OCR 和外层 schema-v2 数据模型已经实现；图片下载、缓存、二维码/OCR 分析器、CLI 接入和 Evidence Bundle 尚未实现。

当次离线验证：

```text
pytest: 235 passed
mypy: Success: no issues found in 36 source files
coverage: repository 88%; browser_policy 98%; hydration 96%; Chrome Provider 91%
```

已完成 0.5.0 的独立 PyInstaller onedir 构建和脚本内置离线冒烟，产物为 `dist\release\wxcli-0.5.0-windows-x64.zip`，SHA-256 为 `41bd019295566279bb9a5356c88945aa319ca098c4fb7940002c98fc5de0ef00`。原子安装、回滚到 0.4.0、重新安装以及双向回滚均已成功，最终 `current` 为 0.5.0，`previous` 为 0.4.0。用户级 Skill 的 7 个仓库管理文件与源码哈希一致，安装目录另有 `.wxcli-version` 版本标记文件。

`wxcli --json browser policy status` 当时实际返回 `never / configured:false / valid:true`；策略文件不存在，因此使用安全缺省值。招聘雷达也已通过 PATH 实际启动 0.5.0 完成空候选、零网络的 schema-v1 管道验收。招聘雷达当时完整 477 项测试通过，660 秒 wxcli 等待上限已经包含在提交 `9d65d03 feat(radar): enforce official announcement admission` 中。

2026-08-30 已在用户明确授权后完成安装版 0.5.0 的真实 Agent-first / Exa / 微信 / Chrome 验收：外部搜索产生 18 个严格 Public URL 候选，wxcli 接受 18 个、尝试回读 17 个并验证成功 17 个，`partial:false`；批次使用 CLI 单次 `auto-fallback`，一个 Browser Run 回读 17 个候选，`user_action_required:0`。另用未回读候选验证单篇路径：`--no-browser` 正确以退出码 6 返回 `VERIFICATION_REQUIRED`，随后 `--browser-fallback` 以退出码 0 成功提取非空正文和图片。验收结束后无 wxcli Chrome 进程残留，长期策略仍为 `never / configured:false / valid:true`。真实 URL、标题和正文未写入 Git。

---

以下内容是 2026-08-29 的 0.4.0 已安装版本快照。

> 快照日期：2026-08-29
>
> 快照版本：0.4.0
>
> 快照代码状态：0.4.0 已合并到当时的本地 `main`，尚未打 tag、配置远程仓库或发布外部 Release

## 1. 0.4.0 快照结论

wxcli 0.4.0 的开发、离线测试、Windows onedir 打包、稳定目录安装、回滚验证和真实 Agent-first 微信文章发现验收均已完成。当时本机服役版本是 0.4.0，保留 0.3.0 作为一键回滚版本。

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

## 2. Git、版本和安装状态快照

- 快照分支：`main`
- 快照提交：`2b9c53a fix: reject empty public article evidence`
- 原功能分支：`codex/safe-resumable-draft-updates`，目前与 `main` 指向同一提交
- 工作区在生成本交接文件前是干净的
- 项目版本：`pyproject.toml` 与 `src/wxcli/__init__.py` 均为 0.4.0
- Git remote：未配置
- Git tag：尚未创建
- 外部 push / PR / Release：均未执行

当时的本机稳定安装布局：

```text
%LOCALAPPDATA%\Programs\wxcli\
  current\       # 当时为 0.4.0，PATH 永远指向这里
  previous\      # 上一个 0.3.0，可回滚
  skills\<版本>\ # 对应版本的 wxcli Skill 快照
```

当时命令解析到：

```text
C:\Users\Mayn\AppData\Local\Programs\wxcli\current\wxcli.exe
```

用户级 wxcli Skill 当时已同步为 0.4.0。

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

## 7. 0.4.0 限制和当时未完成事项

### 产品限制

- 微信没有开放完整公众号文章搜索 API，发现覆盖率取决于外部搜索 Provider 的收录和延迟。
- HTTP 回读经常遇到微信验证页；Chrome 也不能保证每篇文章都可读取。
- wxcli 0.4.0 不做持久浏览器自动兜底、二维码识别、二维码目标提取或图片 OCR。
- 不下载完整证据包，不继续访问外链，不处理官网或 ATS 内容。
- 不进行企业、招聘公告、批次、岗位或申请渠道的业务分类。

### 快照时尚未完成的发布工作

- 尚未配置 Git remote。
- 尚未 push 本地 `main`。
- 尚未创建 `v0.4.0` tag。
- 尚未发布外部 Release。
- 原功能分支尚未删除；它与 `main` 当前指向同一提交，保留不会影响运行。

### 文档提示

`docs/release-windows.md` 现在明确按所选 Direct Discovery Provider 配置 Brave 或 Exa；Agent-first Candidate Batch 路径仍不需要向 wechat-oa 传递搜索 Key。

## 8. 当时建议的下一步

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
git show -s --oneline wechat-oa-v0.5.1
wechat-oa --version
wechat-oa --json doctor
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m mypy src
```

预期结果：分支为 `main`，存在 `wechat-oa-v0.5.1` 发布标签，版本为 0.5.1，Doctor 默认不执行 live checks；本轮热修分支测试 390 项通过，mypy 零问题。真实微信/Chrome live smoke 只在用户单独明确授权后执行，不属于默认接手检查。
