"""
OpenPages LogCollector zip extraction and error-chunking utilities.

Known OpenPages log names are grouped into named categories (aurora, startup,
reporting, cognos, liberty, solr, …). Every other log-like file in the zip is
still ingested under a category derived from its filename so analysis covers
the full bundle — not only Cognos.
"""

from __future__ import annotations

import os
import re
import zipfile
from collections import defaultdict
from io import BytesIO
from pathlib import Path


# Extensions treated as text logs (and common rotated forms handled separately).
_LOG_EXTENSIONS = {
    ".log",
    ".txt",
    ".out",
    ".err",
    ".trace",
    ".message",
    ".messages",
}

# Binary / archive types that must never be decoded as logs.
_SKIP_EXTENSIONS = {
    ".zip",
    ".gz",
    ".tgz",
    ".jar",
    ".war",
    ".ear",
    ".class",
    ".dll",
    ".so",
    ".exe",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".xlsx",
    ".xls",
    ".doc",
    ".docx",
    ".pptx",
    ".key",
    ".p12",
    ".jks",
    ".cer",
    ".der",
    ".bin",
    ".dat",
}


def _decode_bytes(data: bytes) -> str:
    """Decode log bytes as UTF-8, falling back to latin-1 (never raises)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _is_safe_zip_member(member_name: str, dest_root: Path) -> bool:
    """
    Reject zip-slip paths that would escape ``dest_root``.

    Absolute paths, ``..`` segments, and resolved paths outside the extract
    directory are all treated as unsafe.
    """
    normalised = member_name.replace("\\", "/")
    if normalised.startswith("/") or normalised.startswith("../") or "/../" in normalised:
        return False
    if os.path.isabs(member_name):
        return False
    target = (dest_root / normalised).resolve()
    try:
        target.relative_to(dest_root.resolve())
    except ValueError:
        return False
    return True


def _looks_binary(sample: bytes) -> bool:
    """Heuristic: NUL bytes in the first chunk imply a non-text file."""
    if not sample:
        return False
    return b"\x00" in sample[:8192]


def _is_log_like(basename: str) -> bool:
    """Return True if ``basename`` looks like a text log file."""
    lower = basename.lower()
    # Multi-suffix: archive.log.gz — skip compressed for now.
    if lower.endswith(".gz") or lower.endswith(".zip"):
        return False

    suffix = Path(lower).suffix
    if suffix in _SKIP_EXTENSIONS:
        return False
    if suffix in _LOG_EXTENSIONS:
        return True
    # Rotated logs: app.log.1, messages.log.2024-05-01
    if re.search(r"\.log\.\d", lower) or re.search(r"\.log\.\d{4}", lower):
        return True
    # Common WebSphere / Liberty names sometimes appear without a useful suffix
    # but almost always end with .log in LogCollector; keep a few extras.
    stem = Path(lower).stem
    if stem in {"messages", "trace", "systemout", "systemerr", "console"}:
        return True
    return False


def _sanitize_category(name: str) -> str:
    """Turn a filename stem into a stable, UI-friendly category key."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", name.strip())
    cleaned = cleaned.strip("._") or "other"
    return cleaned[:80]


def _categorise(basename: str) -> str | None:
    """
    Map a file basename to a log category.

    Known OpenPages / middleware families get fixed names. Any other log-like
    file gets a category from its filename stem so nothing is silently dropped.
    Non-log files return ``None`` and are skipped for analysis.
    """
    if not _is_log_like(basename):
        return None

    lower = basename.lower()

    # Prefer known OpenPages / stack families (order matters: more specific first).
    if "aurora" in lower:
        return "aurora"
    if "startup" in lower:
        return "startup"
    if "reporting" in lower:
        return "reporting"
    if "cognos" in lower:
        return "cognos"
    if "solr" in lower:
        return "solr"
    if any(
        token in lower
        for token in (
            "systemout",
            "systemerr",
            "messages.log",
            "console.log",
            "liberty",
            "websphere",
        )
    ):
        return "liberty"
    if "objectmanager" in lower or "op-cmd" in lower:
        return "objectmanager"
    if "ffdc" in lower:
        return "ffdc"

    # Fallback: one category per distinct log filename stem.
    stem = Path(basename).stem
    # For rotated names like "app.log.1", Path.stem is "app.log" — good enough.
    return _sanitize_category(stem)


class OpenPagesZipParser:
    """Extract and categorise an OpenPages LogCollector ``.zip`` bundle."""

    EXTRACT_DIR = Path("./extracted_logs")

    # Error-severity markers that start a capturable chunk.
    _ERROR_MARKERS = ("ERROR", "FATAL", "Exception", "CRITICAL")

    def __init__(self, zip_bytes: bytes) -> None:
        if not zip_bytes:
            raise ValueError("zip_bytes must be a non-empty bytes object")
        self._zip_bytes = zip_bytes

    def extract_bundle(self) -> dict[str, str]:
        """
        Decompress the bundle into ``./extracted_logs`` and concatenate text
        by category.

        Includes **all** log-like members (not only Cognos). Known families are
        grouped; remaining logs are keyed by filename stem.

        Returns:
            Mapping of category name -> concatenated raw log text for that
            category. Categories with no matching files are omitted.

        Raises:
            zipfile.BadZipFile: If the bytes are not a valid zip archive.
            ValueError: If the archive contains no categorisable log files.
        """
        dest_root = self.EXTRACT_DIR.resolve()
        dest_root.mkdir(parents=True, exist_ok=True)

        category_parts: dict[str, list[str]] = defaultdict(list)
        skipped_non_log = 0
        skipped_binary = 0

        try:
            with zipfile.ZipFile(BytesIO(self._zip_bytes), "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    if not _is_safe_zip_member(name, dest_root):
                        continue

                    basename = Path(name.replace("\\", "/")).name
                    category = _categorise(basename)

                    target = dest_root / name.replace("\\", "/")
                    target.parent.mkdir(parents=True, exist_ok=True)

                    if category is None:
                        # Still extract for local inspection, but skip analysis.
                        with zf.open(info) as src, open(target, "wb") as dst:
                            dst.write(src.read())
                        skipped_non_log += 1
                        continue

                    raw = zf.read(info)
                    with open(target, "wb") as dst:
                        dst.write(raw)

                    if _looks_binary(raw):
                        skipped_binary += 1
                        continue

                    text = _decode_bytes(raw)
                    header = f"===== BEGIN {name.replace(chr(92), '/')} =====\n"
                    footer = f"\n===== END {basename} =====\n"
                    category_parts[category].append(header + text + footer)
        except zipfile.BadZipFile as exc:
            raise zipfile.BadZipFile(
                f"Invalid or corrupt LogCollector zip bundle: {exc}"
            ) from exc

        result = {
            cat: "\n".join(parts)
            for cat, parts in sorted(category_parts.items())
            if parts
        }
        if not result:
            raise ValueError(
                "Zip extracted successfully but no log-like files were found "
                f"(skipped_non_log={skipped_non_log}, skipped_binary={skipped_binary}). "
                "Expected .log / .txt / .out / .err / .trace (and known OpenPages names)."
            )
        return result

    @staticmethod
    def chunk_errors(raw_text: str, max_lines: int = 100) -> list[dict]:
        """
        Scan ``raw_text`` line-by-line and capture error / exception blocks.

        When a line contains ERROR, FATAL, Exception, or CRITICAL, capture up
        to ``max_lines`` subsequent lines (full stack-trace / caused-by chain).
        The scan then skips ahead past the captured block so chunks do not
        overlap.

        Returns:
            List of ``{"text": str, "start_line": int}`` dicts. ``start_line``
            is 1-based so it can be correlated with ``TimestampIndexer`` rows.
        """
        if not raw_text:
            return []

        lines = raw_text.splitlines()
        chunks: list[dict] = []
        i = 0
        n = len(lines)

        while i < n:
            line = lines[i]
            if any(marker in line for marker in OpenPagesZipParser._ERROR_MARKERS):
                start = i
                end = min(i + max_lines, n)
                block = lines[start:end]
                chunks.append(
                    {
                        "text": "\n".join(block),
                        "start_line": start + 1,  # 1-based for UI / index join
                    }
                )
                i = end  # skip past captured block — no overlapping chunks
            else:
                i += 1

        return chunks
