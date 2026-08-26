"""Static release contract tests; the full build is exercised by PowerShell."""

from pathlib import Path
import tomllib

from wxcli import __version__


ROOT = Path(__file__).parents[1]


def test_source_and_package_versions_match() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == __version__


def test_release_is_windows_x64_onedir_zip_with_checksum() -> None:
    script = (ROOT / "scripts" / "build-release.ps1").read_text(encoding="utf-8")

    assert "--onedir" in script
    assert "--onefile" not in script
    assert "windows-x64" in script
    assert "Compress-Archive" in script
    assert "Get-FileHash" in script
    assert "SHA256" in script
    assert "status --porcelain" in script


def test_release_contains_install_and_cleanup_guidance() -> None:
    instructions = (ROOT / "docs" / "release-windows.md").read_text(encoding="utf-8")

    assert "安装" in instructions
    assert "SHA-256" in instructions
    assert "browser clear" in instructions
    assert "cache clear" in instructions
    assert "凭据管理器" in instructions
