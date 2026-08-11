from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import tempfile
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
        self.assertEqual(listing[0]["image_count"], 1)
        detail = self.store.detail(feedback_id)
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

    def test_android_download_counter_starts_at_baseline_and_is_idempotent(self) -> None:
        self.assertEqual(self.store.android_downloads(), 28)
        self.assertEqual(self.store.record_android_download("android-event-0001"), 29)
        self.assertEqual(self.store.record_android_download("android-event-0001"), 29)
        self.assertEqual(self.store.record_android_download("android-event-0002"), 30)

    def test_android_download_counter_rejects_invalid_event_id(self) -> None:
        with self.assertRaisesRegex(server.FeedbackError, "eventId"):
            self.store.record_android_download("bad")

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


if __name__ == "__main__":
    unittest.main()
