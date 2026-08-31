# Adopt WeChat OA while retaining wxcli compatibility

The product is named **WeChat OA**, with `wechat-oa` as its canonical CLI and Skill identifier, because its boundary is WeChat Official Account content rather than general WeChat automation. The `wxcli` executable, Python package, local state paths, credential identifiers, and existing evidence provenance remain compatible so Agent Reach, Recruitment Radar, installed sessions, and rollback releases continue to work; new integrations prefer `wechat-oa` and fall back to `wxcli`.
