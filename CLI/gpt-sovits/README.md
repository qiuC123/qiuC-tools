# GPT-SoVITS CLI harness

This directory contains an agent-friendly CLI that operates a local
[GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) checkout through its real API and
training scripts. It does not include GPT-SoVITS itself, model weights, datasets, or media.

## Publication and license boundary

This subproject has not declared an independent open-source license. Public visibility does
not grant permission to redistribute this source, model weights, datasets, reference audio,
or generated media. Review and comply with the upstream GPT-SoVITS license and the licenses
for every model and dataset you use.

See [PUBLICATION_SCOPE.md](docs/PUBLICATION_SCOPE.md) for the source-only publication boundary
and [MODEL_TRANSFER.md](docs/MODEL_TRANSFER.md) for local model migration guidance.

## Install

Prerequisites:

- Python 3.10 or newer for this harness
- A compatible local GPT-SoVITS checkout with its own working Python environment
- FFmpeg available on `PATH`

```powershell
cd CLI/gpt-sovits/agent-harness
python -m pip install -e .
cli-anything-gpt-sovits --help
```

The checkout is resolved in this order:

1. `--checkout`
2. `GPT_SOVITS_CHECKOUT`
3. `~/GPT-SoVITS`

The runtime is resolved from `--runtime`, then `GPT_SOVITS_RUNTIME`, then the checkout's
`.conda/python.exe` on Windows or `.conda/bin/python` on other platforms.

## Main command groups

- `doctor`: inspect the local backend, runtime, GPU, models, FFmpeg, and API.
- `serve`: start, inspect, stop, and read sanitized logs for a CLI-managed loopback service.
- `model`: list or select local GPT and SoVITS weights for that managed service.
- `reference`: validate reference audio.
- `synthesize`: call the real backend and verify the returned WAV.
- `dataset`: extract, inspect, transcribe, manifest, index, and compare local candidates.
- `training`: inspect prerequisites, create a bounded plan, preprocess, run, and report status.

`training plan` uses a neutral `speaker` identifier by default. It accepts `--speaker` and
`--language`, and an optional `--expected-approved-count` gate; when the language or count is
omitted, the CLI derives it from the approved manifest instead of assuming a project identity.

All commands support machine-readable JSON. Mutating commands support `--dry-run` and should
be dry-run first.

```powershell
$env:GPT_SOVITS_CHECKOUT = '<path-to-gpt-sovits>'
cli-anything-gpt-sovits doctor --json
cli-anything-gpt-sovits serve start --dry-run --json
cli-anything-gpt-sovits reference inspect '<reference.wav>' --json
```

The API URL must be loopback-only. Model selection changes only the CLI-managed runtime
configuration; it does not silently overwrite the upstream default configuration.

## Tests

Real backend tests are mandatory and fail when no checkout is configured. They do not skip or
replace synthesis with a fake backend.

```powershell
$env:GPT_SOVITS_TEST_CHECKOUT = '<path-to-gpt-sovits>'
$env:GPT_SOVITS_CHECKOUT = $env:GPT_SOVITS_TEST_CHECKOUT
cd CLI/gpt-sovits/agent-harness
python -m pip install --no-deps -e .
$env:CLI_ANYTHING_FORCE_INSTALLED = '1'
python -m pytest cli_anything/gpt_sovits/tests -q
```

Test fixtures may create temporary synthetic weights or WAV files, but generated artifacts and
real assets are excluded from Git.
