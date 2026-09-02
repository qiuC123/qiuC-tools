"""Static release contract tests; the full build is exercised by PowerShell."""

from pathlib import Path
import tomllib

from wxcli import __version__


ROOT = Path(__file__).parents[1]


def test_source_and_package_versions_match() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == __version__
    assert project["project"]["scripts"]["wechat-oa"] == "wxcli.cli:main"
    assert project["project"]["scripts"]["wxcli"] == "wxcli.cli:main"


def test_direct_runtime_imports_are_declared_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "click>=8.1,<9.0" in project["project"]["dependencies"]


def test_release_is_windows_x64_onedir_zip_with_checksum() -> None:
    script = (ROOT / "scripts" / "build-release.ps1").read_text(encoding="utf-8")

    assert "--onedir" in script
    assert "--onefile" not in script
    assert "windows-x64" in script
    assert "Compress-Archive" in script
    assert "Get-FileHash" in script
    assert "SHA256" in script
    assert "status --porcelain" in script
    assert "PowerShell 7" in script
    assert '--name wechat-oa' in script
    assert "'wechat-oa.exe'" in script
    assert "'wxcli.exe'" in script


def test_release_contains_install_and_cleanup_guidance() -> None:
    instructions = (ROOT / "docs" / "release-windows.md").read_text(encoding="utf-8")

    assert "安装" in instructions
    assert "SHA-256" in instructions
    assert "browser clear" in instructions
    assert "browser policy set never" in instructions
    assert "cache clear" in instructions
    assert "凭据管理器" in instructions


def test_live_smoke_requires_explicit_flags_and_a_discovery_capable_executable() -> None:
    script = (ROOT / "scripts" / "live-discovery-smoke.ps1").read_text(encoding="utf-8")

    assert "$WxcliPath" in script
    assert "0.5.0" in script
    assert "AllowLiveSearch" in script
    assert "AllowLiveWeChat" in script
    assert "AllowBrowser" in script


def test_agent_first_live_smoke_keeps_search_wechat_and_browser_authority_separate() -> None:
    script = (ROOT / "scripts" / "live-agent-discovery-smoke.ps1").read_text(
        encoding="utf-8"
    )

    assert "AllowLiveAgentSearch" in script
    assert "AllowLiveWeChat" in script
    assert "AllowBrowser" in script
    assert "exec --ephemeral --sandbox read-only" in script
    assert "--output-schema" in script
    assert "-o $batchPath -" in script
    assert "$batch.discovery_request.query -ne $Query" in script
    assert "discovery', 'hydrate" in script
    assert "0.5.0" in script
    assert "api_key" not in script.casefold()


def test_packaged_discovery_help_includes_candidate_hydration() -> None:
    script = (ROOT / "scripts" / "build-release.ps1").read_text(encoding="utf-8")

    assert "'search', 'hydrate', 'auth', 'cache'" in script
    assert "browser', 'policy', 'status'" in script
    assert "browser verify command is missing" in script
    assert "smoke-localappdata" in script
    assert "'media', 'doctor'" in script
    assert "standard_qr.available" in script
    assert "discovery media controls are missing" in script
    assert script.count("--analyze-media") >= 2
    assert "Evidence Bundle controls are missing" in script
    assert "Evidence Bundle preflight smoke test failed" in script
    assert "bundle-preflight-existing" in script


def test_agent_reach_installer_can_refresh_the_official_personal_skill() -> None:
    script = (ROOT / "scripts" / "install-agent-reach-integration.ps1").read_text(
        encoding="utf-8"
    )

    assert "$packageSkillRoot" in script
    assert "Install-AgentReachPersonalSkill" in script
    assert "$RefreshAgentReachSkill" in script
    assert '".wechat-oa-backups"' in script
