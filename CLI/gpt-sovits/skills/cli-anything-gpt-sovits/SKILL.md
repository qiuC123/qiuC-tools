---
name: cli-anything-gpt-sovits
description: Operate a local GPT-SoVITS checkout through an agent-friendly CLI for diagnostics, managed inference, dataset preparation, and bounded training.
---

# cli-anything-gpt-sovits

Use this Skill when an agent needs to inspect or operate a separately installed GPT-SoVITS
checkout through the `cli-anything-gpt-sovits` command.

## Prerequisites

- Python 3.10 or newer
- A compatible local GPT-SoVITS checkout and runtime
- FFmpeg on `PATH`
- Local model files whose licenses permit the intended use

Install the harness from `CLI/gpt-sovits/agent-harness` with `python -m pip install -e .`.

## Backend discovery

The checkout is selected by `--checkout`, then `GPT_SOVITS_CHECKOUT`, then
`~/GPT-SoVITS`. The runtime is selected by `--runtime`, then `GPT_SOVITS_RUNTIME`,
then the checkout's `.conda/python.exe` on Windows or `.conda/bin/python` elsewhere.

## Command groups

- `doctor`: read-only backend and API diagnostics.
- `serve start/status/stop/logs`: lifecycle for a CLI-managed loopback service.
- `model list/use-gpt/use-sovits`: inspect or select local weights for that service.
- `reference inspect`: validate reference audio.
- `synthesize`: call the real API and verify a non-streaming WAV.
- `dataset ...`: extract, inspect, transcribe, manifest, index, and compare candidates.
- `training doctor/plan/preprocess/run/status/download-uvr`: bounded local workflows.

For `training plan`, pass `--speaker` and `--language` when a manifest is ambiguous. Use
`--expected-approved-count` only when an independently approved count must be enforced; otherwise
the plan records the manifest's actual approved count.

## Agent procedure

1. Run `doctor --json`.
2. Inspect inputs before changing state.
3. Add `--dry-run --json` to every mutation first.
4. Start only a loopback service managed by this CLI.
5. Verify the active runtime model state before synthesis or training.
6. Inspect returned WAV metadata and hashes instead of trusting the exit code.
7. Stop the service and confirm its status when finished.

Example:

```powershell
$env:GPT_SOVITS_CHECKOUT = '<path-to-gpt-sovits>'
cli-anything-gpt-sovits doctor --json
cli-anything-gpt-sovits serve start --dry-run --json
cli-anything-gpt-sovits synthesize --help
```

## Safety and publication boundary

Do not commit, upload, or publish model weights, reference media, datasets, prompts, generated
audio, logs, or state. This subproject has no independently declared open-source license. Public
visibility does not grant redistribution rights, and all upstream, model, voice, data, and media
licenses remain applicable.
