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
    """

    # --- Regex patterns (class constants) ---------------------------------
    # Timestamps are stashed BEFORE port/host masking so values like
    # ``10:45:32`` are not mistaken for ``host:port``.
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

    # IPv6: covers compressed forms (::) and full 8-hextet addresses.
    # Alternatives that retain trailing hextets MUST come before forms that
    # end at '::', otherwise "fe80::1" would match only "fe80::".
    IPV6_PATTERN: Pattern[str] = re.compile(
        r"(?<![0-9a-fA-F:])(?:"
        r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
        r"|(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}"
        r"|(?:[0-9a-fA-F]{1,4}:){1,5}(?::[0-9a-fA-F]{1,4}){1,2}"
        r"|(?:[0-9a-fA-F]{1,4}:){1,4}(?::[0-9a-fA-F]{1,4}){1,3}"
        r"|(?:[0-9a-fA-F]{1,4}:){1,3}(?::[0-9a-fA-F]{1,4}){1,4}"
        r"|(?:[0-9a-fA-F]{1,4}:){1,2}(?::[0-9a-fA-F]{1,4}){1,5}"
        r"|[0-9a-fA-F]{1,4}:(?::[0-9a-fA-F]{1,4}){1,6}"
        r"|:(?::[0-9a-fA-F]{1,4}){1,7}"
        r"|::(?:[0-9a-fA-F]{1,4}:){0,5}[0-9a-fA-F]{1,4}"
        r"|(?:[0-9a-fA-F]{1,4}:){1,7}:"
        r"|::"
        r")(?![0-9a-fA-F:])"
    )

    # host:port — skip clock-like ``HH:MM`` / ``HH:MM:SS`` leftovers.
    # Lookahead rejects another ``:digits`` (time) or a fractional ``,digits``.
    PORT_PATTERN: Pattern[str] = re.compile(
        r"(?<=[\w.\]\)>]):(\d{2,5})\b(?!\s*:\d{2})(?!,\d)"
    )

    UNIX_PATH_PATTERN: Pattern[str] = re.compile(
        r"(?<![:\w])/(?:[\w.-]+/)+[\w.-]*"
    )

    WINDOWS_PATH_PATTERN: Pattern[str] = re.compile(
        r"(?:[A-Za-z]:\\|\\\\)(?:[^\s\"'<>|]+\\)*[^\s\"'<>|]*"
    )

    # Hostnames: URL authority (://host) and explicit host-like keys.
    # Capture groups keep the prefix; only the hostname is replaced.
    # Avoid matching Java packages after bare whitespace (e.g. ``at com.sun.org...``).
    HOST_PATTERN: Pattern[str] = re.compile(
        r"(?:"
        r"(://)((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,})"
        r"|"
        r"(?i:\b((?:host|hostname|server|servername|remotehost|remote_host)[=:]\s*)"
        r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}))"
        r")"
    )

    def mask(self, text: str) -> tuple[str, dict[str, str]]:
        """
        Replace sensitive values with stable placeholders.

        Token families: ``<IP_N>``, ``<PORT_N>``, ``<PATH_N>``, ``<HOST_N>``.
        Numbering is per unique original value within this call.

        Timestamps are temporarily protected so clock fields are never treated
        as ports, then restored unchanged (they are not entered in token_map).

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
            if value in value_to_token:
                return value_to_token[value]
            counters[kind] += 1
            token = f"<{kind}_{counters[kind]}>"
            value_to_token[value] = token
            token_map[token] = value
            return token

        # 0) Stash timestamps so ``10:45:32`` cannot become ``:<PORT_n>``.
        protected_timestamps: list[str] = []

        def _stash_ts(m: re.Match[str]) -> str:
            protected_timestamps.append(m.group(0))
            return f"__TS_PROTECT_{len(protected_timestamps) - 1}__"

        result = self.TIMESTAMP_PROTECT_PATTERN.sub(_stash_ts, text)

        def _sub_full(pattern: Pattern[str], kind: str, s: str) -> str:
            def repl(m: re.Match[str]) -> str:
                return _allocate(kind, m.group(0))

            return pattern.sub(repl, s)

        def _sub_port(s: str) -> str:
            def repl(m: re.Match[str]) -> str:
                port = m.group(1)
                token = _allocate("PORT", port)
                return f":{token}"

            return SensitiveDataMasker.PORT_PATTERN.sub(repl, s)

        result = _sub_full(self.IPV6_PATTERN, "IP", result)
        result = _sub_port(result)
        result = _sub_full(self.IPV4_PATTERN, "IP", result)
        result = _sub_full(self.WINDOWS_PATH_PATTERN, "PATH", result)
        result = _sub_full(self.UNIX_PATH_PATTERN, "PATH", result)

        def _sub_host(s: str) -> str:
            def repl(m: re.Match[str]) -> str:
                # Group layout: (://)(host)  OR  (key=)(host) via nested groups.
                if m.group(1) is not None and m.group(2) is not None:
                    return m.group(1) + _allocate("HOST", m.group(2))
                # Second alternative: groups 3=prefix, 4=host
                prefix = m.group(3) or ""
                host = m.group(4) or ""
                if not host:
                    return m.group(0)
                return prefix + _allocate("HOST", host)

            return SensitiveDataMasker.HOST_PATTERN.sub(repl, s)

        result = _sub_host(result)

        # Restore timestamps literally (not as mask tokens).
        for idx, ts in enumerate(protected_timestamps):
            result = result.replace(f"__TS_PROTECT_{idx}__", ts)

        return result, token_map

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
