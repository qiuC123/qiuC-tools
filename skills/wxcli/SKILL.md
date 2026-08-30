---
name: wxcli
description: >
  Compatibility alias for the former wxcli name. Use only when a user,
  integration, command, or document explicitly says wxcli; prefer the canonical
  WeChat OA skill and wechat-oa command for new WeChat Official Account work.
  Preserve all existing safety approvals and never publish, mass-send, delete,
  bypass verification, or expose credentials.
---

# wxcli compatibility

`wxcli` is the supported legacy name for **WeChat OA**. For new work, invoke the
`wechat-oa` Skill and command. If `wechat-oa --version` is unavailable, the
installed `wxcli` command may be used with the same arguments and JSON contract.

Do not translate or migrate internal Python package names, local state paths,
credential identifiers, Evidence schema fields, or caller-owned provenance
values merely because they still contain `wxcli`.

Browser use, live API checks, draft creation, and draft updates retain their
independent explicit-authorization requirements. Never use this compatibility
entry to weaken the canonical WeChat OA safety boundaries.
