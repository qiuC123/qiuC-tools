"""Tests for strict public WeChat article URL acceptance."""

import pytest

from wxcli.errors import ValidationError
from wxcli.public_url import validate_public_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://mp.weixin.qq.com/s/token-123",
            "https://mp.weixin.qq.com/s/token-123",
        ),
        (
            "HTTPS://mp.weixin.qq.com/s?__biz=business&mid=123&idx=1&sn=abc",
            "https://mp.weixin.qq.com/s?__biz=business&mid=123&idx=1&sn=abc",
        ),
    ],
)
def test_accepts_only_supported_url_forms(raw: str, expected: str) -> None:
    assert validate_public_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://mp.weixin.qq.com/s/token",
        "https://example.com/s/token",
        "https://mp.weixin.qq.com/s/token?utm_source=share",
        "https://mp.weixin.qq.com:invalid/s/token",
        "https://mp.weixin.qq.com/s?__biz=business",
        "https://mp.weixin.qq.com/s?__biz=business&mid=",
        "https://mp.weixin.qq.com/s?__biz=business&__biz=again&mid=123",
        "https://mp.weixin.qq.com/mp/profile_ext?action=home",
        "https://mp.weixin.qq.com/s/token#fragment",
    ],
)
def test_rejects_every_other_url_shape(raw: str) -> None:
    with pytest.raises(ValidationError):
        validate_public_url(raw)
