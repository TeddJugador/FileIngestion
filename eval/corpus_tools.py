"""Shared helpers for the evaluation: the manifest, document IDs and corpus building."""

from __future__ import annotations

import hashlib
import io
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

EVAL_DIR = Path(__file__).resolve().parent
CORPUS_DIR = EVAL_DIR / "corpus"
SOURCE_DIR = CORPUS_DIR / "source"
SCHOOL_NAME = "Kingfisher Primary School"


@dataclass(frozen=True)
class CorpusDocument:
    key: str
    file: str
    title: str
    audience: tuple[str, ...]

    @property
    def extension(self) -> str:
        return self.file.rsplit(".", 1)[-1]

    @property
    def source(self) -> Path:
        return SOURCE_DIR / f"{self.key}.md"

    @property
    def path(self) -> Path:
        return CORPUS_DIR / self.file


@dataclass(frozen=True)
class Manifest:
    school_id: str
    upload_prefix: str
    documents: tuple[CorpusDocument, ...]

    def by_key(self) -> dict[str, CorpusDocument]:
        return {d.key: d for d in self.documents}

    def relative_path(self, document: CorpusDocument) -> str:
        return f"{self.upload_prefix}/{document.file}"

    def document_id(self, document: CorpusDocument) -> str:
        """Ingestion's ID for this upload path (src/ingestion/documents.py)."""
        relative = self.relative_path(document).lower()
        return f"doc-{hashlib.sha256(f'{self.school_id}/{relative}'.encode()).hexdigest()[:12]}"


def load_manifest(path: Path = CORPUS_DIR / "manifest.yaml") -> Manifest:
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    return Manifest(
        school_id=raw["school_id"],
        upload_prefix=raw["upload_prefix"],
        documents=tuple(
            CorpusDocument(
                key=d["key"], file=d["file"], title=d["title"], audience=tuple(d["audience"])
            )
            for d in raw["documents"]
        ),
    )


# --- building PDF and DOCX from the Markdown sources -------------------------------------


def _blocks(markdown: str) -> list[tuple[str, Any]]:
    """("title" | "heading" | "paragraph", text) and ("table", rows) in order."""
    blocks: list[tuple[str, Any]] = []
    paragraph: list[str] = []
    table: list[list[str]] = []

    def flush() -> None:
        if paragraph:
            blocks.append(("paragraph", " ".join(paragraph)))
            paragraph.clear()
        if table:
            blocks.append(("table", [row[:] for row in table]))
            table.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("## "):
            flush()
            blocks.append(("heading", stripped[3:]))
        elif stripped.startswith("# "):
            flush()
            blocks.append(("title", stripped[2:]))
        elif stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not all(set(c) <= {"-", ":"} for c in cells):  # skip the |---| separator
                table.append(cells)
        else:
            paragraph.append(stripped)
    flush()
    return blocks


def build_pdf(document: CorpusDocument) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    lines: list[tuple[str, str]] = []  # (font, text)
    for kind, value in _blocks(document.source.read_text()):
        if kind == "title":
            lines += [("Helvetica-Bold", value), ("Helvetica", "")]
        elif kind == "heading":
            lines += [("Helvetica-Bold", value)]
        elif kind == "table":
            lines += [("Helvetica", " | ".join(row)) for row in value] + [("Helvetica", "")]
        else:
            lines += [("Helvetica", text) for text in textwrap.wrap(value, 88)] + [
                ("Helvetica", "")
            ]

    per_page = 7  # booklet-sized pages: each PDF spans 3+ pages with running headers
    pages = [lines[i : i + per_page] for i in range(0, len(lines), per_page)]
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=1)  # invariant: reproducible bytes
    _, height = A4
    for number, page in enumerate(pages, start=1):
        pdf.setFont("Helvetica", 8)
        pdf.drawString(50, height - 40, f"{SCHOOL_NAME} - {document.title}")
        y = height - 80
        for font, text in page:
            pdf.setFont(font, 10)
            pdf.drawString(50, y, text)
            y -= 16
        pdf.setFont("Helvetica", 8)
        pdf.drawString(50, 30, f"Page {number} of {len(pages)}")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def build_docx(document: CorpusDocument) -> bytes:
    from docx import Document

    doc = Document()
    doc.core_properties.author = SCHOOL_NAME
    for kind, value in _blocks(document.source.read_text()):
        if kind == "title":
            doc.add_heading(value, level=0)
        elif kind == "heading":
            doc.add_heading(value, level=1)
        elif kind == "table":
            table = doc.add_table(rows=len(value), cols=len(value[0]))
            for r, row in enumerate(value):
                for c, cell in enumerate(row):
                    table.cell(r, c).text = cell
        else:
            doc.add_paragraph(value)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def build(document: CorpusDocument) -> bytes:
    if document.extension == "pdf":
        return build_pdf(document)
    if document.extension == "docx":
        return build_docx(document)
    return document.source.read_bytes()
