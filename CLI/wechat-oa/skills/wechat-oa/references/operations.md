# 本地操作与诊断

## 安装来源

- canonical 源码：`https://github.com/qiuC123/qiuC-tools/tree/main/CLI/wechat-oa`
- 固定发布页：`https://github.com/qiuC123/qiuC-tools/releases/tag/wechat-oa-v0.5.1`
- Windows x64 ZIP：`https://github.com/qiuC123/qiuC-tools/releases/download/wechat-oa-v0.5.1/wechat-oa-0.5.1-windows-x64.zip`
- SHA-256 文件：`https://github.com/qiuC123/qiuC-tools/releases/download/wechat-oa-v0.5.1/wechat-oa-0.5.1-windows-x64.zip.sha256`
- 固定 SHA-256：`bb348471aea7dac2c1f4e80e4c6a815a509ec584ba09305999f1c08014bd360a`

只有 Windows x64 支持该发布包。`wechat-oa` 与 `wxcli` 都不存在时，先向用户说明下载
版本、来源、哈希和目标位置；只有取得明确授权后才能下载或解压。默认解压到新建的临时
目录，使用绝对路径执行离线检查，不修改用户级 `PATH` 或持久安装目录：

```powershell
$releaseTemp = Join-Path ([System.IO.Path]::GetTempPath()) `
  ("wechat-oa-0.5.1-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $releaseTemp | Out-Null
$releaseZip = Join-Path $releaseTemp "wechat-oa-0.5.1-windows-x64.zip"
$releaseHash = "$releaseZip.sha256"
Invoke-WebRequest `
  "https://github.com/qiuC123/qiuC-tools/releases/download/wechat-oa-v0.5.1/wechat-oa-0.5.1-windows-x64.zip" `
  -OutFile $releaseZip
Invoke-WebRequest `
  "https://github.com/qiuC123/qiuC-tools/releases/download/wechat-oa-v0.5.1/wechat-oa-0.5.1-windows-x64.zip.sha256" `
  -OutFile $releaseHash
$actualHash = (Get-FileHash $releaseZip -Algorithm SHA256).Hash.ToLowerInvariant()
$publishedHash = ((Get-Content $releaseHash -Raw).Trim() -split "\s+")[0].ToLowerInvariant()
$pinnedHash = "bb348471aea7dac2c1f4e80e4c6a815a509ec584ba09305999f1c08014bd360a"
if ($actualHash -ne $publishedHash -or $actualHash -ne $pinnedHash) {
    throw "WeChat OA release checksum mismatch"
}
Expand-Archive -LiteralPath $releaseZip -DestinationPath $releaseTemp
$releaseExe = Join-Path $releaseTemp `
  "wechat-oa-0.5.1-windows-x64\wechat-oa.exe"
& $releaseExe --version
& $releaseExe --json doctor
```

必须同时看到版本 `0.5.1`、进程退出码 `0` 和 Doctor JSON 的 `ok: true` 才算临时
安装可用。真实网络、官方接口和 Chrome 不属于安装验收。持久安装、用户级 `PATH` 修改、
升级或回滚必须由用户另行明确请求，并遵循源码目录中的 `docs/release-windows.md`。

## Doctor

默认 Doctor 不执行真实网络或账号检查：

```powershell
wechat-oa --json doctor
```

只有用户明确授权真实只读检查时使用：

```powershell
wechat-oa --json doctor --allow-live-api
```

## 浏览器

```powershell
wechat-oa --json browser status
wechat-oa --json browser login
wechat-oa --json browser clear
wechat-oa --json browser policy status
wechat-oa browser policy set auto-fallback
wechat-oa browser policy set never
```

- `status` 绝不启动 Chrome，只报告 `profile_exists`、旧版迁移时间和真实文章成功读取
  产生的 `last_successful_read_at`；这些本地事实不证明远端会话当前有效。
- `login` 打开 wechat-oa 独立、可见、持久的 Chrome profile，供用户手工登录或
  验证；窗口正常结束不等于文章已经成功读取。
- `clear` 删除该独立 profile 和本地状态记录。它是破坏性本地操作，必须获得
  用户明确请求。
- profile 有跨进程锁；被占用时不要并发启动第二个 wechat-oa Chrome。
- `policy set auto-fallback` 是一次持久授权，只能在用户明确要求后运行；默认策略为
  `never`。`browser clear` 不修改策略，`--no-browser` 可以禁止单次调用。

## 缓存

```powershell
wechat-oa --json cache clear
```

只清理公共文章成功缓存。由于它会删除本地数据，必须由用户明确请求。
