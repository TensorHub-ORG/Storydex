#!/usr/bin/env python3
"""Small, dependency-free feedback receiver for the Storydex update host."""

from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Any, Iterator
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4


BASE_PATH = "/storydex/feedback"
MAX_REQUEST_BYTES = 24 * 1024 * 1024
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TEXT = 5000
TOKEN_LIFETIME_SECONDS = 12 * 60 * 60
MAX_SUBMISSIONS_PER_IP_HOUR = 30
COOMI_LOGIN_URL = "https://updates.septemc.com/coomi/feedback/api/admin/login"
IMAGE_TYPES = {
    "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": (".jpg", b"\xff\xd8\xff"),
    "image/webp": (".webp", b"RIFF"),
}
SENSITIVE_KEY_MARKERS = (
    "apikey", "authorization", "token", "secret", "prompt", "conversation",
    "messages", "manuscript", "content", "requestbody", "responsebody",
)


class FeedbackError(ValueError):
    def __init__(self, message: str, status: int = HTTPStatus.UNPROCESSABLE_ENTITY):
        super().__init__(message)
        self.status = int(status)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def bounded_text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def sanitize_structured_value(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "[truncated]"
    if isinstance(value, dict):
        output = {}
        for key, item in list(value.items())[:100]:
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if any(marker in normalized for marker in SENSITIVE_KEY_MARKERS):
                continue
            output[str(key)[:80]] = sanitize_structured_value(item, depth + 1)
        return output
    if isinstance(value, list):
        return [sanitize_structured_value(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:MAX_TEXT]


def load_json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedbackError("请求正文必须是有效的 UTF-8 JSON。", HTTPStatus.BAD_REQUEST) from exc
    if not isinstance(value, dict):
        raise FeedbackError("请求正文必须是 JSON 对象。", HTTPStatus.BAD_REQUEST)
    return value


def validate_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    source = bounded_text(payload.get("source"), 20)
    if source not in {"error", "settings"}:
        raise FeedbackError("source 必须是 error 或 settings。")
    description = bounded_text(payload.get("description"))
    if len(description) < 5:
        raise FeedbackError("问题描述至少需要 5 个字符。")
    images = payload.get("images") or []
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise FeedbackError(f"最多只能上传 {MAX_IMAGES} 张图片。")

    error_value = payload.get("error")
    diagnostics = payload.get("diagnostics") or {}
    if error_value is not None and not isinstance(error_value, dict):
        raise FeedbackError("error 必须是对象或 null。")
    if not isinstance(diagnostics, dict):
        raise FeedbackError("diagnostics 必须是对象。")

    return {
        "submission_id": bounded_text(payload.get("submissionId"), 120) or str(uuid4()),
        "submitted_at": bounded_text(payload.get("submittedAt"), 80) or utc_now(),
        "source": source,
        "category": bounded_text(payload.get("category"), 40) or "bug",
        "description": description,
        "contact": bounded_text(payload.get("contact"), 200),
        "error": sanitize_structured_value(error_value or {}),
        "diagnostics": sanitize_structured_value(diagnostics),
        "privacy": sanitize_structured_value(
            payload.get("privacy") if isinstance(payload.get("privacy"), dict) else {}
        ),
        "images": images,
    }


class FeedbackStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.data_dir = self.root / "data"
        self.image_dir = self.data_dir / "images"
        self.database_path = self.data_dir / "feedback.sqlite3"
        self.secret_path = self.data_dir / "auth-secret"

    def initialize(self) -> None:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    submission_id TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    contact TEXT NOT NULL,
                    error_json TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL,
                    privacy_json TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    user_agent TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY,
                    feedback_id TEXT NOT NULL REFERENCES feedback(id) ON DELETE CASCADE,
                    original_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    byte_size INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS feedback_received_at_idx
                    ON feedback(received_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS feedback_submission_id_idx
                    ON feedback(submission_id);
                """
            )
        if not self.secret_path.exists():
            self.secret_path.write_text(secrets.token_hex(48), encoding="ascii")
            try:
                self.secret_path.chmod(0o600)
            except OSError:
                pass

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def auth_secret(self) -> bytes:
        return self.secret_path.read_text(encoding="ascii").strip().encode("ascii")

    def save(self, payload: dict[str, Any], client_ip: str, user_agent: str) -> str:
        data = validate_feedback(payload)
        normalized_ip = bounded_text(client_ip, 120)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM feedback WHERE submission_id = ?", (data["submission_id"],)
            ).fetchone()
            if existing is not None:
                return str(existing["id"])
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            recent_count = connection.execute(
                "SELECT COUNT(*) FROM feedback WHERE client_ip = ? AND received_at >= ?",
                (normalized_ip, cutoff),
            ).fetchone()[0]
            if int(recent_count) >= MAX_SUBMISSIONS_PER_IP_HOUR:
                raise FeedbackError("反馈提交过于频繁，请稍后重试。", HTTPStatus.TOO_MANY_REQUESTS)
        feedback_id = str(uuid4())
        staged: list[tuple[str, str, str, int]] = []
        written: list[Path] = []
        try:
            for image in data["images"]:
                if not isinstance(image, dict):
                    raise FeedbackError("图片条目必须是对象。")
                mime = bounded_text(image.get("mimeType"), 80).lower()
                image_type = IMAGE_TYPES.get(mime)
                if image_type is None:
                    raise FeedbackError("仅支持 PNG、JPEG 或 WebP 图片。")
                encoded = image.get("dataBase64")
                if not isinstance(encoded, str):
                    raise FeedbackError("图片缺少 base64 数据。")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise FeedbackError("图片 base64 数据无效。") from exc
                extension, signature = image_type
                if not raw or len(raw) > MAX_IMAGE_BYTES or not raw.startswith(signature):
                    raise FeedbackError("图片内容、格式或大小无效。")
                if mime == "image/webp" and (len(raw) < 12 or raw[8:12] != b"WEBP"):
                    raise FeedbackError("WebP 图片签名无效。")
                image_id = str(uuid4())
                stored_name = f"{feedback_id}-{image_id}{extension}"
                destination = self.image_dir / stored_name
                destination.write_bytes(raw)
                written.append(destination)
                original_name = Path(bounded_text(image.get("name"), 160)).name
                staged.append((image_id, original_name, mime, len(raw)))

            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO feedback (
                        id, submission_id, received_at, submitted_at, source, category,
                        description, contact, error_json, diagnostics_json, privacy_json,
                        client_ip, user_agent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        feedback_id, data["submission_id"], utc_now(), data["submitted_at"],
                        data["source"], data["category"], data["description"], data["contact"],
                        compact_json(data["error"]), compact_json(data["diagnostics"]),
                        compact_json(data["privacy"]), normalized_ip,
                        bounded_text(user_agent, 500),
                    ),
                )
                for (image_id, original_name, mime, size), destination in zip(staged, written):
                    connection.execute(
                        """INSERT INTO images
                           (id, feedback_id, original_name, mime_type, stored_name, byte_size)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (image_id, feedback_id, original_name, mime, destination.name, size),
                    )
            return feedback_id
        except Exception:
            for destination in written:
                destination.unlink(missing_ok=True)
            raise

    def list(self, query: str, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        pattern = f"%{query[:200]}%"
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT f.id, f.received_at, f.source, f.category, f.description,
                          f.contact, f.client_ip, COUNT(i.id) AS image_count
                   FROM feedback f LEFT JOIN images i ON i.feedback_id = f.id
                   WHERE ? = '' OR f.description LIKE ? OR f.contact LIKE ?
                         OR f.category LIKE ? OR f.id LIKE ?
                   GROUP BY f.id ORDER BY f.received_at DESC LIMIT ?""",
                (query, pattern, pattern, pattern, pattern, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def detail(self, feedback_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
            if row is None:
                return None
            images = connection.execute(
                "SELECT id, original_name, mime_type, byte_size FROM images WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchall()
        result = dict(row)
        for key in ("error_json", "diagnostics_json", "privacy_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        result["images"] = [
            {
                **dict(image),
                "url": f"{BASE_PATH}/admin/api/image?id={quote(image['id'])}",
            }
            for image in images
        ]
        return result

    def image(self, image_id: str) -> tuple[Path, str] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT stored_name, mime_type FROM images WHERE id = ?", (image_id,)
            ).fetchone()
        if row is None:
            return None
        path = (self.image_dir / row["stored_name"]).resolve()
        if path.parent != self.image_dir.resolve() or not path.is_file():
            return None
        return path, str(row["mime_type"])


def issue_token(store: FeedbackStore) -> str:
    expires = int(time.time()) + TOKEN_LIFETIME_SECONDS
    nonce = secrets.token_hex(12)
    payload = f"{expires}.{nonce}"
    signature = hmac.new(store.auth_secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def valid_token(store: FeedbackStore, token: str) -> bool:
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit():
        return False
    payload = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(store.auth_secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return int(parts[0]) >= int(time.time()) and hmac.compare_digest(parts[2], expected)


def verify_admin_password(password: str) -> bool:
    if not password or len(password) > 500:
        return False
    body = compact_json({"password": password}).encode("utf-8")
    request = urlrequest.Request(
        COOMI_LOGIN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "StorydexFeedback/1"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
            return response.status == HTTPStatus.OK and bool(result.get("token"))
    except (urlerror.URLError, ValueError, json.JSONDecodeError):
        return False


class FeedbackHandler(BaseHTTPRequestHandler):
    server_version = "StorydexFeedback/1"
    store: FeedbackStore
    admin_html: bytes

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format_string % args}", flush=True)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = compact_json(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise FeedbackError("Content-Length 无效。", HTTPStatus.BAD_REQUEST) from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise FeedbackError("请求正文为空或超过 24 MB。", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        return load_json_object(self.rfile.read(length))

    def bearer_token(self) -> str:
        match = re.fullmatch(r"Bearer\s+(.+)", self.headers.get("Authorization", ""), re.IGNORECASE)
        return match.group(1).strip() if match else ""

    def require_admin(self) -> bool:
        if valid_token(self.store, self.bearer_token()):
            return True
        self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "管理员登录已失效。"})
        return False

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        try:
            payload = self.read_json()
            if path == f"{BASE_PATH}/api":
                client_ip = self.headers.get("X-Real-IP") or self.client_address[0]
                feedback_id = self.store.save(payload, client_ip, self.headers.get("User-Agent", ""))
                self.send_json(HTTPStatus.CREATED, {"ok": True, "id": feedback_id})
                return
            if path == f"{BASE_PATH}/api/admin/login":
                if not verify_admin_password(bounded_text(payload.get("password"), 500)):
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "密码错误或认证服务不可用。"})
                    return
                self.send_json(HTTPStatus.OK, {"token": issue_token(self.store), "expiresIn": TOKEN_LIFETIME_SECONDS})
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在。"})
        except FeedbackError as exc:
            self.send_json(exc.status, {"error": str(exc)})
        except Exception as exc:  # Keep internal details out of the public response.
            print(f"feedback request failed: {exc!r}", flush=True)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "反馈服务处理失败。"})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)
        if path in {f"{BASE_PATH}/health", f"{BASE_PATH}/api/health"}:
            self.send_json(HTTPStatus.OK, {"ok": True, "service": "storydex-feedback"})
            return
        if path in {BASE_PATH, f"{BASE_PATH}/admin"}:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(self.admin_html)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(self.admin_html)
            return
        if not path.startswith(f"{BASE_PATH}/admin/api/") or not self.require_admin():
            if not path.startswith(f"{BASE_PATH}/admin/api/"):
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "资源不存在。"})
            return
        if path == f"{BASE_PATH}/admin/api/list":
            query = bounded_text(params.get("q", [""])[0], 200)
            try:
                limit = int(params.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            self.send_json(HTTPStatus.OK, {"items": self.store.list(query, limit)})
            return
        if path == f"{BASE_PATH}/admin/api/detail":
            detail = self.store.detail(bounded_text(params.get("id", [""])[0], 120))
            self.send_json(HTTPStatus.OK if detail else HTTPStatus.NOT_FOUND, detail or {"error": "反馈不存在。"})
            return
        if path == f"{BASE_PATH}/admin/api/image":
            image = self.store.image(bounded_text(params.get("id", [""])[0], 120))
            if image is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "图片不存在。"})
                return
            image_path, mime = image
            body = image_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime or mimetypes.guess_type(image_path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在。"})


def build_handler(store: FeedbackStore, admin_html: bytes) -> type[FeedbackHandler]:
    class BoundFeedbackHandler(FeedbackHandler):
        pass

    BoundFeedbackHandler.store = store
    BoundFeedbackHandler.admin_html = admin_html
    return BoundFeedbackHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    store = FeedbackStore(args.root)
    store.initialize()
    admin_path = Path(__file__).with_name("admin.html")
    admin_html = admin_path.read_bytes()
    if args.check:
        print(compact_json({"ok": True, "database": str(store.database_path), "admin": str(admin_path)}))
        return
    server = ThreadingHTTPServer((args.host, args.port), build_handler(store, admin_html))
    print(f"Storydex feedback listening on http://{args.host}:{args.port}{BASE_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
