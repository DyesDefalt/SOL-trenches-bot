"""
Privacy filter — sanitize context dict sebelum dikirim ke LLM.

Remove atau mask sensitive data: private keys, wallet paths, API keys, secrets.
Strip berdasarkan blocked key patterns DAN free-form text patterns.
"""

from __future__ import annotations

import re
from typing import Any


class PrivacyFilter:
    """
    Sanitize context dicts dan text sebelum dikirim ke LLM.

    Key-based blocking: dict keys yang match BLOCKED_PATTERNS dihapus.
    Text sanitization: pattern seperti 'private_key=...' di-strip dari string.
    Recursive untuk nested dicts.
    """

    # Keys yang match pattern ini (case-insensitive substring) akan dihapus dari context
    BLOCKED_PATTERNS: list[str] = [
        "private_key",
        "api_key",
        "secret",
        "password",
        "WALLET_PATH",
    ]

    # Regex untuk strip pattern dari free-form text (key=value atau key: value)
    _TEXT_PATTERN_RE = re.compile(
        r"(?:private_key|api_key|secret|password|wallet_path)"
        r"[\s]*[=:\s]+[\S]+",
        re.IGNORECASE,
    )

    @staticmethod
    def _key_is_blocked(key: str) -> bool:
        """Check apakah dict key mengandung salah satu blocked pattern."""
        key_lower = key.lower()
        return any(p.lower() in key_lower for p in PrivacyFilter.BLOCKED_PATTERNS)

    @staticmethod
    def sanitize_context(context: dict[str, Any]) -> dict[str, Any]:
        """
        Remove keys matching blocked patterns. Recursive untuk nested dicts.

        Returns new dict — original tidak dimodifikasi.
        """
        result: dict[str, Any] = {}
        for k, v in context.items():
            if PrivacyFilter._key_is_blocked(k):
                # Skip key yang sensitif
                continue
            if isinstance(v, dict):
                result[k] = PrivacyFilter.sanitize_context(v)
            elif isinstance(v, list):
                result[k] = [
                    PrivacyFilter.sanitize_context(item)
                    if isinstance(item, dict)
                    else item
                    for item in v
                ]
            else:
                result[k] = v
        return result

    @staticmethod
    def sanitize_text(text: str) -> str:
        """Strip patterns like 'private_key=...' dari free-form text."""
        return PrivacyFilter._TEXT_PATTERN_RE.sub("[REDACTED]", text)


# Module-level singleton
privacy_filter = PrivacyFilter()
