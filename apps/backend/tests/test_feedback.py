from __future__ import annotations

import base64
import asyncio
from types import SimpleNamespace

from fastapi import HTTPException

from api import routes_sys
from services import tool_failure_feedback_service


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
    assert captured["platform"] == "windows"
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


def test_tool_failure_analysis_is_local_bounded_and_redacted(monkeypatch) -> None:
    calls = []

    class FakeProvider:
        async def chat(self, messages, tools=None, **kwargs):
            calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
            return SimpleNamespace(content="确认路径 C:\\private\\chapter.txt，参考 https://private.invalid/fix")

    monkeypatch.setattr(
        tool_failure_feedback_service,
        "get_bridge_provider",
        lambda provider_id, **kwargs: FakeProvider(),
    )
    request = routes_sys.ToolFailureAnalysisRequest(**{
        "providerId": "provider-1",
        "trace": [
            {
                "sequence": index,
                "tool": "read_file",
                "status": "error",
                "argumentShape": {"path": "C:\\private\\chapter.txt", "apiKey": "secret-value"},
                "elapsedMs": 120,
                "category": "not_found",
                "errorSummary": "failed at C:\\private\\chapter.txt using sk-private-token",
            }
            for index in range(1, 4)
        ],
    })

    response = asyncio.run(routes_sys.analyze_tool_failure_feedback(request))

    assert response.data["failureCount"] == 3
    assert response.data["redactionVersion"] == "storydex-tool-trace-v1"
    assert "C:\\private" not in response.data["analysis"]
    assert "private.invalid" not in response.data["analysis"]
    assert "C:\\private" not in response.data["programEvidence"]
    assert "secret-value" not in response.data["programEvidence"]
    assert len(calls) == 1
    assert calls[0]["tools"] == []
    assert calls[0]["kwargs"]["reasoning_effort"] == "low"
    serialized_messages = str(calls[0]["messages"])
    assert "C:\\private" not in serialized_messages
    assert "secret-value" not in serialized_messages


def test_tool_failure_analysis_requires_three_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        tool_failure_feedback_service,
        "get_bridge_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider must not be called")),
    )
    request = routes_sys.ToolFailureAnalysisRequest(**{
        "trace": [
            {"sequence": 1, "tool": "read_file", "status": "error", "argumentShape": {}},
            {"sequence": 2, "tool": "search", "status": "error", "argumentShape": {}},
            {"sequence": 3, "tool": "read_file", "status": "success", "argumentShape": {}},
        ],
    })

    try:
        asyncio.run(routes_sys.analyze_tool_failure_feedback(request))
    except HTTPException as error:
        assert error.status_code == 422
    else:
        raise AssertionError("fewer than three failures must be rejected")
