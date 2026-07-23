from __future__ import annotations

from pathlib import Path


STORY_WORD_COUNT_ALGORITHM = "storydex_visible_characters_v1"


def count_story_text_words(content: str) -> int:
    """Return the same objective count shown by the Storydex editor.

    Storydex treats every non-whitespace Unicode character as one displayed
    "word" for fiction targets.  Keeping this in one backend helper prevents
    Agent prompts, file APIs and post-write validation from drifting apart.
    """

    return sum(1 for char in str(content or "") if not char.isspace())


def count_story_file_words(path: Path) -> int:
    return count_story_text_words(Path(path).read_text(encoding="utf-8-sig"))
