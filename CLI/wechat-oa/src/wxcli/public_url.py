"""Strict validation for the two supported public WeChat article URL forms."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from wxcli.errors import ValidationError

_HOST = "mp.weixin.qq.com"
_REQUIRED_QUERY_KEYS = frozenset({"__biz", "mid"})


def validate_public_url(value: str) -> str:
    """Validate and normalize an explicitly supported public WeChat URL."""
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValidationError("The URL is malformed.") from error

    try:
        port = parsed.port
    except ValueError as error:
        raise ValidationError("The URL is malformed.") from error
    if parsed.scheme.lower() != "https" or parsed.hostname != _HOST:
        raise ValidationError("Use an HTTPS mp.weixin.qq.com article URL.")
    if parsed.username is not None or parsed.password is not None or port is not None:
        raise ValidationError("The public URL must not include user info or a port.")
    if parsed.fragment:
        raise ValidationError("The public URL must not include a fragment.")

    path_parts = parsed.path.split("/")
    is_token_path = (
        len(path_parts) == 3
        and path_parts[0] == ""
        and path_parts[1] == "s"
        and bool(path_parts[2])
    )
    if is_token_path:
        if parsed.query:
            raise ValidationError("The /s/<token> URL form must not include a query string.")
        return urlunsplit(("https", _HOST, parsed.path, "", ""))

    if parsed.path != "/s":
        raise ValidationError("Only the supported /s article URL forms are accepted.")

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query_values: dict[str, list[str]] = {}
    for key, item in query_pairs:
        query_values.setdefault(key, []).append(item)
    if any(
        len(query_values.get(key, [])) != 1 or not query_values[key][0]
        for key in _REQUIRED_QUERY_KEYS
    ):
        raise ValidationError("The /s query URL form requires non-empty __biz and mid values.")

    return urlunsplit(("https", _HOST, "/s", urlencode(query_pairs), ""))
