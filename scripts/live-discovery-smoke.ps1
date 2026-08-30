[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Query,
    [string]$WxcliPath = 'wxcli',
    [switch]$AllowLiveSearch,
    [switch]$AllowLiveWeChat,
    [switch]$AllowBrowser
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $AllowLiveSearch) {
    throw 'Real Brave search requires -AllowLiveSearch.'
}
if ($AllowLiveWeChat -and -not $AllowLiveSearch) {
    throw 'Real WeChat hydration also requires -AllowLiveSearch.'
}
if ($AllowBrowser -and -not $AllowLiveWeChat) {
    throw 'Visible Chrome requires both -AllowLiveSearch and -AllowLiveWeChat.'
}

$versionOutput = (& $WxcliPath --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "The selected wxcli executable could not report its version: $WxcliPath"
}
$versionMatch = [regex]::Match($versionOutput, '(?<version>\d+\.\d+\.\d+)')
if (-not $versionMatch.Success -or [version]$versionMatch.Groups['version'].Value -lt [version]'0.5.0') {
    throw "Live discovery smoke requires wxcli 0.5.0 or newer; selected executable reported: $versionOutput"
}

$arguments = @('--json', 'discovery', 'search', $Query, '--limit', '5')
if ($AllowLiveWeChat) {
    $arguments += @('--hydrate', '--priority-hydrate', '3', '--max-hydrate', '3')
}
if ($AllowBrowser) {
    $arguments += '--browser-fallback'
}

$output = & $WxcliPath @arguments
if ($LASTEXITCODE -ne 0) {
    throw "wxcli live discovery smoke failed with exit code $LASTEXITCODE."
}
$result = $output | ConvertFrom-Json -ErrorAction Stop
if (-not $result.ok -or $result.data.schema_version -ne '1') {
    throw 'wxcli live discovery smoke returned an invalid contract.'
}
$result | ConvertTo-Json -Depth 20
