# GPT-SoVITS CLI public test plan

## Scope

The public suite validates the source-only harness without embedding models, media, datasets,
machine-specific paths, logs, state, or acceptance evidence. Synthetic fixtures cover unit and
contract behavior. Real E2E coverage invokes an explicitly configured GPT-SoVITS checkout and
must produce and inspect a real WAV.

## Test inventory

- `test_core.py`: configuration precedence, local URL policy, state locking, managed service,
  model selection, audio validation, JSON envelopes, CLI contracts, and subproject-wide private
  asset ignore rules.
- `test_full_e2e.py`: installed-command workflows, synthetic local API contracts, and one real
  backend synthesis with RIFF/WAVE, FFprobe, non-silence, log-redaction, and cleanup checks.
- `test_phase2a.py`: dataset extraction, manifests, offline ASR/UVR boundaries, path safety, and
  repeatable workflow contracts.
- `test_phase2b.py`: approved-only plans, artifact trust, isolated configuration, preprocessing,
  training runner contracts, resume safety, and completion status.

## Portability regression plan

Configuration tests prove:

1. an explicit checkout overrides `GPT_SOVITS_CHECKOUT`;
2. the environment overrides the portable home-directory default;
3. the fallback checkout is `~/GPT-SoVITS`;
4. an explicit runtime overrides `GPT_SOVITS_RUNTIME`;
5. the runtime fallback is platform-specific.

The real E2E test reads its checkout only from `GPT_SOVITS_TEST_CHECKOUT` or
`GPT_SOVITS_CHECKOUT`. A missing setting is an explicit failure, never a skip or fake backend.

## Release validation

Run with a real compatible backend:

```powershell
$env:GPT_SOVITS_TEST_CHECKOUT = '<path-to-gpt-sovits>'
$env:GPT_SOVITS_CHECKOUT = $env:GPT_SOVITS_TEST_CHECKOUT
python -m pip install --no-deps -e .
$env:CLI_ANYTHING_FORCE_INSTALLED = '1'
python -m pytest cli_anything/gpt_sovits/tests -q
```

Also run Stage 2B and Stage 2A focused suites, strict UTF-8 decoding, Python compilation/import,
PEP 420 and setup metadata checks, installed entry-point help/JSON/dry-run checks, source/media/
secret scans, and Git history isolation checks.

## Red-to-green record

Before the portability fix, the new home-default regression test failed because configuration
returned a machine-specific checkout instead of `~/GPT-SoVITS`. The implementation now resolves
explicit option, environment, and portable default in the documented order.

Before the publication-boundary fix, two new tests failed: the training planner rejected explicit
neutral speaker/language/count inputs, and the subproject root did not ignore private assets in
directories outside the harness. Both tests now pass with parameterized plans and root-level
ignore protection.

## Latest publication-branch results

- Portability red test: `1 failed` before the fix because the old default was machine-specific.
- Portability focused green test: `14 passed in 2.67s`.
- Real-checkout environment contract: `2 passed in 4.61s`.
- Stage 2B: `54 passed in 9.61s`.
- Publication-ignore focused contract: `1 passed in 0.58s`.
- Stage 2A: `161 passed in 16.67s`.
- Python 3.13 editable installation completed successfully.
- Force-installed complete suite: `305 passed in 124.42s`, with 0 failures and 0 skips.
- The installed entry point and real GPT-SoVITS backend produced and programmatically validated a
  WAV. Backend paths were supplied only through the documented environment variables and are
  deliberately omitted from this public record.
