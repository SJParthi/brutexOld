"""Secret-scrubbing primitives — the COMMON home for redacting secrets from text.

DEC-AWS-SSM-SEAM-COMMON-001 (MIGRATION.md #1754) + DEC-DHAN-AUTH-001. :func:`scrub_secrets`
is extracted verbatim from :func:`brutex.adapters.groww.auth._sanitize_reason` (Groww
byte-identical — that helper now delegates here) so a SECOND vendor (Dhan) shares one
scrubber. :func:`redact_url_query` is the NEW Dhan-specific piece: the headless PIN+TOTP
mint carries the secret PIN + TOTP code in the URL QUERY STRING, and a urllib
``HTTPError.__str__`` embeds that full URL — so every Dhan transport exception must have its
message scrubbed AND its URL query redacted before it can reach a log, an audit entry, or a
re-raised exception message.

Stdlib-only (imports ``collections.abc`` + ``urllib.parse``, nothing from any ``brutex``
package), so it can never join an import cycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit


def scrub_secrets(text: str, secrets: Iterable[str]) -> str:
    """Scrub every *secrets* occurrence in *text* to ``***`` (secret hygiene).

    Used to make a vendor's failure reason VISIBLE without leaking a credential (an api key,
    a TOTP shared secret, a minted TOTP code, a login PIN). A blank/empty secret is skipped
    (``str.replace("", …)`` would otherwise corrupt the whole string by inserting between
    every character). The longest secrets are scrubbed first so a secret that contains
    another as a substring is fully masked. O(len(text) × distinct secrets).
    """
    sanitized = text
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        sanitized = sanitized.replace(secret, "***")
    return sanitized


def redact_url_query(url: str) -> str:
    """Redact the QUERY STRING of *url* to ``[REDACTED]``, preserving scheme/host/path.

    The Dhan headless mint (``https://auth.dhan.co/app/generateAccessToken?dhanClientId=…&
    pin=…&totp=…``) carries the secret PIN + TOTP code in the query string, which a urllib
    ``HTTPError.__str__`` embeds verbatim. This keeps the diagnostic host+path (so the
    operator sees WHICH endpoint failed) while never leaking the secrets. A *url* with no
    query is returned scheme/host/path-unchanged (an empty query stays empty — no spurious
    ``?[REDACTED]``). A value that does not parse as a URL (no scheme/netloc) is returned as
    a whole-string ``[REDACTED]`` fail-safe rather than risk leaking an unparsed secret. O(1).
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[REDACTED]"
    if not parts.scheme and not parts.netloc:
        # Not a recognisable URL — fail safe (never risk leaking an unparsed secret).
        return "[REDACTED]"
    redacted_query = "[REDACTED]" if parts.query else ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, redacted_query, parts.fragment))


__all__ = ["redact_url_query", "scrub_secrets"]
