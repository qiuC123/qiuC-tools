# wxcli Windows x64 安装与清理

## 安装

1. 校验下载的 ZIP：

   ```powershell
   Get-FileHash .\wxcli-0.1.0-windows-x64.zip -Algorithm SHA256
   Get-Content .\wxcli-0.1.0-windows-x64.zip.sha256
   ```

   两处 SHA-256 应完全相同。

2. 解压整个 ZIP。`wxcli.exe` 必须和 `_internal` 目录保持在一起，不能只复制 EXE。
3. 在解压目录中运行：

   ```powershell
   .\wxcli-0.1.0-windows-x64\wxcli.exe --version
   .\wxcli-0.1.0-windows-x64\wxcli.exe doctor
   ```

4. 如需在当前 PowerShell 窗口直接输入 `wxcli`，可临时加入 PATH：

   ```powershell
   $env:Path = "$(Resolve-Path .\wxcli-0.1.0-windows-x64);$env:Path"
   wxcli --help
   ```

本程序不需要 Python，但可见浏览器功能需要安装在默认路径的 Google Chrome。首次使用官方接口时，运行 `wxcli auth configure`，凭证会进入 Windows 凭据管理器。

## 升级

把新 ZIP 解压到一个新目录并使用其中的 `wxcli.exe`。运行状态保存在 `%LOCALAPPDATA%\wxcli`，不会因为替换程序目录而丢失。

## 清理与卸载

先关闭所有 wxcli 和它打开的 Chrome 窗口，然后按需要执行：

```powershell
# 删除公共文章缓存
.\wxcli.exe cache clear

# 删除 wxcli 专用 Chrome profile 和本地浏览器状态
.\wxcli.exe browser clear
```

随后可以删除解压出的 `wxcli-0.1.0-windows-x64` 目录。程序没有安装系统服务，也不会创建外部 Release。

如果还要彻底删除账号配置：

1. 打开 Windows“凭据管理器”，删除由 `wxcli` 创建的凭据条目。
2. 删除 `%LOCALAPPDATA%\wxcli` 目录，其中只有普通配置、到期时间、缓存和专用浏览器资料；不要把该目录内容发送给他人。

删除这些本地数据不可撤销，并会要求下次重新配置或登录。
