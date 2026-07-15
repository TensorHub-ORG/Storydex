from __future__ import annotations

import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.coomi_version_service import check_coomi_version  # noqa: E402


def main() -> int:
    status = check_coomi_version(requirements_path=REPOSITORY_ROOT / "requirements.txt")
    print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
