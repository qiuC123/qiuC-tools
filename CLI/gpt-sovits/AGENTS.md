# GPT-SoVITS CLI contribution rules

- Keep this directory source-only. Never commit model weights, audio, video, datasets, logs,
  runtime state, caches, secrets, or local environment files.
- Do not add machine-specific absolute paths. Backend locations must come from command-line
  options, documented environment variables, or portable defaults.
- Add or update related automated tests for every behavior change. Run the relevant tests before
  committing and record only passing, reproducible results in the mirrored `TEST.md` files.
- Keep the canonical and packaged `SKILL.md` files aligned, and keep the two `TEST.md` files
  byte-identical.
- Every logical change must have its own Git commit. Do not publish or redistribute models and
  data merely because this source directory is publicly visible.
