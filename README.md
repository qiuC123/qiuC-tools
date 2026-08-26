# wxcli

`wxcli` is a Windows-only, read-only command-line tool for viewing WeChat Official Account content.

This repository is being built in small, reviewed steps. It will never publish, delete, modify account content, bypass verification, or export browser cookies.

## Development setup

Use Windows PowerShell with Python 3.12:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
pytest
mypy src
wxcli --version
```

## Packaging spike

The current packaging check creates a Windows x64 PyInstaller `onedir` folder only; it does not create a release:

```powershell
.\scripts\build-spike.ps1
.\dist\spike\wxcli\wxcli.exe --version
```

## Security boundary

Secrets, access tokens, cookies, browser profiles, and runtime state are excluded from Git. Future credential setup will store AppSecret and access tokens in the Windows credential manager; it will not accept secrets in command arguments.
