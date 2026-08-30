from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
CHANNEL_SOURCE = ROOT / "integrations" / "agent-reach" / "wechat.py"
SKILL_ROOT = ROOT / "skills" / "wechat-oa"
COMPATIBILITY_SKILL_ROOT = ROOT / "skills" / "wxcli"


@dataclass
class FakeProbeResult:
    status: str
    output: str = ""
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def load_channel(monkeypatch, results: FakeProbeResult | dict[str, FakeProbeResult]):
    agent_reach = ModuleType("agent_reach")
    agent_reach.__path__ = []  # type: ignore[attr-defined]
    channels = ModuleType("agent_reach.channels")
    channels.__path__ = []  # type: ignore[attr-defined]
    base = ModuleType("agent_reach.channels.base")
    base.Channel = object  # type: ignore[attr-defined]
    probe = ModuleType("agent_reach.probe")
    probe.probe_command = (  # type: ignore[attr-defined]
        lambda command, *args, **kwargs: results[command]
        if isinstance(results, dict)
        else results
    )

    monkeypatch.setitem(sys.modules, "agent_reach", agent_reach)
    monkeypatch.setitem(sys.modules, "agent_reach.channels", channels)
    monkeypatch.setitem(sys.modules, "agent_reach.channels.base", base)
    monkeypatch.setitem(sys.modules, "agent_reach.probe", probe)

    module_name = "agent_reach.channels.wechat_test"
    spec = importlib.util.spec_from_file_location(module_name, CHANNEL_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module.WeChatChannel


def test_agent_reach_channel_matches_only_supported_public_urls(monkeypatch) -> None:
    channel = load_channel(monkeypatch, FakeProbeResult("ok", "0.1.0"))()

    assert channel.can_handle("https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ")
    assert channel.can_handle("https://mp.weixin.qq.com/s/token.with.dot")
    assert channel.can_handle("https://mp.weixin.qq.com/s?__biz=abc&mid=123")
    assert channel.can_handle("https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1")
    assert not channel.can_handle("http://mp.weixin.qq.com/s/token")
    assert not channel.can_handle("https://mp.weixin.qq.com/s/token?extra=1")
    assert not channel.can_handle("https://mp.weixin.qq.com/s?__biz=abc&__biz=def&mid=123")
    assert not channel.can_handle("https://user@mp.weixin.qq.com/s/token")
    assert not channel.can_handle("https://mp.weixin.qq.com:443/s/token")
    assert not channel.can_handle("https://example.com/s/token")


def test_agent_reach_channel_prefers_wechat_oa(monkeypatch) -> None:
    channel_type = load_channel(monkeypatch, FakeProbeResult("ok", "0.1.0"))
    channel = channel_type()

    status, message = channel.check()

    assert status == "ok"
    assert "0.1.0" in message
    assert "WeChat OA" in message
    assert channel.active_backend == "wechat-oa"


def test_agent_reach_channel_falls_back_to_wxcli(monkeypatch) -> None:
    channel_type = load_channel(
        monkeypatch,
        {
            "wechat-oa": FakeProbeResult("missing"),
            "wxcli": FakeProbeResult("ok", "0.5.0"),
        },
    )
    channel = channel_type()

    status, message = channel.check()

    assert status == "ok"
    assert "兼容命令" in message
    assert channel.active_backend == "wxcli"


def test_agent_reach_channel_does_not_claim_missing_wechat_oa(monkeypatch) -> None:
    channel_type = load_channel(
        monkeypatch,
        {
            "wechat-oa": FakeProbeResult("missing"),
            "wxcli": FakeProbeResult("missing"),
        },
    )
    channel = channel_type()

    status, message = channel.check()

    assert status == "warn"
    assert "PATH" in message
    assert channel.active_backend is None


def test_wechat_oa_skill_contains_required_contracts() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    references = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((SKILL_ROOT / "references").glob("*.md"))
    )

    assert "TODO" not in skill
    assert "wechat-oa --json article get" in skill
    assert "wechat-oa --json discovery search" in skill
    assert "wechat-oa --json discovery hydrate" in skill
    assert "wxcli --version" in skill
    assert "VERIFICATION_REQUIRED" in skill
    assert "explicitly authorizes browser mode" in skill
    assert "--browser-fallback" in skill
    assert "--no-browser" in skill
    assert "browser policy set auto-fallback" in skill
    assert "images[]" in skill
    assert "import-word --confirm" in skill
    assert "未发布草稿" in references
    assert "AppSecret" in references
    assert "绕过验证" in references
    assert "article_identity" in references
    assert "不是微信官方或全量索引" in references
    assert "last_successful_read_at" in references
    assert "BROWSER_BUSY" in references


def test_wxcli_skill_is_a_narrow_compatibility_alias() -> None:
    skill = (COMPATIBILITY_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "name: wxcli" in skill
    assert "Compatibility alias" in skill
    assert "wechat-oa" in skill
    assert not (COMPATIBILITY_SKILL_ROOT / "references").exists()
