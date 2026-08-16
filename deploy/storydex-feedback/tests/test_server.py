from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import http.client
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("storydex_feedback_server", MODULE_PATH)
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class FeedbackStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = server.FeedbackStore(Path(self.temporary.name))
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_persists_feedback_and_authenticated_image_metadata(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"fixture"
        feedback_id = self.store.save({
            "platform": "android",
            "source": "error",
            "category": "stability",
            "description": "Session persistence failed after chapter generation.",
            "error": {"message": "permission denied"},
            "diagnostics": {
                "runtime": "storydex-coomi-rs",
                "rawPrompt": "private manuscript content",
                "nested": {"accessToken": "private", "safe": True},
            },
            "images": [{
                "name": "failure.png",
                "mimeType": "image/png",
                "dataBase64": base64.b64encode(png).decode("ascii"),
            }],
        }, "127.0.0.1", "test")

        listing = self.store.list("permission", 20)
        self.assertEqual(listing, [])
        listing = self.store.list("Session persistence", 20)
        self.assertEqual(listing[0]["id"], feedback_id)
        self.assertEqual(listing[0]["platform"], "android")
        self.assertEqual(listing[0]["image_count"], 1)
        self.assertEqual(self.store.list("", 20, "windows"), [])
        self.assertEqual(self.store.list("", 20, "android")[0]["id"], feedback_id)
        detail = self.store.detail(feedback_id)
        self.assertEqual(detail["platform"], "android")
        self.assertEqual(detail["error"]["message"], "permission denied")
        self.assertNotIn("rawPrompt", detail["diagnostics"])
        self.assertNotIn("accessToken", detail["diagnostics"]["nested"])
        self.assertTrue(detail["diagnostics"]["nested"]["safe"])
        image = self.store.image(detail["images"][0]["id"])
        self.assertEqual(image[0].read_bytes(), png)

    def test_rejects_spoofed_image_content(self) -> None:
        with self.assertRaisesRegex(server.FeedbackError, "图片内容"):
            self.store.save({
                "source": "settings",
                "description": "This is not really a PNG image.",
                "images": [{
                    "name": "fake.png",
                    "mimeType": "image/png",
                    "dataBase64": base64.b64encode(b"not-png").decode("ascii"),
                }],
            }, "127.0.0.1", "test")

    def test_signed_admin_token_expires(self) -> None:
        token = server.issue_token(self.store)
        self.assertTrue(server.valid_token(self.store, token))
        expired_payload = f"{int(time.time()) - 1}.nonce"
        signature = server.hmac.new(
            self.store.auth_secret(), expired_payload.encode("ascii"), server.hashlib.sha256
        ).hexdigest()
        self.assertFalse(server.valid_token(self.store, f"{expired_payload}.{signature}"))

    def test_duplicate_submission_is_idempotent(self) -> None:
        payload = {
            "submissionId": "same-submission",
            "source": "settings",
            "description": "A feedback retry should not create a duplicate.",
        }
        first = self.store.save(payload, "127.0.0.1", "test")
        second = self.store.save(payload, "127.0.0.1", "test")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.list("", 20)), 1)

    def test_tool_failure_analysis_is_validated_and_filterable(self) -> None:
        feedback_id = self.store.save({
            "submissionId": "tool-analysis-001",
            "platform": "android",
            "source": "error",
            "category": "tool_failure_analysis",
            "description": "本轮工具调用失败三次，已生成脱敏工程分析。",
            "error": {
                "feedbackType": "tool_failure_analysis",
                "analysisStatus": "ready",
                "failureCount": 3,
                "detail": "失败链、证据、推断、修复建议与验收条件。",
            },
            "diagnostics": {"failureCount": 3, "redactionVersion": "storydex-tool-trace-v1"},
            "privacy": {"conversation": False, "projectFiles": False, "apiKeys": False},
        }, "127.0.0.1", "test")

        self.assertEqual(
            self.store.list("", 20, "android", "tool_failure_analysis")[0]["id"],
            feedback_id,
        )
        self.assertEqual(self.store.list("", 20, "windows", "tool_failure_analysis"), [])
        detail = self.store.detail(feedback_id)
        self.assertEqual(detail["error"]["analysisStatus"], "ready")
        self.assertEqual(detail["diagnostics"]["redactionVersion"], "storydex-tool-trace-v1")

    def test_tool_failure_analysis_rejects_incomplete_protocol(self) -> None:
        with self.assertRaisesRegex(server.FeedbackError, "至少需要三次失败"):
            self.store.save({
                "source": "error",
                "category": "tool_failure_analysis",
                "description": "工具故障分析数据不完整，应当拒绝。",
                "error": {
                    "feedbackType": "tool_failure_analysis",
                    "analysisStatus": "ready",
                    "failureCount": 2,
                    "detail": "insufficient",
                },
            }, "127.0.0.1", "test")

    def test_android_download_counter_starts_at_baseline_and_is_idempotent(self) -> None:
        self.assertEqual(self.store.android_downloads(), 28)
        self.assertEqual(self.store.record_android_download("android-event-0001"), 29)
        self.assertEqual(self.store.record_android_download("android-event-0001"), 29)
        self.assertEqual(self.store.record_android_download("android-event-0002"), 30)

    def test_android_download_counter_rejects_invalid_event_id(self) -> None:
        with self.assertRaisesRegex(server.FeedbackError, "eventId"):
            self.store.record_android_download("bad")

    def test_daily_active_is_unique_by_utc_day_ip_and_platform(self) -> None:
        first_day = datetime(2026, 8, 16, 23, 59, tzinfo=timezone.utc)
        self.assertTrue(self.store.record_daily_active("android", "203.0.113.10", "0.1.3", "test", first_day))
        self.assertFalse(self.store.record_daily_active("android", "203.0.113.10", "0.1.3", "test", first_day))
        self.assertTrue(self.store.record_daily_active("windows", "203.0.113.10", "2.0.5", "test", first_day))
        self.assertTrue(self.store.record_daily_active("android", "203.0.113.11", "0.1.3", "test", first_day))
        self.assertTrue(self.store.record_daily_active(
            "android", "203.0.113.10", "0.1.3", "test", first_day + timedelta(minutes=2)
        ))

        stats = self.store.activity_stats(2, first_day + timedelta(minutes=2))
        self.assertEqual(stats["series"][0]["android"], 2)
        self.assertEqual(stats["series"][0]["windows"], 1)
        self.assertEqual(stats["series"][1]["android"], 1)
        self.assertEqual(stats["period"], {"windows": 1, "android": 3, "total": 4})

    def test_daily_active_rejects_invalid_platform(self) -> None:
        with self.assertRaisesRegex(server.FeedbackError, "platform"):
            self.store.record_daily_active("web", "203.0.113.10", "1", "test")

    def test_stats_endpoint_requires_admin_and_dau_accepts_empty_post(self) -> None:
        httpd = server.ThreadingFeedbackServer(
            ("127.0.0.1", 0), server.build_handler(self.store, b"admin")
        )
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            connection.request("POST", f"{server.BASE_PATH}/api/stats/dau/android", body=b"", headers={
                "X-Real-IP": "203.0.113.12",
                "X-Storydex-Version": "0.1.3",
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["counted"])

            connection.request("GET", f"{server.BASE_PATH}/admin/api/stats?days=30")
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()

            token = server.issue_token(self.store)
            connection.request("GET", f"{server.BASE_PATH}/admin/api/stats?days=30", headers={
                "Authorization": f"Bearer {token}",
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read())
            self.assertEqual(payload["today"]["android"], 1)
            connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_connect_passes_a_string_path_to_legacy_sqlite(self) -> None:
        original_connect = server.sqlite3.connect
        observed_paths = []

        def legacy_connect(database, *args, **kwargs):
            self.assertIsInstance(database, str)
            observed_paths.append(database)
            return original_connect(database, *args, **kwargs)

        with mock.patch.object(server.sqlite3, "connect", side_effect=legacy_connect):
            with self.store.connect() as connection:
                connection.execute("SELECT 1")

        self.assertEqual(observed_paths, [str(self.store.database_path)])

    def test_initialize_migrates_legacy_feedback_table_to_windows(self) -> None:
        legacy_root = Path(self.temporary.name) / "legacy"
        legacy_data = legacy_root / "data"
        legacy_data.mkdir(parents=True)
        database_path = legacy_data / "feedback.sqlite3"
        connection = sqlite3.connect(str(database_path))
        try:
            connection.execute(
                """CREATE TABLE feedback (
                    id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, received_at TEXT NOT NULL,
                    submitted_at TEXT NOT NULL, source TEXT NOT NULL, category TEXT NOT NULL,
                    description TEXT NOT NULL, contact TEXT NOT NULL, error_json TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL, privacy_json TEXT NOT NULL,
                    client_ip TEXT NOT NULL, user_agent TEXT NOT NULL
                )"""
            )
            connection.execute(
                """INSERT INTO feedback VALUES
                   ('legacy-id', 'legacy-submission', 'now', 'now', 'settings', 'bug',
                    'Legacy desktop feedback', '', '{}', '{}', '{}', '127.0.0.1', 'test')"""
            )
            connection.commit()
        finally:
            connection.close()

        legacy_store = server.FeedbackStore(legacy_root)
        legacy_store.initialize()

        listing = legacy_store.list("", 20, "windows")
        self.assertEqual(listing[0]["id"], "legacy-id")
        self.assertEqual(listing[0]["platform"], "windows")


if __name__ == "__main__":
    unittest.main()
