"""
Timestamp parsing and line-level indexing for OpenPages / Java application logs.

Supports ISO-8601 (with ``T`` or space separators, ``Z`` or millisecond commas)
and classic syslog-style prefixes. Unparseable lines get ``timestamp=None``
rather than raising — log files are messy and must never crash the indexer.
"""

from __future__ import annotations

import re
from datetime import datetime
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
        # Java logs often use comma as the fractional-second separator;
        # dateutil expects a dot.
        normalised = raw.replace(",", ".")
        try:
            return dateutil_parser.parse(normalised, fuzzy=False)
        except (ValueError, OverflowError, TypeError):
            # Fuzzy=False still fails on some edge cases; try fuzzy as last resort.
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
        # splitlines() drops the trailing newline; empty files yield [].
        for idx, line in enumerate(raw_text.splitlines(), start=1):
            entries.append(
                {
                    "timestamp": self._parse_timestamp(line),
                    "line_number": idx,
                    "line": line,
                    "category": category,
                }
            )
        # Already in line order; explicit sort documents the contract.
        entries.sort(key=lambda e: e["line_number"])
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
                continue  # unparseable lines drop out when filtering by time
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
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
