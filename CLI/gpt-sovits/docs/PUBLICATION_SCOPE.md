# GPT-SoVITS CLI publication scope

This directory is prepared as a public, source-only snapshot of the GPT-SoVITS CLI harness.

## Included

- CLI source code and packaging metadata
- Automated tests that use synthetic fixtures or an explicitly configured local backend
- Public installation, command, safety, and model-transfer documentation

## Excluded

- Model weights and pretrained checkpoints
- Reference audio, films, datasets, generated media, and transcripts
- Runtime logs, state directories, caches, local environment files, and acceptance evidence
- Internal handoffs, machine-specific development state, private paths, and local training records

The publication branch starts from `origin/main`. It imports only the reviewed GPT-SoVITS CLI
snapshot and does not merge the private local `main` history, so unrelated internal commits do
not enter the public branch.

Compatible models must be transferred separately as local files. They must not be committed to
GitHub. Public visibility of this source does not grant permission to redistribute models,
datasets, reference media, or upstream project assets.

No independent open-source license has been declared for this subproject. Users must also review
and comply with the upstream GPT-SoVITS license and every model or dataset license that applies.
