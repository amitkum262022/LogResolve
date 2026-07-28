"""
Pluggable log-bundle ingestion sources for the OpenPages Log Analysis Assistant.

All concrete sources implement ``LogBundleSource.fetch_bundle()`` so the app
(and future automation) can swap between manual upload and remote pull
without changing downstream parser / workflow code.
"""

from __future__ import annotations

import zipfile
from abc import ABC, abstractmethod
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable


class LogBundleSource(ABC):
    """Abstract source that yields a raw LogCollector zip bundle as bytes."""

    @abstractmethod
    def fetch_bundle(self) -> bytes:
        """Return the raw bytes of an OpenPages LogCollector ``.zip`` bundle."""


class ManualUploadSource(LogBundleSource):
    """Wraps any upload object that provides ``getvalue()`` returning bytes."""

    def __init__(self, uploaded_file: Any) -> None:
        if uploaded_file is None:
            raise ValueError("uploaded_file must not be None")
        if not hasattr(uploaded_file, "getvalue"):
            raise TypeError(
                "uploaded_file must provide getvalue() returning bytes"
            )
        self._uploaded_file = uploaded_file

    def fetch_bundle(self) -> bytes:
        """Read the entire uploaded file into memory and return its bytes."""
        data = self._uploaded_file.getvalue()
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("uploaded_file.getvalue() must return bytes")
        return bytes(data)


class LooseFilesUploadSource(LogBundleSource):
    """
    Pack one or more uploaded files into a single in-memory zip for the parser.

    Accepts:
      - ``.zip`` LogCollector bundles (members are merged in)
      - loose log files (``.log``, ``.txt``, ``.out``, ``.err``, ``.trace``, …)
    """

    def __init__(self, uploaded_files: Iterable[Any]) -> None:
        files = [f for f in (uploaded_files or []) if f is not None]
        if not files:
            raise ValueError("uploaded_files must contain at least one file")
        self._uploaded_files = files

    def fetch_bundle(self) -> bytes:
        """Return a zip that contains all uploaded logs / nested zip members."""
        # Fast path: a single .zip is already a valid LogCollector bundle —
        # do not decompress and re-pack (that doubles memory and time).
        if len(self._uploaded_files) == 1:
            uploaded = self._uploaded_files[0]
            name = (getattr(uploaded, "name", None) or "").lower()
            data = uploaded.getvalue()
            if not isinstance(data, (bytes, bytearray)):
                raise TypeError(f"{name or 'upload'}: getvalue() must return bytes")
            raw = bytes(data)
            if name.endswith(".zip"):
                # Validate by opening the central directory only — do not
                # CRC-scan every member (testzip) on large LogCollector zips.
                try:
                    with zipfile.ZipFile(BytesIO(raw), "r") as zf:
                        if not zf.namelist():
                            raise zipfile.BadZipFile("empty zip archive")
                except zipfile.BadZipFile as exc:
                    raise zipfile.BadZipFile(
                        f"Uploaded file {name!r} is not a valid zip: {exc}"
                    ) from exc
                return raw

        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as out_zf:
            for uploaded in self._uploaded_files:
                name = getattr(uploaded, "name", None) or "upload.log"
                data = uploaded.getvalue()
                if not isinstance(data, (bytes, bytearray)):
                    raise TypeError(f"{name}: getvalue() must return bytes")
                raw = bytes(data)
                lower = name.lower()

                if lower.endswith(".zip"):
                    try:
                        with zipfile.ZipFile(BytesIO(raw), "r") as inner:
                            for info in inner.infolist():
                                if info.is_dir():
                                    continue
                                stem = Path(name).stem
                                member = f"{stem}/{info.filename}"
                                out_zf.writestr(member, inner.read(info))
                    except zipfile.BadZipFile as exc:
                        raise zipfile.BadZipFile(
                            f"Uploaded file {name!r} is not a valid zip: {exc}"
                        ) from exc
                else:
                    out_zf.writestr(f"uploads/{Path(name).name}", raw)

        return buf.getvalue()


class OpenPagesAPISource(LogBundleSource):
    """
    Extension point for automated LogCollector retrieval from OpenPages.

    Constructor accepts connection settings that a future implementation would
    use to authenticate and download a fresh diagnostic zip.
    """

    def __init__(self, base_url: str, api_token: str) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_token = api_token or ""

    def fetch_bundle(self) -> bytes:
        """
        Pull a LogCollector zip from OpenPages (not yet implemented).

        Intended wiring (choose one when integrating):

        1. **OpenPages REST / administration API**
           ``GET {base_url}/oprest/api/v1/admin/logcollector/export``
           (or the tenant-specific LogCollector download endpoint documented for
           your OpenPages version), with ``Authorization: Bearer {api_token}``
           (or the OpenPages session / Basic auth scheme your deployment uses).
           The response body is the binary ``.zip`` bundle.

        2. **watsonx Orchestrate skill**
           Invoke an Orchestrate skill that wraps the same LogCollector export
           (e.g. ``openpages.logcollector.export``), passing ``base_url`` and a
           secret-backed token, then return the skill's zip payload bytes.

        Raises:
            NotImplementedError: Always, until a real endpoint/skill is wired.
        """
        raise NotImplementedError(
            "OpenPagesAPISource is not configured. Wire "
            "GET {base_url}/oprest/api/v1/admin/logcollector/export "
            "(Authorization: Bearer {api_token}) or a watsonx Orchestrate "
            "skill such as openpages.logcollector.export, then return the "
            "response zip bytes. Falling back to manual upload "
            f"(LooseFilesUploadSource) is required until then. (base_url={self.base_url!r})"
        )
