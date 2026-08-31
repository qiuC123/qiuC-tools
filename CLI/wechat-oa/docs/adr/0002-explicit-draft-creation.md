# Permit explicit creation of new drafts without broader write access

Status: superseded in part by ADR-0003 for safe replacement of an existing unpublished draft. The creation decision below remains active.

wxcli may convert a local Word document and cover into a previewable package, then—only with an explicit `--confirm` flag—upload the required images and create one new unpublished Official Account draft. This capability is implemented as a separate draft writer rather than a Provider because Providers remain read-only; publishing, deleting, commenting, and mass sending remain outside the product boundary. ADR-0003 later permits one narrowly controlled existing-draft replacement path. The trade-off is that image uploads cannot be rolled back if a later API call fails, so the CLI validates and previews everything locally before any network write and reports partial-upload counts on failure.
