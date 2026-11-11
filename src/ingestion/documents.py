"""Document checks (content vs extension), metadata parsing and stable IDs."""

from __future__ import annotations

import codecs
import hashlib
import zipfile
from typing import BinaryIO

ALLOWED_AUDIENCES = ("STAFF", "PARENT")  # canonical order
MAX_TITLE_CHARS = 120

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown",
    "txt": "text/plain",
}


def parse_audience(raw: str | None) -> list[str] | None:
    """``"STAFF, PARENT"`` -> ``["STAFF", "PARENT"]``; ``None`` if missing or invalid.

    Must be a non-empty, comma-separated subset of STAFF and PARENT (exact case).
    Returned in canonical order without duplicates.
    """
    if raw is None:
        return None
    parts = [part.strip() for part in raw.split(",")]
    if not parts or any(part not in ALLOWED_AUDIENCES for part in parts):
        return None
    return [audience for audience in ALLOWED_AUDIENCES if audience in parts]


def resolve_title(raw: str | None, relative_path: str) -> str:
    """Metadata title if it is 1-120 characters after trimming, else the file name stem."""
    if raw is not None:
        title = raw.strip()
        if 1 <= len(title) <= MAX_TITLE_CHARS:
            return title
    filename = relative_path.rsplit("/", 1)[-1]
    return filename.rsplit(".", 1)[0][:MAX_TITLE_CHARS]


def document_id(school_id: str, relative_path: str) -> str:
    """Stable across re-uploads of the same path (case-insensitive)."""
    digest = hashlib.sha256(f"{school_id}/{relative_path.lower()}".encode()).hexdigest()
    return f"doc-{digest[:12]}"


def document_version(content_sha256: str) -> str:
    return f"v-{content_sha256[:12]}"


def content_matches_extension(extension: str, file: BinaryIO) -> bool:
    """Check the content really is what the extension says. ``file`` must be seekable."""
    file.seek(0)
    if extension == "pdf":
        return file.read(4) == b"%PDF"
    if extension == "docx":
        # A ZIP header alone also matches .xlsx and .zip, so look for the Word part.
        try:
            with zipfile.ZipFile(file) as archive:
                return "word/document.xml" in archive.namelist()
        except zipfile.BadZipFile:
            return False
    if extension in ("md", "txt"):
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            while chunk := file.read(64 * 1024):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            return False
        return True
    return False
