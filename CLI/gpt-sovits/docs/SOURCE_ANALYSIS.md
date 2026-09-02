# GPT-SoVITS source analysis

## Relevant upstream surfaces

- `api_v2.py`: real local inference API used by the managed service.
- `GPT_SoVITS/configs/tts_infer.yaml`: source configuration copied into isolated CLI state.
- Upstream dataset preparation scripts: invoked by the preprocessing workflow.
- Upstream SoVITS and GPT training entry points: invoked by bounded training plans.
- Pretrained model directories: required locally but never included in this repository.

## Harness modules

- `core/config.py`: portable option and environment discovery.
- `core/service.py`: managed process lifecycle, configuration isolation, and structured logs.
- `core/audio.py`: WAV and FFprobe validation.
- `core/models.py`: local model discovery and guarded selection.
- `core/dataset.py`, `core/workflow.py`, `core/uvr.py`: local candidate preparation.
- `core/phase2b.py`, `core/training.py`: plan validation, preprocessing, training, and status.
- `gpt_sovits_cli.py`: Click command surface and REPL.

## Design conclusions

The harness must keep GPT-SoVITS as a hard local dependency. It should never synthesize through
a replacement implementation, silently connect to a remote API, or treat process exit alone as
proof of a valid result. The public snapshot therefore includes source and tests only; runtime
assets stay local.
