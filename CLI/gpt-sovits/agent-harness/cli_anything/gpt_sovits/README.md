# cli-anything-gpt-sovits

Install the optional MCP adapter from the harness source with `pip install -e ".[mcp]"`.
Run `gpt-sovits-mcp --checkout <absolute-source-path> --runtime <backend-python-path>`,
or `python -m cli_anything.gpt_sovits.mcp_server`.
The stdio server exposes ten inference tools; dataset and training remain CLI-only.
See `CLI/gpt-sovits/docs/MCP.md` in the source repository for client configuration.

This Python package provides the installed `cli-anything-gpt-sovits` command. It requires a
separate, compatible GPT-SoVITS checkout and uses the real upstream service and training scripts.

## Configuration

```text
checkout: --checkout > GPT_SOVITS_CHECKOUT > ~/GPT-SoVITS
runtime:  --runtime > GPT_SOVITS_RUNTIME > <checkout>/.conda/python.exe (Windows)
                                      or <checkout>/.conda/bin/python (other platforms)
```

Additional overrides are available for the loopback API URL, inference configuration, and state
directory. Run `cli-anything-gpt-sovits --help` for the exact options.

## Usage

```powershell
cli-anything-gpt-sovits doctor --json
cli-anything-gpt-sovits serve status --json
cli-anything-gpt-sovits synthesize --help
cli-anything-gpt-sovits dataset --help
cli-anything-gpt-sovits training --help
cli-anything-gpt-sovits training plan --help
```

Use `--dry-run --json` before every mutation. The managed service is loopback-only, and the CLI
does not automatically publish, upload, or redistribute outputs and models.
Training plan identity and language are explicit inputs (`--speaker`, `--language`), while the
approved-count gate is optional and otherwise follows the manifest.

## Testing

Install the package editable, set `GPT_SOVITS_TEST_CHECKOUT` to a real compatible checkout, set
`CLI_ANYTHING_FORCE_INSTALLED=1`, and run the package test directory. Missing real backend
configuration is a failure, not a skip.

This subproject has no independently declared open-source license. Public visibility does not
grant model or data redistribution rights; upstream and asset licenses still apply.
