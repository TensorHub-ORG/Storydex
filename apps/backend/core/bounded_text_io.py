from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MAX_FULL_READ_BYTES = 64 * 1024
UTF8_BYTES_PER_CHAR = 4
READ_SLOP_BYTES = 1024
HEAD_TAIL_TRUNCATION_MARKER = "\n... [read_context middle truncated]\n"
TAIL_ANCHOR_MARKER = "... [tail anchor]\n"
TRUNCATION_MARKER = "\n... [truncated]"


@dataclass(frozen=True)
class BoundedTextRead:
    text: str
    total_chars: int
    truncated: bool


def read_text_limited(
    path: Path,
    limit: int,
    *,
    preserve_tail: bool = False,
    middle_marker: str = HEAD_TAIL_TRUNCATION_MARKER,
) -> BoundedTextRead:
    safe_limit = max(1, int(limit))
    target = Path(path)
    try:
        size_bytes = target.stat().st_size
    except OSError:
        size_bytes = 0

    full_read_limit = _full_read_limit(safe_limit)
    if size_bytes <= full_read_limit:
        text = target.read_text(encoding="utf-8", errors="replace")
        if preserve_tail and len(text) > safe_limit:
            head_tail = _head_tail_from_text(text, safe_limit, marker=middle_marker)
            if head_tail is not None:
                return BoundedTextRead(head_tail, len(text), True)
        returned = text[:safe_limit]
        return BoundedTextRead(returned, len(text), len(returned) < len(text))

    if preserve_tail:
        available = safe_limit - len(middle_marker)
        if available > 80:
            head_limit = available // 2
            tail_limit = available - head_limit
            returned = (
                f"{_read_head_chars(target, head_limit)}"
                f"{middle_marker}"
                f"{_read_tail_chars(target, tail_limit, size_bytes=size_bytes)}"
            )
            return BoundedTextRead(returned, max(size_bytes, len(returned) + 1), True)

    returned = _read_head_chars(target, safe_limit)
    return BoundedTextRead(returned, max(size_bytes, len(returned) + 1), True)


def read_text_preview(path: Path, *, max_chars: int = 1200) -> str:
    safe_limit = max(1, int(max_chars))
    read = read_text_limited(path, safe_limit, preserve_tail=False)
    text = str(read.text or "").strip()
    if not read.truncated or len(text) <= safe_limit - len(TRUNCATION_MARKER):
        return text[:safe_limit]
    return text[: max(0, safe_limit - len(TRUNCATION_MARKER))].rstrip() + TRUNCATION_MARKER


def read_text_tail(path: Path, *, max_chars: int = 1200, marker: str = TAIL_ANCHOR_MARKER) -> str:
    safe_limit = max(1, int(max_chars))
    target = Path(path)
    try:
        size_bytes = target.stat().st_size
    except OSError:
        size_bytes = 0

    if size_bytes <= _full_read_limit(safe_limit):
        text = str(target.read_text(encoding="utf-8", errors="replace") or "").strip()
        if len(text) <= safe_limit:
            return text
        tail_size = max(1, safe_limit - len(marker))
        return marker + text[-tail_size:]

    tail_size = max(1, safe_limit - len(marker))
    return marker + _read_tail_chars(target, tail_size, size_bytes=size_bytes).strip()


def _full_read_limit(char_limit: int) -> int:
    return max(MAX_FULL_READ_BYTES, max(1, int(char_limit)) * UTF8_BYTES_PER_CHAR + READ_SLOP_BYTES)


def _head_tail_from_text(text: str, safe_limit: int, *, marker: str) -> str | None:
    available = safe_limit - len(marker)
    if available <= 80:
        return None
    head_limit = available // 2
    tail_limit = available - head_limit
    return f"{text[:head_limit]}{marker}{text[-tail_limit:]}"


def _read_head_chars(path: Path, char_limit: int) -> str:
    safe_limit = max(1, int(char_limit))
    byte_limit = safe_limit * UTF8_BYTES_PER_CHAR + READ_SLOP_BYTES
    with Path(path).open("rb") as handle:
        data = handle.read(byte_limit)
    return data.decode("utf-8", errors="replace")[:safe_limit]


def _read_tail_chars(path: Path, char_limit: int, *, size_bytes: int = 0) -> str:
    safe_limit = max(1, int(char_limit))
    byte_limit = safe_limit * UTF8_BYTES_PER_CHAR + READ_SLOP_BYTES
    if not size_bytes:
        try:
            size_bytes = Path(path).stat().st_size
        except OSError:
            size_bytes = 0
    with Path(path).open("rb") as handle:
        if size_bytes > byte_limit:
            handle.seek(max(0, size_bytes - byte_limit))
        data = handle.read(byte_limit)
    return data.decode("utf-8", errors="replace")[-safe_limit:]
