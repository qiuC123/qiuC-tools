# -*- coding: utf-8 -*-
"""WeChat Official Account articles — wxcli channel for Agent Reach."""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import parse_qsl, urlsplit

from agent_reach.probe import probe_command

from .base import Channel


class WeChatChannel(Channel):
    """Detect supported public article URLs and verify the wxcli executable."""

    name = "wechat"
    description = "微信公众号文章"
    backends = ["wxcli"]
    tier = 0

    def can_handle(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return False
        if parts.scheme.casefold() != "https":
            return False
        if (parts.hostname or "").casefold() != "mp.weixin.qq.com":
            return False
        if parts.username is not None or parts.password is not None or port is not None:
            return False

        path_parts = parts.path.split("/")
        if len(path_parts) == 3 and path_parts[:2] == ["", "s"] and path_parts[2]:
            return not parts.query and not parts.fragment
        if parts.path != "/s" or parts.fragment:
            return False

        query: defaultdict[str, list[str]] = defaultdict(list)
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query[key].append(value)
        return (
            len(query["__biz"]) == 1
            and bool(query["__biz"][0])
            and len(query["mid"]) == 1
            and bool(query["mid"][0])
        )

    def check(self, config=None):
        self.active_backend = None
        probe = probe_command(
            "wxcli",
            ["--version"],
            timeout=10,
            package="wxcli",
        )
        if probe.status == "missing":
            return "warn", "wxcli 未安装或不在 PATH；请安装 Windows x64 发布包并将其目录加入 PATH"
        if probe.status == "broken":
            return "error", "wxcli 命令存在但无法执行；请重新安装 Windows x64 发布包"
        if not probe.ok:
            detail = probe.hint or probe.status
            return "error", f"wxcli 版本检查失败：{detail}"

        self.active_backend = self.backends[0]
        version = probe.output.strip() or "未知版本"
        return "ok", f"wxcli 可用（{version}，Windows 只读微信公众号后端）"
