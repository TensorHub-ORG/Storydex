from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import tempfile


START_MARKER = "<!-- storydex-android-download:start -->"
END_MARKER = "<!-- storydex-android-download:end -->"
OVERLAY_PATTERN = re.compile(
    rf"(?m)^[ \t]*{re.escape(START_MARKER)}.*?^[ \t]*{re.escape(END_MARKER)}[ \t]*\n?",
    re.DOTALL | re.MULTILINE,
)


def inject_overlay(index_path: Path, script_url: str, version: str) -> bool:
    original = index_path.read_text(encoding="utf-8")
    block = (
        f"  {START_MARKER}\n"
        f'  <script defer src="{script_url}"></script>\n'
        f"  {END_MARKER}\n"
    )
    if block in original:
        return False
    cleaned = OVERLAY_PATTERN.sub("", original)
    if "</body>" not in cleaned:
        raise ValueError(f"missing </body> in {index_path}")
    updated = cleaned.replace("</body>", f"{block}</body>", 1)
    if updated == original:
        return False

    backup = index_path.with_name(f"{index_path.name}.pre-android-v{version}.bak")
    if not backup.exists():
        shutil.copy2(index_path, backup)

    # mkstemp creates the temporary file with mode 0600; preserve the original
    # index.html permissions (typically 0644) so the web server can still read
    # the replaced file after os.replace.
    original_mode = index_path.stat().st_mode & 0o777

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{index_path.name}.", suffix=".tmp", dir=index_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(updated)
        os.chmod(temporary_name, original_mode)
        os.replace(temporary_name, index_path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("index", type=Path)
    parser.add_argument("script_url")
    parser.add_argument("version")
    args = parser.parse_args()
    changed = inject_overlay(args.index, args.script_url, args.version)
    print("updated" if changed else "unchanged")


if __name__ == "__main__":
    main()
