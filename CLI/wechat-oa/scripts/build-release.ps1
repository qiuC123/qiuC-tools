[CmdletBinding()]
param(
    [switch]$AllowDirty,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'The release build requires PowerShell 7 or newer. Run it with pwsh.'
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$buildRoot = Join-Path $projectRoot 'build\release'
$distRoot = Join-Path $projectRoot 'dist\release'

function Assert-LastCommand([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Remove-SafeProjectDirectory([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $projectRoot.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a directory outside the project: $resolved"
    }
    if ($resolved -in @($projectRoot, $rootPrefix.TrimEnd('\'))) {
        throw 'Refusing to remove the project root.'
    }
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

function Remove-SafeProjectFile([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $projectRoot.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a file outside the project: $resolved"
    }
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Force
    }
}

function Invoke-PackagedWxcli([string[]]$CliArguments, [int[]]$ExpectedExitCodes = @(0)) {
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $script:executable
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.Environment['LOCALAPPDATA'] = (Join-Path $buildRoot 'smoke-localappdata')
    foreach ($argument in $CliArguments) {
        $start.ArgumentList.Add($argument)
    }
    $process = [System.Diagnostics.Process]::Start($start)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $result = [pscustomobject]@{
        ExitCode = $process.ExitCode
        Stdout = $stdoutTask.Result
        Stderr = $stderrTask.Result
    }
    if ($result.ExitCode -notin $ExpectedExitCodes) {
        throw "Packaged wxcli returned unexpected exit code $($result.ExitCode)."
    }
    return $result
}

function ConvertFrom-SingleJson([object]$Result) {
    if (-not [string]::IsNullOrEmpty($Result.Stderr)) {
        throw 'Packaged wxcli unexpectedly wrote to stderr in JSON mode.'
    }
    $lines = @($Result.Stdout.TrimEnd() -split "`r?`n")
    if ($lines.Count -ne 1) {
        throw 'Packaged wxcli did not emit exactly one JSON document.'
    }
    return $Result.Stdout | ConvertFrom-Json -ErrorAction Stop
}

if (-not $IsWindows) {
    throw 'The wxcli V1 release can only be built on Windows.'
}
if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
    throw 'The wxcli V1 release must be built on Windows x64.'
}
if (-not $AllowDirty) {
    $dirty = @(& git -C $projectRoot status --porcelain --untracked-files=all)
    Assert-LastCommand 'Git worktree check'
    if ($dirty.Count -gt 0) {
        throw 'The Git worktree must be clean for a formal release build.'
    }
}

$version = (& py -3.12 -c "import pathlib,tomllib; print(tomllib.loads(pathlib.Path(r'$projectRoot\pyproject.toml').read_text(encoding='utf-8'))['project']['version'])").Trim()
Assert-LastCommand 'Version lookup'
if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Unsupported release version: $version"
}
$artifactName = "wechat-oa-$version-windows-x64"
$artifactDirectory = Join-Path $distRoot $artifactName
$zipPath = Join-Path $distRoot "$artifactName.zip"
$checksumPath = "$zipPath.sha256"
$stagingRoot = Join-Path (Join-Path $distRoot '.staging') $artifactName

$existingArtifacts = @(
    @($artifactDirectory, $zipPath, $checksumPath) |
        Where-Object { Test-Path -LiteralPath $_ }
)
if ($existingArtifacts.Count -gt 0 -and -not $Force) {
    $existingList = $existingArtifacts -join ', '
    throw "Release artifacts already exist for ${version}: $existingList. Re-run with -Force to replace only this version."
}
if ($Force) {
    Remove-SafeProjectDirectory $artifactDirectory
    Remove-SafeProjectFile $zipPath
    Remove-SafeProjectFile $checksumPath
}

Remove-SafeProjectDirectory $buildRoot
Remove-SafeProjectDirectory $stagingRoot
New-Item -ItemType Directory -Path $buildRoot, $stagingRoot -Force | Out-Null

$buildEnvironment = Join-Path $buildRoot 'venv'
& py -3.12 -m venv $buildEnvironment
Assert-LastCommand 'Clean Python environment creation'
$buildPython = Join-Path $buildEnvironment 'Scripts\python.exe'

& $buildPython -m pip install --disable-pip-version-check --quiet $projectRoot
Assert-LastCommand 'Runtime dependency installation'
& $buildPython -m pip install --disable-pip-version-check --quiet 'pyinstaller>=6.7,<7.0'
Assert-LastCommand 'PyInstaller installation'

& $buildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --name wechat-oa `
    --paths (Join-Path $projectRoot 'src') `
    --collect-all playwright `
    --collect-submodules keyring.backends `
    --exclude-module pytest `
    --exclude-module mypy `
    --distpath $stagingRoot `
    --workpath (Join-Path $buildRoot 'pyinstaller') `
    --specpath (Join-Path $buildRoot 'spec') `
    (Join-Path $projectRoot 'src\wxcli\__main__.py')
Assert-LastCommand 'PyInstaller onedir build'

Move-Item -LiteralPath (Join-Path $stagingRoot 'wechat-oa') -Destination $artifactDirectory
Remove-SafeProjectDirectory $stagingRoot
Copy-Item -LiteralPath (Join-Path $artifactDirectory 'wechat-oa.exe') -Destination (Join-Path $artifactDirectory 'wxcli.exe')
Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $artifactDirectory
Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $artifactDirectory
Copy-Item -LiteralPath (Join-Path $projectRoot 'docs\release-windows.md') -Destination (Join-Path $artifactDirectory 'INSTALL.md')
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText(
    (Join-Path $artifactDirectory 'VERSION.txt'),
    "$version`n",
    $utf8NoBom
)

$script:executable = Join-Path $artifactDirectory 'wechat-oa.exe'
$plainVersion = Invoke-PackagedWxcli @('--version')
if ($plainVersion.Stdout.Trim() -ne $version -or -not [string]::IsNullOrEmpty($plainVersion.Stderr)) {
    throw 'Packaged WeChat OA version smoke test failed.'
}
$jsonVersion = ConvertFrom-SingleJson (Invoke-PackagedWxcli @('--json', '--version'))
if (-not $jsonVersion.ok -or $jsonVersion.data.version -ne $version) {
    throw 'Packaged WeChat OA JSON version smoke test failed.'
}
$script:executable = Join-Path $artifactDirectory 'wxcli.exe'
$legacyVersion = Invoke-PackagedWxcli @('--version')
if ($legacyVersion.Stdout.Trim() -ne $version -or -not [string]::IsNullOrEmpty($legacyVersion.Stderr)) {
    throw 'Packaged wxcli compatibility command smoke test failed.'
}
$script:executable = Join-Path $artifactDirectory 'wechat-oa.exe'
$help = Invoke-PackagedWxcli @('--help')
foreach ($command in @('article', 'account', 'auth', 'browser', 'cache', 'discovery', 'doctor')) {
    if ($help.Stdout -notmatch "\b$command\b") {
        throw "Packaged WeChat OA help is missing command: $command"
    }
}
$smokeArticle = Join-Path $buildRoot 'smoke-article.md'
[System.IO.File]::WriteAllText($smokeArticle, "# 打包冒烟`n`nUTF-8 正文`n", $utf8NoBom)
$localArticle = ConvertFrom-SingleJson (
    Invoke-PackagedWxcli @('--json', 'article', 'local', $smokeArticle)
)
if (-not $localArticle.ok -or $localArticle.data.title -ne '打包冒烟') {
    throw 'Packaged WeChat OA local article smoke test failed.'
}
$invalidInput = ConvertFrom-SingleJson (
    Invoke-PackagedWxcli @('--json', 'article', 'local') @(2)
)
if ($invalidInput.ok -or $invalidInput.error.code -ne 'INVALID_ARGUMENT') {
    throw 'Packaged WeChat OA invalid-argument smoke test failed.'
}
$authStatus = ConvertFrom-SingleJson (Invoke-PackagedWxcli @('--json', 'auth', 'status'))
if (-not $authStatus.ok) {
    throw 'Packaged WeChat OA keyring smoke test failed.'
}
$discoveryHelp = Invoke-PackagedWxcli @('discovery', '--help')
foreach ($command in @('search', 'hydrate', 'auth', 'cache')) {
    if ($discoveryHelp.Stdout -notmatch "\b$command\b") {
        throw "Packaged wxcli discovery help is missing command: $command"
    }
}
$discoveryAuthStatus = ConvertFrom-SingleJson (
    Invoke-PackagedWxcli @('--json', 'discovery', 'auth', 'status', '--provider', 'brave')
)
if (-not $discoveryAuthStatus.ok) {
    throw 'Packaged wxcli discovery keyring status smoke test failed.'
}
$browserStatus = ConvertFrom-SingleJson (Invoke-PackagedWxcli @('--json', 'browser', 'status'))
if (-not $browserStatus.ok) {
    throw 'Packaged wxcli browser status smoke test failed.'
}
$browserPolicyStatus = ConvertFrom-SingleJson (
    Invoke-PackagedWxcli @('--json', 'browser', 'policy', 'status')
)
if (-not $browserPolicyStatus.ok -or $browserPolicyStatus.data.policy -ne 'never') {
    throw 'Packaged wxcli browser policy smoke test failed.'
}
$mediaDoctor = ConvertFrom-SingleJson (
    Invoke-PackagedWxcli @('--json', 'media', 'doctor') @(0, 1)
)
if (
    -not $mediaDoctor.ok -or
    $mediaDoctor.data.overall -ne 'pass' -or
    -not $mediaDoctor.data.standard_qr.available -or
    $mediaDoctor.data.windows_ocr.availability -notin @('available', 'unavailable', 'failed')
) {
    throw 'Packaged wxcli Media Doctor smoke test failed.'
}
$doctor = ConvertFrom-SingleJson (Invoke-PackagedWxcli @('--json', 'doctor') @(0, 1))
$liveChecks = @($doctor.data.checks | Where-Object { $_.name -in @('network', 'stable_token', 'ip_allowlist', 'draft_permission', 'published_permission') })
if (-not $doctor.ok -or @($liveChecks | Where-Object { $_.status -ne 'skip' }).Count -gt 0) {
    throw 'Packaged wxcli doctor attempted or reported an unauthorized live check.'
}

Compress-Archive -LiteralPath $artifactDirectory -DestinationPath $zipPath -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText(
    $checksumPath,
    "$hash  $([System.IO.Path]::GetFileName($zipPath))`n",
    $utf8NoBom
)

Remove-SafeProjectDirectory $buildRoot

[pscustomobject]@{
    version = $version
    directory = $artifactDirectory
    zip = $zipPath
    sha256 = $hash
} | ConvertTo-Json
