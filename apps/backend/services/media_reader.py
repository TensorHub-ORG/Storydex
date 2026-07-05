from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class MediaReader:
    def read_special_document(self, path: Path, *, workspace_root: Path) -> Optional[Dict[str, Any]]:
        suffix = path.suffix.lower()
        if suffix == ".ipynb":
            return self._read_notebook(path=path, workspace_root=workspace_root)
        if suffix == ".pdf":
            return self._read_pdf(path=path, workspace_root=workspace_root)
        if suffix in IMAGE_SUFFIXES:
            return self._read_image(path=path, workspace_root=workspace_root)
        return None

    def _read_notebook(self, *, path: Path, workspace_root: Path) -> Dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cells = payload.get("cells") if isinstance(payload, dict) else []
        rendered_cells: List[str] = []
        cell_summaries: List[Dict[str, Any]] = []
        if isinstance(cells, list):
            for index, cell in enumerate(cells, start=1):
                if not isinstance(cell, dict):
                    continue
                cell_type = str(cell.get("cell_type") or "unknown")
                source = cell.get("source")
                if isinstance(source, list):
                    source_text = "".join(str(item) for item in source)
                else:
                    source_text = str(source or "")
                rendered_cells.append(f"## Cell {index} [{cell_type}]\n{source_text}".rstrip())
                cell_summaries.append(
                    {
                        "index": index,
                        "cellType": cell_type,
                        "sourceLength": len(source_text),
                        "lineCount": len(source_text.splitlines()),
                    }
                )

        stat = path.stat()
        return {
            "relativePath": path.relative_to(workspace_root).as_posix(),
            "content": "\n\n".join(rendered_cells),
            "size": stat.st_size,
            "updatedAt": _mtime_iso(path),
            "extension": path.suffix.lower(),
            "kind": "notebook",
            "media": {
                "type": "notebook",
                "cellCount": len(cell_summaries),
                "cells": cell_summaries,
            },
        }

    def _read_pdf(self, *, path: Path, workspace_root: Path) -> Dict[str, Any]:
        data = path.read_bytes()
        page_count = data.count(b"/Type /Page")
        text_preview = _safe_pdf_text_preview(data)
        stat = path.stat()
        return {
            "relativePath": path.relative_to(workspace_root).as_posix(),
            "content": text_preview or "[PDF binary document: text extraction unavailable without an optional PDF parser]",
            "size": stat.st_size,
            "updatedAt": _mtime_iso(path),
            "extension": path.suffix.lower(),
            "kind": "pdf",
            "media": {
                "type": "pdf",
                "pageCountEstimate": page_count,
                "textExtraction": "basic-preview" if text_preview else "metadata-only",
            },
        }

    def _read_image(self, *, path: Path, workspace_root: Path) -> Dict[str, Any]:
        data = path.read_bytes()
        dimensions = _detect_image_dimensions(data, path.suffix.lower())
        stat = path.stat()
        return {
            "relativePath": path.relative_to(workspace_root).as_posix(),
            "content": "[Image file: binary content omitted from text context]",
            "size": stat.st_size,
            "updatedAt": _mtime_iso(path),
            "extension": path.suffix.lower(),
            "kind": "image",
            "media": {
                "type": "image",
                "width": dimensions.get("width"),
                "height": dimensions.get("height"),
                "format": dimensions.get("format") or path.suffix.lower().lstrip("."),
            },
        }


def _mtime_iso(path: Path) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _safe_pdf_text_preview(data: bytes, *, max_chars: int = 4000) -> str:
    fragments: List[str] = []
    for marker in (b"(", b"<"):
        if len("".join(fragments)) >= max_chars:
            break
        cursor = 0
        while len("".join(fragments)) < max_chars:
            start = data.find(marker, cursor)
            if start < 0:
                break
            end_marker = b")" if marker == b"(" else b">"
            end = data.find(end_marker, start + 1)
            if end < 0:
                break
            raw = data[start + 1 : end]
            cursor = end + 1
            try:
                text = raw.decode("utf-8", errors="ignore")
            except Exception:
                continue
            text = " ".join(text.split())
            if len(text) >= 12 and any(ch.isalpha() for ch in text):
                fragments.append(text[:400])
    return "\n".join(fragments)[:max_chars].strip()


def _detect_image_dimensions(data: bytes, suffix: str) -> Dict[str, Any]:
    if suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return {"format": "png", "width": width, "height": height}

    if suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8"):
        dims = _jpeg_dimensions(data)
        if dims:
            return {"format": "jpeg", **dims}

    if suffix == ".gif" and data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return {"format": "gif", "width": width, "height": height}

    if suffix == ".webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        dims = _webp_dimensions(data)
        if dims:
            return {"format": "webp", **dims}

    return {"format": suffix.lstrip("."), "width": None, "height": None}


def _jpeg_dimensions(data: bytes) -> Optional[Dict[str, int]]:
    cursor = 2
    while cursor + 9 < len(data):
        if data[cursor] != 0xFF:
            cursor += 1
            continue
        marker = data[cursor + 1]
        cursor += 2
        if marker in {0xD8, 0xD9}:
            continue
        if cursor + 2 > len(data):
            break
        length = int.from_bytes(data[cursor : cursor + 2], "big")
        if length < 2 or cursor + length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(data[cursor + 3 : cursor + 5], "big")
            width = int.from_bytes(data[cursor + 5 : cursor + 7], "big")
            return {"width": width, "height": height}
        cursor += length
    return None


def _webp_dimensions(data: bytes) -> Optional[Dict[str, int]]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return {"width": width, "height": height}
    if chunk == b"VP8 " and len(data) >= 30:
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return {"width": width, "height": height}
    return None
