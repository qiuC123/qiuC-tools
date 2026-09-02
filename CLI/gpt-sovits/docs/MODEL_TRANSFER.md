# Local model transfer

Model files are deliberately excluded from this repository. Move them between trusted machines
through a separate local or encrypted transfer process.

## Compatible bundle

A typical bundle contains:

- GPT checkpoint: `speaker-gpt.ckpt`
- SoVITS checkpoint: `speaker-sovits.pth`
- Optional reference audio: `reference.wav`
- Optional UTF-8 prompt manifest: `reference-prompt.json`

The destination also needs a compatible GPT-SoVITS revision, matching pretrained dependencies,
language resources, and a working runtime. A file extension alone does not prove compatibility.

## Verify before use

1. Record the GPT-SoVITS revision and runtime versions on the source machine.
2. Calculate SHA-256 for every transferred file.
3. Transfer through a trusted channel without renaming formats.
4. Recalculate SHA-256 on the destination and compare exact values.
5. Inspect the reference WAV and run CLI model selection in dry-run mode before real inference.
6. Keep the files outside the repository and point the CLI to their local paths.

Example placeholders:

```powershell
Get-FileHash '<model-directory>/speaker-gpt.ckpt' -Algorithm SHA256
Get-FileHash '<model-directory>/speaker-sovits.pth' -Algorithm SHA256
cli-anything-gpt-sovits model use-gpt '<model-directory>/speaker-gpt.ckpt' --dry-run --json
cli-anything-gpt-sovits model use-sovits '<model-directory>/speaker-sovits.pth' --dry-run --json
```

Do not commit checkpoints, reference audio, prompt manifests containing private material, or
generated samples to GitHub. The upstream GPT-SoVITS license and each model, voice, dataset, and
media license remain independently applicable.
