"""
Timestamp parsing and line-level indexing for OpenPages / Java application logs.

Supports ISO-8601 (with ``T`` or space separators, ``Z`` or millisecond commas)
and classic syslog-style prefixes. Unparseable lines get ``timestamp=None``
rather than raising — log files are messy and must never crash the indexer.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from dateutil import parser as dateutil_parser


class TimestampIndexer:
    """Build and filter a per-line timestamp index over categorised log text."""

    # ISO-8601 near line start: optional bracket/paren wrapper used by Cognos /
    # Java logs, e.g. ``[2026-05-13 10:45:32,291] ...``
    _ISO_RE = re.compile(
        r"^"
        r"[\[\(<\"]?"
        r"(?P<ts>\d{4}-\d{2}-\d{2}"
        r"(?:[T ]\d{2}:\d{2}:\d{2}"
        r"(?:[.,]\d{1,9})?"
        r"(?:Z|[+-]\d{2}:?\d{2})?"
        r")?)"
    )

    # Syslog-style with optional leading bracket.
    _SYSLOG_RE = re.compile(
        r"^"
        r"[\[\(<\"]?"
        r"(?P<ts>"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
    )

    _ISO_BASIC = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    )

    def _parse_fast(self, raw: str) -> Optional[datetime]:
        """Parse common OpenPages / Java formats without dateutil."""
        s = raw.replace(",", ".")
        # Strip trailing Z / offset for strptime; re-apply UTC if Z.
        tz_utc = False
        if s.endswith("Z"):
            s = s[:-1]
            tz_utc = True
        # Drop fractional seconds for strptime (keep wall-clock second).
        if "." in s[19:] if len(s) > 19 else False:
            head, frac = s.split(".", 1)
            # frac may include offset like 123+0000 — strip non-digits from frac head
            digits = []
            for ch in frac:
                if ch.isdigit():
                    digits.append(ch)
                else:
                    # remaining is offset e.g. +00:00
                    rest = frac[len(digits) :]
                    if rest.startswith(("+", "-")):
                        # ignore offset for speed; treat as naive local
                        pass
                    s = head
                    break
            else:
                s = head

        for fmt in self._ISO_BASIC:
            try:
                dt = datetime.strptime(s, fmt)
                return dt.replace(tzinfo=timezone.utc) if tz_utc else dt
            except ValueError:
                continue
        return None

    def _parse_timestamp(self, line: str) -> Optional[datetime]:
        """
        Attempt to parse a timestamp from the start of ``line``.

        Returns ``None`` for lines with no recognised format — never raises.
        """
        if not line:
            return None

        match = self._ISO_RE.match(line)
        if match is None:
            match = self._SYSLOG_RE.match(line)
        if match is None:
            return None

        raw = match.group("ts")
        fast = self._parse_fast(raw)
        if fast is not None:
            return fast

        normalised = raw.replace(",", ".")
        try:
            return dateutil_parser.parse(normalised, fuzzy=False)
        except (ValueError, OverflowError, TypeError):
            try:
                return dateutil_parser.parse(normalised, fuzzy=True)
            except (ValueError, OverflowError, TypeError):
                return None

    def build_index(self, category: str, raw_text: str) -> list[dict]:
        """
        Index every line in ``raw_text``.

        Returns a list of dicts::

            {"timestamp": datetime | None, "line_number": int,
             "line": str, "category": str}

        Sorted by ``line_number`` (log order), not by timestamp, so stack
        traces remain contiguous even when intermediate lines lack stamps.
        """
        if raw_text is None:
            return []

        entries: list[dict] = []
        for idx, line in enumerate(raw_text.splitlines(), start=1):
            entries.append(
                {
                    "timestamp": self._parse_timestamp(line),
                    "line_number": idx,
                    "line": line,
                    "category": category,
                }
            )
        return entries

    def filter_by_range(
        self,
        index: list[dict],
        start: Optional[datetime],
        end: Optional[datetime],
    ) -> list[dict]:
        """
        Keep entries whose timestamp falls in ``[start, end]`` (inclusive).

        When either bound is set (a range filter is active), entries with
        ``timestamp=None`` are excluded. When both bounds are ``None``, the
        full index is returned unchanged.
        """
        if not index:
            return []

        if start is None and end is None:
            return list(index)

        filtered: list[dict] = []
        for entry in index:
            ts = entry.get("timestamp")
            if ts is None:
                continue
            # Compare naive/aware safely by stripping tz for range checks.
            ts_cmp = ts.replace(tzinfo=None) if getattr(ts, "tzinfo", None) else ts
            start_cmp = (
                start.replace(tzinfo=None) if start and start.tzinfo else start
            )
            end_cmp = end.replace(tzinfo=None) if end and end.tzinfo else end
            if start_cmp is not None and ts_cmp < start_cmp:
                continue
            if end_cmp is not None and ts_cmp > end_cmp:
                continue
            filtered.append(entry)
        return filtered

    def filter_by_keyword(self, index: list[dict], keyword: str) -> list[dict]:
        """Case-insensitive substring filter over the ``line`` field."""
        if not index:
            return []
        if not keyword:
            return list(index)

        needle = keyword.lower()
        return [e for e in index if needle in (e.get("line") or "").lower()]
