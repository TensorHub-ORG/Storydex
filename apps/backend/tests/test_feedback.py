from __future__ import annotations

import base64
import asyncio

from fastapi import HTTPException

from api import routes_sys


def test_feedback_forwards_sanitized_diagnostics_and_image(monkeypatch) -> None:
    captured = {}

    async def forward(payload: dict) -> dict:
        captured.update(payload)
        return {"id": "feedback-123"}

    monkeypatch.setattr(routes_sys, "_forward_feedback", forward)
    image = base64.b64encode(b"valid-image-fixture").decode("ascii")
    response = asyncio.run(routes_sys.submit_feedback(routes_sys.FeedbackSubmitRequest(**{
            "source": "error",
            "category": "stability",
            "description": "The agent stopped before persisting the chapter.",
            "errorMessage": "permission denied",
            "errorType": "SessionPersistenceError",
            "errorDetails": {
                "operation": "checkpoint",
                "apiKey": "must-not-leave-the-device",
                "rawPrompt": "must-not-leave-the-device",
                "nested": {
                    "prompt": "private manuscript instruction",
                    "requestBody": "private provider payload",
                    "safe": True,
                },
            },
            "diagnostics": {
                "storydexVersion": "2.0.3",
                "traceId": "trace-1",
                "authorization": "secret",
                "unknown": "not allowlisted",
            },
            "images": [{
                "name": "screenshot.png",
                "mimeType": "image/png",
                "dataUrl": f"data:image/png;base64,{image}",
            }],
        })))

    assert response.data["feedbackId"] == "feedback-123"
    assert captured["diagnostics"] == {"storydexVersion": "2.0.3", "traceId": "trace-1"}
    assert "apiKey" not in captured["error"]["details"]
    assert "rawPrompt" not in captured["error"]["details"]
    assert "prompt" not in captured["error"]["details"]["nested"]
    assert "requestBody" not in captured["error"]["details"]["nested"]
    assert captured["images"][0]["dataBase64"] == image
    assert captured["privacy"] == {"conversationIncluded": False, "projectFilesIncluded": False}


def test_feedback_rejects_unsupported_image_type(monkeypatch) -> None:
    async def forward(payload: dict) -> dict:
        raise AssertionError(f"invalid payload must not be forwarded: {payload}")

    monkeypatch.setattr(routes_sys, "_forward_feedback", forward)
    request = routes_sys.FeedbackSubmitRequest(**{
            "source": "settings",
            "category": "bug",
            "description": "This image type should be rejected.",
            "images": [{
                "name": "payload.svg",
                "mimeType": "image/svg+xml",
                "dataUrl": "data:image/svg+xml;base64,PHN2Zy8+",
            }],
        })
    try:
        asyncio.run(routes_sys.submit_feedback(request))
    except HTTPException as error:
        assert error.status_code == 422
    else:
        raise AssertionError("unsupported image type must be rejected")
