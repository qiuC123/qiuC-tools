# qiuC Tools

[![WeChat OA CI](https://github.com/qiuC123/qiuC-tools/actions/workflows/wechat-oa-ci.yml/badge.svg)](https://github.com/qiuC123/qiuC-tools/actions/workflows/wechat-oa-ci.yml)

`qiuC-tools` 是 qiuC 系列 CLI 的源码与发布仓库。每个工具拥有独立的版本、测试和
构建脚本，并在各自目录声明许可边界；供 Agent 使用的 Skill 仍由
[`qiuC-skills`](https://github.com/qiuC123/qiuC-skills) 分发。

## 与 qiuC-skills 的关系

`qiuC-tools` 负责实际 CLI 的源码、测试和 Release；`qiuC-skills` 负责 Agent 调用说明、
安全边界和安装入口。两个仓库互相引用，但不是 Git submodule，也不会自动同步。

```text
qiuC-skills（Agent 能力说明）──下载固定版本──> qiuC-tools（CLI 源码与 Release）
```

CLI 参数或输出契约变化时，应先在本仓库完成实现和发布，再同步更新对应 Skill。
安装 Skill 不代表实际 CLI 已安装。

## 目录

```text
CLI/
└─ wechat-oa/  # 微信公众号内容与草稿证据 CLI
└─ gpt-sovits/  # GPT-SoVITS 本地推理与训练工作流 CLI
```

## WeChat OA

- 源码：[`CLI/wechat-oa`](CLI/wechat-oa)
- Windows 发布标签：`wechat-oa-v0.5.1`
- Skill：[`qiuC-skills/wechat-oa`](https://github.com/qiuC123/qiuC-skills/tree/main/wechat-oa)

多个 CLI 共用本仓库时，标签使用 `<工具名>-v<版本>`，避免版本号相互冲突。
