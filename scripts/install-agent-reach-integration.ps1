[CmdletBinding()]
param(
    [string]$AgentReachPython,
    [string]$PersonalSkillsRoot
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$userProfilePath = [Environment]::GetFolderPath("UserProfile")
if ([string]::IsNullOrWhiteSpace($AgentReachPython)) {
    $AgentReachPython = Join-Path $userProfilePath ".agent-reach-venv\Scripts\python.exe"
}
if ([string]::IsNullOrWhiteSpace($PersonalSkillsRoot)) {
    $PersonalSkillsRoot = Join-Path $userProfilePath ".agents\skills"
}

$sourceSkill = Join-Path $repoRoot "skills\wxcli"
$sourceChannel = Join-Path $repoRoot "integrations\agent-reach\wechat.py"
$sourceReference = Join-Path $repoRoot "integrations\agent-reach\wechat.md"
$requiredFiles = @(
    (Join-Path $sourceSkill "SKILL.md"),
    $sourceChannel,
    $sourceReference,
    $AgentReachPython
)
foreach ($requiredFile in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required file not found: $requiredFile"
    }
}

$packageRoot = (& $AgentReachPython -c "import pathlib, agent_reach; print(pathlib.Path(agent_reach.__file__).resolve().parent)").Trim()
if ([string]::IsNullOrWhiteSpace($packageRoot)) {
    throw "Could not locate the installed agent_reach package."
}
$packageRoot = (Resolve-Path -LiteralPath $packageRoot).Path
$registryPath = Join-Path $packageRoot "channels\__init__.py"
$channelTarget = Join-Path $packageRoot "channels\wechat.py"
if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) {
    throw "Agent Reach channel registry not found: $registryPath"
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    [System.IO.File]::WriteAllText($Path, $Content, [System.Text.UTF8Encoding]::new($false))
}

function Add-AgentReachSkillRoute([string]$SkillRoot) {
    $skillPath = Join-Path $SkillRoot "SKILL.md"
    if (-not (Test-Path -LiteralPath $skillPath -PathType Leaf)) {
        return
    }
    $referenceRoot = Join-Path $SkillRoot "references"
    New-Item -ItemType Directory -Path $referenceRoot -Force | Out-Null
    Copy-Item -LiteralPath $sourceReference -Destination (Join-Path $referenceRoot "wechat.md") -Force

    $backupPath = "$skillPath.wxcli-backup"
    if (-not (Test-Path -LiteralPath $backupPath)) {
        Copy-Item -LiteralPath $skillPath -Destination $backupPath
    }

    $content = [System.IO.File]::ReadAllText($skillPath, [System.Text.Encoding]::UTF8)
    $routeRow = "| 微信公众号文章/公众号账号 | wechat | [references/wechat.md](references/wechat.md) |"
    if (-not $content.Contains($routeRow)) {
        $anchor = "| 网页/文章/RSS | web | [references/web.md](references/web.md) |"
        if (-not $content.Contains($anchor)) {
            throw "Agent Reach SKILL.md route-table anchor changed; refusing an unsafe patch: $skillPath"
        }
        $content = $content.Replace($anchor, "$routeRow`r`n$anchor")
    }
    $referenceLine = "- [微信公众号](references/wechat.md) — wxcli 文章读取和经确认的 Word 草稿导入"
    $legacyReferenceLine = "- [微信公众号](references/wechat.md) — wxcli 只读文章、草稿和已发布内容"
    $content = $content.Replace($legacyReferenceLine, $referenceLine)
    if (-not $content.Contains($referenceLine)) {
        $anchor = "- [网页阅读](references/web.md) — Jina Reader, RSS"
        if (-not $content.Contains($anchor)) {
            throw "Agent Reach SKILL.md reference anchor changed; refusing an unsafe patch: $skillPath"
        }
        $content = $content.Replace($anchor, "$referenceLine`r`n$anchor")
    }
    $content = $content.Replace("15 platforms", "16 platforms").Replace("15 平台", "16 平台")
    Write-Utf8NoBom -Path $skillPath -Content $content
}

New-Item -ItemType Directory -Path $PersonalSkillsRoot -Force | Out-Null
$personalSkillTarget = Join-Path $PersonalSkillsRoot "wxcli"
New-Item -ItemType Directory -Path $personalSkillTarget -Force | Out-Null
Copy-Item -Path (Join-Path $sourceSkill "*") -Destination $personalSkillTarget -Recurse -Force

$registryBackup = "$registryPath.wxcli-backup"
if (-not (Test-Path -LiteralPath $registryBackup)) {
    Copy-Item -LiteralPath $registryPath -Destination $registryBackup
}
Copy-Item -LiteralPath $sourceChannel -Destination $channelTarget -Force

$registry = [System.IO.File]::ReadAllText($registryPath, [System.Text.Encoding]::UTF8)
$importLine = "from .wechat import WeChatChannel"
if (-not $registry.Contains($importLine)) {
    $importAnchor = "from .web import WebChannel"
    if (-not $registry.Contains($importAnchor)) {
        throw "Agent Reach registry import anchor changed; refusing an unsafe patch: $registryPath"
    }
    $registry = $registry.Replace($importAnchor, "$importLine`r`n$importAnchor")
}
if (-not $registry.Contains("    WeChatChannel(),")) {
    $listAnchor = "    WebChannel(),"
    if (-not $registry.Contains($listAnchor)) {
        throw "Agent Reach registry list anchor changed; refusing an unsafe patch: $registryPath"
    }
    $registry = $registry.Replace($listAnchor, "    WeChatChannel(),`r`n$listAnchor")
}
if (-not $registry.Contains('"WeChatChannel"')) {
    $exportAnchor = '    "ALL_CHANNELS",'
    if (-not $registry.Contains($exportAnchor)) {
        throw "Agent Reach registry export anchor changed; refusing an unsafe patch: $registryPath"
    }
    $registry = $registry.Replace($exportAnchor, "    `"WeChatChannel`",`r`n$exportAnchor")
}
Write-Utf8NoBom -Path $registryPath -Content $registry

$activeAgentReachSkill = Join-Path $PersonalSkillsRoot "agent-reach"
Add-AgentReachSkillRoute -SkillRoot $activeAgentReachSkill
Add-AgentReachSkillRoute -SkillRoot (Join-Path $packageRoot "skill")

Write-Output "Installed wxcli skill: $personalSkillTarget"
Write-Output "Registered Agent Reach channel: $channelTarget"
Write-Output "Patched Agent Reach routing references."
