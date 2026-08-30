# wxcli Windows x64 安装、升级与清理

## 安装

1. 校验下载的 ZIP：

   ```powershell
   Get-FileHash .\wxcli-0.5.0-windows-x64.zip -Algorithm SHA256
   Get-Content .\wxcli-0.5.0-windows-x64.zip.sha256
   ```

   两处 SHA-256 应完全相同。

2. 解压整个 ZIP。`wxcli.exe` 必须和 `_internal` 目录保持在一起，不能只复制 EXE。维护者从仓库构建时，解压后的完整目录位于 `dist\release\wxcli-<版本>-windows-x64`。
3. 在仓库根目录运行安装脚本：

   ```powershell
   pwsh .\scripts\install-release.ps1 -Version 0.5.0
   ```

   脚本先把完整产物复制到临时目录并完成离线冒烟测试，然后通过同一 NTFS 卷内的目录改名切换版本。安装位置固定为：

   ```text
   %LOCALAPPDATA%\Programs\wxcli\current
   ```

4. 安装脚本会把 `current` prepend 到用户级 PATH；PATH 只需配置一次，以后升级和回滚都不需要修改。请新开一个终端，再运行 `wxcli --version`。旧的 wxcli PATH 条目只会产生警告，脚本不会自动删除，请在确认新版本可用后人工清理。
5. 默认还会把仓库中的 `skills\wxcli` 同步到 `%USERPROFILE%\.agents\skills\wxcli`，并在固定安装根目录中保存按版本编号的 Skill 快照。测试或多版本并存时可以显式使用 `-SkipPath` 或 `-SkipSkill`。

本程序不需要 Python，但可见浏览器功能需要安装在默认路径的 Google Chrome。首次使用官方接口时运行 `wxcli auth configure`；首次使用文章发现时运行 `wxcli discovery auth configure --provider brave`。两类密钥分别进入 Windows 凭据管理器。

人工真实测试应明确选择稳定安装目录中的可执行文件，避免当前终端继续使用 PATH 中的旧版本：

```powershell
$wxcliExe = Join-Path $env:LOCALAPPDATA 'Programs\wxcli\current\wxcli.exe'
.\scripts\live-discovery-smoke.ps1 -Query '校园招聘' -WxcliPath $wxcliExe -AllowLiveSearch
```

加入 `-AllowLiveWeChat` 才会回读微信原文；再加入 `-AllowBrowser` 才可能打开可见 Chrome。

Agent-first 的人工测试另用 Codex CLI 和 Agent Reach/Exa，三个权限开关相互独立：

```powershell
# 只搜索并输出 Candidate Batch，不访问微信原文
.\scripts\live-agent-discovery-smoke.ps1 -Query '校园招聘' -CodexPath 'codex' -AllowLiveAgentSearch

# 再授权 wxcli 回读微信原文；只有额外加 -AllowBrowser 才可能打开 Chrome
.\scripts\live-agent-discovery-smoke.ps1 -Query '校园招聘' -CodexPath 'codex' `
  -WxcliPath $wxcliExe `
  -AllowLiveAgentSearch -AllowLiveWeChat
```

该脚本使用临时、只读的 `codex exec` 和 schema 约束输出；普通 CI 与离线打包测试不会运行它。

## 升级

把新版本的完整产物放入 `dist\release\wxcli-<版本>-windows-x64`，然后运行：

```powershell
# 默认读取 pyproject.toml 中的版本
pwsh .\scripts\install-release.ps1
```

脚本在 `current.new` 完成复制和离线冒烟测试。通过后，原 `current` 政名为 `previous`，`current.new` 再改名为 `current`；任何时候都不在服役目录中原地覆盖文件。如果最后一次改名失败，脚本会把 `previous` 自动恢复为 `current`。

只保留一个 `previous`：再次安装新版本时，原有 `previous` 会先删除，当前服役版本成为新的 `previous`。运行状态仍保存在 `%LOCALAPPDATA%\wxcli`，不会因为程序目录切换而丢失。0.5.0 的 `browser-policy.json` 与独立 Chrome profile 也位于这里；回滚到 0.4.0 时旧程序会忽略策略文件，再滚回 0.5.0 后继续使用。

## 回滚

先关闭正在运行的 wxcli，再执行：

```powershell
pwsh .\scripts\install-release.ps1 -Rollback
```

脚本通过三次目录改名交换 `current` 和 `previous`，再对新的 `current` 运行离线冒烟测试；测试失败会自动反向交换。再次执行同一命令可以滚回升级后的版本。若存在对应版本的 Skill 快照，回滚时会同时恢复用户级 Skill；快照缺失只会警告，不会阻断程序回滚。

## 清理与卸载

先关闭所有 wxcli 和它打开的 Chrome 窗口，然后按需要执行：

```powershell
# 删除公共文章缓存
wxcli cache clear

# 删除 discovery 搜索缓存、候选历史和 checkpoint 状态；不删除 Brave 凭据
wxcli discovery cache clear

# 删除 wxcli 专用 Chrome profile 和本地浏览器状态
wxcli browser clear

# 撤销持久浏览器自动回退授权；browser clear 不会替你修改策略
wxcli browser policy set never
```

随后可以删除 `%LOCALAPPDATA%\Programs\wxcli`，并从用户级 PATH 人工删除 `%LOCALAPPDATA%\Programs\wxcli\current`。如不再需要 Agent Skill，也可以删除 `%USERPROFILE%\.agents\skills\wxcli`。程序没有安装系统服务，也不会创建外部 Release。

如果还要彻底删除账号配置：

1. 打开 Windows“凭据管理器”，删除由 `wxcli` 创建的凭据条目。
2. 删除 `%LOCALAPPDATA%\wxcli` 目录，其中只有普通配置、到期时间、缓存和专用浏览器资料；不要把该目录内容发送给他人。

删除这些本地数据不可撤销，并会要求下次重新配置或登录。

## 从源码构建（维护者）

正式打包脚本需要 PowerShell 7 或更高版本，请用 `pwsh .\scripts\build-release.ps1` 运行。Windows 自带的旧 PowerShell 5.1 不能执行该构建脚本，但不影响已经打包好的 `wxcli.exe`。
