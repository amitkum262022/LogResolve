"""
Sensitive-data masking for OpenPages log text before any LLM / indexing work.

IMPORTANT: ``SensitiveDataMasker.mask()`` MUST run before any text is chunked,
indexed for timestamps, or sent to the LangGraph workflow. The returned
``token_map`` must NEVER be transmitted anywhere (API, logs, telemetry) — it
exists solely for local UI "reveal original" display via ``unmask()``.
"""

from __future__ import annotations

import re
from typing import Pattern


class SensitiveDataMasker:
    """
    Replace sensitive substrings with stable, numbered placeholder tokens.

    Within a single ``mask()`` call, identical original values always map to
    the same token (e.g. the same IP always becomes ``<IP_1>``).

    Large texts are processed **line-by-line** so expensive regexes never run
    over multi-megabyte strings (which previously made Load Bundle hang).
    """

    # --- Regex patterns (class constants) ---------------------------------
    TIMESTAMP_PROTECT_PATTERN: Pattern[str] = re.compile(
        r"("
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
        r"(?:[.,]\d{1,9})?"
        r"(?:Z|[+-]\d{2}:?\d{2})?"
        r"|"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
        r")"
    )

    IPV4_PATTERN: Pattern[str] = re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
    )

    # Simpler IPv6: common forms only (avoids catastrophic backtracking).
    IPV6_PATTERN: Pattern[str] = re.compile(
        r"(?<![0-9a-fA-F:])(?:"
        r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
        r"|(?:[0-9a-fA-F]{1,4}:){1,6}(?::[0-9a-fA-F]{1,4}){1,6}"
        r"|::(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4}"
        r"|(?:[0-9a-fA-F]{1,4}:){1,7}:"
        r"|::"
        r")(?![0-9a-fA-F:])"
    )

    PORT_PATTERN: Pattern[str] = re.compile(
        r"(?<=[\w.\]\)>]):(\d{2,5})\b(?!\s*:\d{2})(?!,\d)"
    )

    UNIX_PATH_PATTERN: Pattern[str] = re.compile(
        r"(?<![:\w])/(?:[\w.-]+/)+[\w.-]*"
    )

    WINDOWS_PATH_PATTERN: Pattern[str] = re.compile(
        r"(?:[A-Za-z]:\\|\\\\)(?:[^\s\"'<>|]+\\)*[^\s\"'<>|]*"
    )

    HOST_PATTERN: Pattern[str] = re.compile(
        r"(?:"
        r"(://)((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,})"
        r"|"
        r"(?i:\b((?:host|hostname|server|servername|remotehost|remote_host)[=:]\s*)"
        r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}))"
        r")"
    )

    _TS_RESTORE_RE = re.compile(r"__TS_PROTECT_(\d+)__")

    def mask(self, text: str) -> tuple[str, dict[str, str]]:
        """
        Replace sensitive values with stable placeholders.

        Token families: ``<IP_N>``, ``<PORT_N>``, ``<PATH_N>``, ``<HOST_N>``.
        Numbering is per unique original value within this call.

        Returns:
            ``(masked_text, token_map)`` where ``token_map`` maps token ->
            original value. Keep ``token_map`` local only — never send it to
            an LLM or remote service.
        """
        if not text:
            return text, {}

        value_to_token: dict[str, str] = {}
        token_map: dict[str, str] = {}
        counters: dict[str, int] = {"IP": 0, "PORT": 0, "PATH": 0, "HOST": 0}

        def _allocate(kind: str, value: str) -> str:
            existing = value_to_token.get(value)
            if existing is not None:
                return existing
            counters[kind] += 1
            token = f"<{kind}_{counters[kind]}>"
            value_to_token[value] = token
            token_map[token] = value
            return token

        def _mask_line(line: str) -> str:
            if not line:
                return line

            protected: list[str] = []

            def _stash_ts(m: re.Match[str]) -> str:
                protected.append(m.group(0))
                return f"__TS_PROTECT_{len(protected) - 1}__"

            result = self.TIMESTAMP_PROTECT_PATTERN.sub(_stash_ts, line)

            # Skip expensive patterns on lines that cannot contain them.
            if ":" in result:
                # IPv6 only when hex+colon-like content is present.
                if any(c in result for c in "abcdefABCDEF") or "::" in result:
                    result = self.IPV6_PATTERN.sub(
                        lambda m: _allocate("IP", m.group(0)), result
                    )
                result = self.PORT_PATTERN.sub(
                    lambda m: f":{_allocate('PORT', m.group(1))}", result
                )

            if "." in result:
                result = self.IPV4_PATTERN.sub(
                    lambda m: _allocate("IP", m.group(0)), result
                )

            if "\\" in result or ":" in result[:3]:
                result = self.WINDOWS_PATH_PATTERN.sub(
                    lambda m: _allocate("PATH", m.group(0)), result
                )

            if "/" in result:
                result = self.UNIX_PATH_PATTERN.sub(
                    lambda m: _allocate("PATH", m.group(0)), result
                )

            if "://" in result or "host" in result.lower() or "server" in result.lower():
                def _host_repl(m: re.Match[str]) -> str:
                    if m.group(1) is not None and m.group(2) is not None:
                        return m.group(1) + _allocate("HOST", m.group(2))
                    prefix = m.group(3) or ""
                    host = m.group(4) or ""
                    if not host:
                        return m.group(0)
                    return prefix + _allocate("HOST", host)

                result = self.HOST_PATTERN.sub(_host_repl, result)

            if protected:
                def _restore(m: re.Match[str]) -> str:
                    idx = int(m.group(1))
                    return protected[idx] if 0 <= idx < len(protected) else m.group(0)

                result = self._TS_RESTORE_RE.sub(_restore, result)

            return result

        # Preserve original newlines (including trailing) by splitting carefully.
        parts = text.splitlines(keepends=True)
        out: list[str] = [_mask_line(p) for p in parts]
        return "".join(out), token_map

    def unmask(self, masked_text: str, token_map: dict[str, str]) -> str:
        """
        Reverse placeholder substitution for local UI display only.

        Never call this before sending text to an LLM — only for local reveal.
        Longer tokens are replaced first so ``<IP_10>`` is not partially
        corrupted by ``<IP_1>``.
        """
        if not masked_text or not token_map:
            return masked_text

        result = masked_text
        for token in sorted(token_map.keys(), key=len, reverse=True):
            result = result.replace(token, token_map[token])
        return result
