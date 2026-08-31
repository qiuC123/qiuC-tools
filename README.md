# qiuC Tools

`qiuC-tools` 是 qiuC 系列 CLI 的源码与发布仓库。每个工具拥有独立的版本、测试、
构建脚本和许可证；供 Agent 使用的 Skill 仍由
[`qiuC-skills`](https://github.com/qiuC123/qiuC-skills) 分发。

## 目录

```text
CLI/
└─ wechat-oa/  # 微信公众号内容与草稿证据 CLI
```

## WeChat OA

- 源码：[`CLI/wechat-oa`](CLI/wechat-oa)
- Windows 发布标签：`wechat-oa-v0.5.1`
- Skill：[`qiuC-skills/wechat-oa`](https://github.com/qiuC123/qiuC-skills/tree/main/wechat-oa)

多个 CLI 共用本仓库时，标签使用 `<工具名>-v<版本>`，避免版本号相互冲突。
