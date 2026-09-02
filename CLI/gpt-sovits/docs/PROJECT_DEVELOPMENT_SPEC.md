# Public development specification

## Goal

Maintain a portable CLI harness for a separately installed GPT-SoVITS checkout while preserving
real-backend behavior, machine-readable output, local-only service boundaries, and reproducible
tests.

## Change workflow

1. Define the intended behavior and security boundary.
2. Add a failing automated test for behavior changes.
3. Make the smallest implementation change.
4. Run focused tests, then the complete force-installed suite against a real backend.
5. Update public documentation and both mirrored `TEST.md` files.
6. Run path, secret, media, packaging, and Git history scans before publication.
7. Commit each logical change separately; publishing requires a separate approval.

## Portability requirements

- Never hardcode a contributor's checkout or home directory.
- Resolve the backend from explicit options, documented environment variables, and portable
  defaults.
- Keep output, state, logs, datasets, media, and model weights outside Git.
- Treat the upstream application as a hard dependency; do not fake synthesis in real E2E tests.

## Compatibility boundary

The harness cannot guarantee that every upstream revision or third-party checkpoint is
compatible. Users must align the GPT-SoVITS version, runtime, pretrained dependencies, model
format, and language resources, then verify local hashes before use.

## License boundary

No independent open-source license has been declared for this subproject. Public source
visibility does not create permission to redistribute source, models, datasets, or media. Users
must comply with upstream and asset-specific licenses.
