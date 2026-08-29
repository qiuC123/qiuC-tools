[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,
    [switch]$Rollback,
    [switch]$SkipPath,
    [switch]$SkipSkill
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'The wxcli installer requires PowerShell 7 or newer. Run it with pwsh.'
}
if (-not $IsWindows) {
    throw 'wxcli can only be installed on Windows.'
}
if ($Rollback -and $PSBoundParameters.ContainsKey('Version')) {
    throw '-Version and -Rollback cannot be used together.'
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$distRoot = Join-Path $projectRoot 'dist\release'
$localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$userProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
if ([string]::IsNullOrWhiteSpace($localAppData) -or [string]::IsNullOrWhiteSpace($userProfile)) {
    throw 'Windows user profile directories could not be resolved.'
}

$installRoot = Join-Path $localAppData 'Programs\wxcli'
$currentDirectory = Join-Path $installRoot 'current'
$previousDirectory = Join-Path $installRoot 'previous'
$newDirectory = Join-Path $installRoot 'current.new'
$swapDirectory = Join-Path $installRoot 'current.swap-tmp'
$skillSnapshotsRoot = Join-Path $installRoot 'skills'
$sourceSkillDirectory = Join-Path $projectRoot 'skills\wxcli'
$agentSkillsRoot = Join-Path $userProfile '.agents\skills'
$installedSkillDirectory = Join-Path $agentSkillsRoot 'wxcli'
$warnings = [System.Collections.Generic.List[string]]::new()
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Assert-SafeInstallPath([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $resolvedRoot = [System.IO.Path]::GetFullPath($installRoot).TrimEnd('\')
    $rootPrefix = $resolvedRoot + '\'
    if (-not $resolved.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the wxcli installation root: $resolved"
    }
    return $resolved
}

function Remove-SafeInstallDirectory([string]$Path) {
    $resolved = Assert-SafeInstallPath $Path
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

function Move-SafeInstallDirectory([string]$Source, [string]$Destination) {
    $resolvedSource = Assert-SafeInstallPath $Source
    $resolvedDestination = Assert-SafeInstallPath $Destination
    if (-not (Test-Path -LiteralPath $resolvedSource -PathType Container)) {
        throw "The directory to rename does not exist: $resolvedSource"
    }
    if (Test-Path -LiteralPath $resolvedDestination) {
        throw "The rename destination already exists: $resolvedDestination"
    }
    [System.IO.Directory]::Move($resolvedSource, $resolvedDestination)
}

function Remove-SafeInstalledSkillDirectory {
    $resolved = [System.IO.Path]::GetFullPath($installedSkillDirectory)
    $expected = [System.IO.Path]::GetFullPath((Join-Path $agentSkillsRoot 'wxcli'))
    if (-not $resolved.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove an unexpected Agent Skill directory: $resolved"
    }
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

function Copy-DirectoryContents([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "The source directory does not exist: $Source"
    }
    if (Test-Path -LiteralPath $Destination) {
        throw "The copy destination already exists: $Destination"
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($item in @(Get-ChildItem -LiteralPath $Source -Force)) {
        Copy-Item -LiteralPath $item.FullName -Destination $Destination -Recurse -Force
    }
}

function Assert-NoRunningInstalledProcess {
    $resolvedRoot = [System.IO.Path]::GetFullPath($installRoot).TrimEnd('\')
    $rootPrefix = $resolvedRoot + '\'
    $matches = [System.Collections.Generic.List[string]]::new()
    foreach ($process in @(Get-Process)) {
        try {
            $processPath = $process.Path
        }
        catch {
            continue
        }
        if ([string]::IsNullOrWhiteSpace($processPath)) {
            continue
        }
        $resolvedProcessPath = [System.IO.Path]::GetFullPath($processPath)
        if ($resolvedProcessPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $matches.Add("$($process.ProcessName) (PID $($process.Id))")
        }
    }
    if ($matches.Count -gt 0) {
        throw "Close running wxcli processes before installing or rolling back: $($matches -join ', ')"
    }
}

function Invoke-PackagedWxcli([string[]]$CliArguments, [int[]]$ExpectedExitCodes = @(0)) {
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $script:executable
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
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
        throw "Installed wxcli returned unexpected exit code $($result.ExitCode)."
    }
    return $result
}

function ConvertFrom-SingleJson([object]$Result) {
    if (-not [string]::IsNullOrEmpty($Result.Stderr)) {
        throw 'Installed wxcli unexpectedly wrote to stderr in JSON mode.'
    }
    $lines = @($Result.Stdout.TrimEnd() -split "`r?`n")
    if ($lines.Count -ne 1) {
        throw 'Installed wxcli did not emit exactly one JSON document.'
    }
    return $Result.Stdout | ConvertFrom-Json -ErrorAction Stop
}

function Read-ReleaseVersion([string]$Directory) {
    $versionFile = Join-Path $Directory 'VERSION.txt'
    if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
        throw "The release is missing VERSION.txt: $Directory"
    }
    $releaseVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
    if ($releaseVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "The release contains an invalid version: $releaseVersion"
    }
    return $releaseVersion
}

function Test-ReleaseDirectory([string]$Directory) {
    $releaseVersion = Read-ReleaseVersion $Directory
    $script:executable = Join-Path $Directory 'wxcli.exe'
    if (-not (Test-Path -LiteralPath $script:executable -PathType Leaf)) {
        throw "The release is missing wxcli.exe: $Directory"
    }

    $plainVersion = Invoke-PackagedWxcli @('--version')
    if ($plainVersion.Stdout.Trim() -ne $releaseVersion -or -not [string]::IsNullOrEmpty($plainVersion.Stderr)) {
        throw 'Installed wxcli plain version smoke test failed.'
    }
    $jsonVersion = ConvertFrom-SingleJson (Invoke-PackagedWxcli @('--json', '--version'))
    if (-not $jsonVersion.ok -or $jsonVersion.data.version -ne $releaseVersion) {
        throw 'Installed wxcli JSON version smoke test failed.'
    }

    $doctor = ConvertFrom-SingleJson (Invoke-PackagedWxcli @('--json', 'doctor') @(0, 1))
    if (-not $doctor.ok) {
        throw 'Installed wxcli offline doctor smoke test failed.'
    }
    foreach ($checkName in @('network', 'stable_token', 'ip_allowlist', 'draft_permission', 'published_permission')) {
        $checks = @($doctor.data.checks | Where-Object { $_.name -eq $checkName })
        if ($checks.Count -ne 1 -or $checks[0].status -ne 'skip') {
            throw "Installed wxcli doctor did not skip live check: $checkName"
        }
    }
    return $releaseVersion
}

function Install-AgentSkill([string]$Source, [string]$ReleaseVersion) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "The wxcli Skill source does not exist: $Source"
    }
    New-Item -ItemType Directory -Path $agentSkillsRoot -Force | Out-Null
    Remove-SafeInstalledSkillDirectory
    Copy-DirectoryContents $Source $installedSkillDirectory
    [System.IO.File]::WriteAllText(
        (Join-Path $installedSkillDirectory '.wxcli-version'),
        "$ReleaseVersion`n",
        $utf8NoBom
    )
}

function Snapshot-And-InstallAgentSkill([string]$ReleaseVersion) {
    New-Item -ItemType Directory -Path $skillSnapshotsRoot -Force | Out-Null
    $snapshotDirectory = Join-Path $skillSnapshotsRoot $ReleaseVersion
    Remove-SafeInstallDirectory $snapshotDirectory
    Copy-DirectoryContents $sourceSkillDirectory $snapshotDirectory
    Install-AgentSkill $sourceSkillDirectory $ReleaseVersion
}

function Restore-AgentSkill([string]$ReleaseVersion) {
    $snapshotDirectory = Join-Path $skillSnapshotsRoot $ReleaseVersion
    if (-not (Test-Path -LiteralPath $snapshotDirectory -PathType Container)) {
        $warnings.Add("The Agent Skill snapshot for wxcli $ReleaseVersion is missing; the installed Skill was left unchanged.")
        return $false
    }
    Install-AgentSkill $snapshotDirectory $ReleaseVersion
    return $true
}

function Get-NormalizedPathEntry([string]$Entry) {
    $trimmed = $Entry.Trim().Trim('"').TrimEnd('\')
    if ([string]::IsNullOrWhiteSpace($trimmed)) {
        return ''
    }
    try {
        $expanded = [Environment]::ExpandEnvironmentVariables($trimmed)
        return [System.IO.Path]::GetFullPath($expanded).TrimEnd('\')
    }
    catch {
        return $trimmed
    }
}

function Configure-UserPath {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $entries = @(
        @($userPath -split ';') |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    $normalizedCurrent = Get-NormalizedPathEntry $currentDirectory
    $stableEntries = @(
        $entries |
            Where-Object {
                (Get-NormalizedPathEntry $_).Equals(
                    $normalizedCurrent,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            }
    )
    $pathChanged = $false
    if ($stableEntries.Count -eq 0) {
        $newUserPath = if ([string]::IsNullOrWhiteSpace($userPath)) {
            $currentDirectory
        }
        else {
            "$currentDirectory;$userPath"
        }
        [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
        $entries = @($currentDirectory) + $entries
        $pathChanged = $true
    }
    elseif ($stableEntries.Count -gt 1) {
        $warnings.Add('The stable wxcli PATH entry appears more than once; remove duplicate entries manually.')
    }

    foreach ($entry in $entries) {
        $normalizedEntry = Get-NormalizedPathEntry $entry
        if (
            $entry -match '(?i)wxcli' -and
            -not $normalizedEntry.Equals($normalizedCurrent, [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            $warnings.Add("An existing wxcli PATH entry was left unchanged: $entry. Remove it manually after verification.")
        }
    }
    return $pathChanged
}

function Invoke-AtomicSwap {
    if (Test-Path -LiteralPath $swapDirectory) {
        throw "A stale rollback swap directory exists: $swapDirectory"
    }
    Move-SafeInstallDirectory $currentDirectory $swapDirectory
    try {
        Move-SafeInstallDirectory $previousDirectory $currentDirectory
    }
    catch {
        $exchangeError = $_
        try {
            Move-SafeInstallDirectory $swapDirectory $currentDirectory
        }
        catch {
            throw "Rollback exchange failed and the original current directory could not be restored: $($_.Exception.Message)"
        }
        throw $exchangeError
    }
    try {
        Move-SafeInstallDirectory $swapDirectory $previousDirectory
    }
    catch {
        $exchangeError = $_
        try {
            Move-SafeInstallDirectory $currentDirectory $previousDirectory
            Move-SafeInstallDirectory $swapDirectory $currentDirectory
        }
        catch {
            throw "Rollback exchange failed and the original directory layout could not be restored: $($_.Exception.Message)"
        }
        throw $exchangeError
    }
}

function Invoke-Install([string]$RequestedVersion) {
    $sourceDirectory = Join-Path $distRoot "wxcli-$RequestedVersion-windows-x64"
    if (-not (Test-Path -LiteralPath $sourceDirectory -PathType Container)) {
        throw "The release source directory does not exist: $sourceDirectory"
    }
    $sourceVersion = Read-ReleaseVersion $sourceDirectory
    if ($sourceVersion -ne $RequestedVersion) {
        throw "The release directory version $RequestedVersion does not match VERSION.txt ($sourceVersion)."
    }

    Assert-NoRunningInstalledProcess
    New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
    if (Test-Path -LiteralPath $swapDirectory) {
        throw "A stale rollback swap directory exists: $swapDirectory"
    }
    Remove-SafeInstallDirectory $newDirectory
    try {
        Copy-DirectoryContents $sourceDirectory $newDirectory
        $candidateVersion = Test-ReleaseDirectory $newDirectory
    }
    catch {
        Remove-SafeInstallDirectory $newDirectory
        throw
    }

    $hadCurrent = Test-Path -LiteralPath $currentDirectory -PathType Container
    if ($hadCurrent) {
        Remove-SafeInstallDirectory $previousDirectory
        Move-SafeInstallDirectory $currentDirectory $previousDirectory
    }
    try {
        Move-SafeInstallDirectory $newDirectory $currentDirectory
    }
    catch {
        $installError = $_
        $restoreError = $null
        if ($hadCurrent -and -not (Test-Path -LiteralPath $currentDirectory) -and (Test-Path -LiteralPath $previousDirectory)) {
            try {
                Move-SafeInstallDirectory $previousDirectory $currentDirectory
            }
            catch {
                $restoreError = $_
            }
        }
        Remove-SafeInstallDirectory $newDirectory
        if ($null -ne $restoreError) {
            throw "Installing wxcli failed, and the previous version could not be restored: $($restoreError.Exception.Message)"
        }
        throw $installError
    }
    return $candidateVersion
}

function Invoke-Rollback {
    if (-not (Test-Path -LiteralPath $previousDirectory -PathType Container)) {
        throw "No previous wxcli installation is available to roll back: $previousDirectory"
    }
    if (-not (Test-Path -LiteralPath $currentDirectory -PathType Container)) {
        throw "The current wxcli installation is missing: $currentDirectory"
    }
    Assert-NoRunningInstalledProcess
    Invoke-AtomicSwap
    try {
        return Test-ReleaseDirectory $currentDirectory
    }
    catch {
        $validationError = $_
        try {
            Invoke-AtomicSwap
        }
        catch {
            throw "Rolled-back wxcli failed validation, and the original version could not be restored: $($_.Exception.Message)"
        }
        throw "Rolled-back wxcli failed validation; the original version was restored. $($validationError.Exception.Message)"
    }
}

if (-not $Rollback -and [string]::IsNullOrWhiteSpace($Version)) {
    $Version = (& py -3.12 -c "import pathlib,tomllib; print(tomllib.loads(pathlib.Path(r'$projectRoot\pyproject.toml').read_text(encoding='utf-8'))['project']['version'])").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'The default release version could not be read from pyproject.toml.'
    }
}
if (-not $Rollback -and $Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Unsupported release version: $Version"
}

$action = if ($Rollback) { 'rollback' } else { 'install' }
$installedVersion = if ($Rollback) {
    Invoke-Rollback
}
else {
    Invoke-Install $Version
}

$pathChanged = $false
if (-not $SkipPath) {
    $pathChanged = Configure-UserPath
    $warnings.Add('User PATH changes apply only to newly opened terminals.')
}

$skillSynced = $false
if (-not $SkipSkill) {
    if ($Rollback) {
        $skillSynced = Restore-AgentSkill $installedVersion
    }
    else {
        Snapshot-And-InstallAgentSkill $installedVersion
        $skillSynced = $true
    }
}

[pscustomobject]@{
    action = $action
    version = $installedVersion
    current = $currentDirectory
    previous = if (Test-Path -LiteralPath $previousDirectory) { $previousDirectory } else { $null }
    path_changed = $pathChanged
    skill_synced = $skillSynced
    warnings = @($warnings | ForEach-Object { $_ })
} | ConvertTo-Json -Depth 4
