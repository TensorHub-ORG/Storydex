from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict


SOURCE_CONTRACT_VERSION = 1
SOURCE_REVISION_PREFIX = "sha256:"


def normalize_source_path(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = PurePosixPath(normalized.lstrip("/"))
    if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid source path: {value}")
    return path.as_posix()


def source_revision_id(raw: bytes) -> str:
    return f"{SOURCE_REVISION_PREFIX}{hashlib.sha256(bytes(raw)).hexdigest()}"


def validate_source_revision(value: str) -> str:
    revision = str(value or "").strip().lower()
    digest = revision.removeprefix(SOURCE_REVISION_PREFIX)
    if not revision.startswith(SOURCE_REVISION_PREFIX) or len(digest) != 64:
        raise ValueError(f"invalid source revision: {value}")
    if any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"invalid source revision: {value}")
    return revision


def source_line_count(text: str) -> int:
    return len(str(text).splitlines())


@dataclass(frozen=True)
class SourceRevision:
    path: str
    revision: str
    size_bytes: int
    mtime_ns: int
    total_chars: int
    total_lines: int
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_source_path(self.path))
        object.__setattr__(self, "revision", validate_source_revision(self.revision))
        if self.content_hash:
            object.__setattr__(self, "content_hash", validate_source_revision(self.content_hash))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "_type": "SourceRevision",
            "_version": SOURCE_CONTRACT_VERSION,
            "path": self.path,
            "revision": self.revision,
            "sizeBytes": max(0, int(self.size_bytes)),
            "mtimeNs": max(0, int(self.mtime_ns)),
            "totalChars": max(0, int(self.total_chars)),
            "totalLines": max(0, int(self.total_lines)),
            "contentHash": self.content_hash,
        }


@dataclass(frozen=True)
class SourceSpan:
    revision: str
    start_char: int
    end_char: int
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision", validate_source_revision(self.revision))
        if min(self.start_char, self.end_char, self.start_byte, self.end_byte) < 0:
            raise ValueError("source span offsets must be non-negative")
        if self.end_char < self.start_char or self.end_byte < self.start_byte:
            raise ValueError("source span end offsets must be exclusive and ordered")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("source span line numbers must be one-based and ordered")

    def to_dict(self, *, include_revision: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "_type": "SourceSpan",
            "_version": SOURCE_CONTRACT_VERSION,
            "startChar": self.start_char,
            "endChar": self.end_char,
            "startByte": self.start_byte,
            "endByte": self.end_byte,
            "startLine": self.start_line,
            "endLine": self.end_line,
            "endExclusive": True,
        }
        if include_revision:
            payload["revision"] = self.revision
        return payload


def describe_utf8_source(
    *,
    path: str,
    raw: bytes,
    mtime_ns: int,
) -> tuple[SourceRevision, str]:
    text = bytes(raw).decode("utf-8")
    revision = source_revision_id(raw)
    return (
        SourceRevision(
            path=path,
            revision=revision,
            size_bytes=len(raw),
            mtime_ns=mtime_ns,
            total_chars=len(text),
            total_lines=source_line_count(text),
            content_hash=revision,
        ),
        text,
    )


def build_source_span(
    text: str,
    *,
    revision: str,
    start_char: int,
    end_char: int,
) -> SourceSpan:
    content = str(text)
    start = max(0, int(start_char))
    end = min(len(content), max(start, int(end_char)))
    start_line = content.count("\n", 0, start) + 1
    end_anchor = max(start, end - 1)
    end_line = content.count("\n", 0, end_anchor) + 1
    return SourceSpan(
        revision=revision,
        start_char=start,
        end_char=end,
        start_byte=len(content[:start].encode("utf-8")),
        end_byte=len(content[:end].encode("utf-8")),
        start_line=start_line,
        end_line=end_line,
    )
