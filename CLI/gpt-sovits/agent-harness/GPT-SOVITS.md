# GPT-SoVITS harness architecture

## Backend

The harness wraps a separately installed GPT-SoVITS checkout. Inference uses the upstream
`api_v2.py` service, while dataset preparation and training commands invoke the upstream scripts
through an isolated runtime configuration. The CLI verifies real WAV output rather than
reimplementing speech synthesis.

## Command map

| Domain | Commands | Backend behavior |
| --- | --- | --- |
| Diagnostics | `doctor`, `training doctor` | Read-only dependency and path inspection |
| Service | `serve start/status/stop/logs` | Managed loopback API process with identity checks |
| Models | `model list/use-gpt/use-sovits` | Managed runtime configuration only |
| Inference | `reference inspect`, `synthesize` | Reference validation and real API synthesis |
| Dataset | `dataset extract/inspect/manifest/transcribe/prepare/index/proofread-index/uvr-compare` | FFmpeg, optional local ASR/UVR, and traceable manifests |
| Training | `training plan/preprocess/run/status/download-uvr` | Approved-only plans and bounded upstream subprocesses |

## State and safety

- The service binds only to loopback addresses.
- Mutations expose JSON output and `--dry-run`.
- State writes are locked and atomic.
- The CLI copies the upstream inference configuration into its state directory before changing
  managed model paths.
- Training plans bind manifests, configurations, model metadata, output paths, and resume
  checkpoints before execution.
- Training speaker, language, and optional approved-count gates are plan inputs; the harness does
  not embed a project-specific identity or dataset size.
- Logs use structured, sanitized events and do not persist prompt or synthesis text.

## Rendering gap assessment

There is no fallback synthesizer. A successful command must come from the real GPT-SoVITS API,
and the resulting WAV is checked for RIFF/WAVE structure, format metadata, duration, size, and
non-silence. Real E2E tests fail if the backend is unavailable.

## Packaging

The package uses the PEP 420 `cli_anything` namespace, exposes the
`cli-anything-gpt-sovits` console entry point, and includes the packaged CLI Skill.
