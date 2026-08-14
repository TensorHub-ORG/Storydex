from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, Iterable


BRIDGE_PROTOCOL_VERSION = 1
STORYDEX_COOMI_RUNTIME_VERSION = "2.1.0-storydex-desktop.1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DESKTOP_RUNTIME_ROOT = REPOSITORY_ROOT / "apps" / "desktop" / "agent-runtime"


def _storydex_coomi_home_path() -> Path:
    """Resolve the isolated Coomi home before runtime modules are imported.

    Production keeps the existing user-level default. Acceptance and packaged
    smoke runs can point a whole backend process at a temporary home without
    changing the user's active provider or copying unrelated providers.
    """

    configured = str(os.getenv("STORYDEX_COOMI_HOME") or "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path.home() / ".storydex" / ".coomi"
    )


STORYDEX_COOMI_HOME = _storydex_coomi_home_path()
STORYDEX_COOMI_CONFIG = STORYDEX_COOMI_HOME / "config" / "providers.json"
REASONING_EFFORTS = frozenset({"auto", "low", "medium", "high", "xhigh", "max"})


class CoomiBridgeError(RuntimeError):
    pass


def _normalize_reasoning_effort(value: Any) -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized not in REASONING_EFFORTS:
        raise CoomiBridgeError(f"Unsupported reasoning effort: {normalized or '<empty>'}")
    return normalized


@dataclass
class BridgeToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class BridgeLLMResponse:
    content: str
    tool_calls: list[BridgeToolCall] | None
    usage: Dict[str, Any] | None
    reasoning_content: str | None = None
    reasoning_request_plan: Dict[str, Any] | None = None
    metadata: Dict[str, Any] | None = None


def ensure_storydex_coomi_config() -> Path:
    STORYDEX_COOMI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if not STORYDEX_COOMI_CONFIG.exists():
        STORYDEX_COOMI_CONFIG.write_text(
            '{\n  "version": 1,\n  "active": "",\n  "providers": {}\n}\n',
            encoding="utf-8",
        )
    return STORYDEX_COOMI_CONFIG


def bridge_command() -> list[str]:
    configured = str(os.getenv("STORYDEX_COOMI_BRIDGE") or "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[1] / "runtime" / "storydex-coomi-bridge.exe",
        Path(__file__).resolve().parents[1] / "runtime" / "storydex-coomi-bridge",
        DESKTOP_RUNTIME_ROOT / "target" / "release" / "storydex-coomi-bridge.exe",
        DESKTOP_RUNTIME_ROOT / "target" / "release" / "storydex-coomi-bridge",
        DESKTOP_RUNTIME_ROOT / "target" / "debug" / "storydex-coomi-bridge.exe",
        DESKTOP_RUNTIME_ROOT / "target" / "debug" / "storydex-coomi-bridge",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return [str(candidate)]
    if str(os.getenv("STORYDEX_TESTING") or "").strip().lower() in {"1", "true", "yes"}:
        raise CoomiBridgeError(
            "Storydex Coomi Rust bridge must be built explicitly in test mode; "
            "implicit cargo run is disabled"
        )
    manifest = DESKTOP_RUNTIME_ROOT / "Cargo.toml"
    if manifest.is_file():
        return [
            "cargo",
            "run",
            "--quiet",
            "--manifest-path",
            str(manifest),
            "-p",
            "storydex-coomi-bridge",
            "--",
        ]
    raise CoomiBridgeError("Storydex Coomi Rust bridge is not installed")


def _base_request(action: str) -> Dict[str, Any]:
    ensure_storydex_coomi_config()
    return {
        "action": action,
        "home": str(STORYDEX_COOMI_HOME),
    }


async def _create_bridge_subprocess() -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            bridge_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPOSITORY_ROOT),
            bufsize=0,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
    except OSError as exc:
        raise CoomiBridgeError(
            f"Storydex Coomi Rust bridge could not start ({type(exc).__name__}): {exc}"
        ) from exc


def _decode_lines(output: bytes) -> list[Dict[str, Any]]:
    packets: list[Dict[str, Any]] = []
    for raw_line in output.decode("utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise CoomiBridgeError(f"Invalid JSONL from Storydex Coomi bridge: {raw_line[:240]}") from exc
        if isinstance(value, dict):
            packets.append(value)
    return packets


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    _kill_process(process)
    try:
        await asyncio.to_thread(process.wait, 5.0)
    except subprocess.TimeoutExpired:
        _kill_process(process)
        await asyncio.to_thread(process.wait)


async def request_once(payload: Dict[str, Any], *, timeout: float = 190.0) -> Dict[str, Any]:
    request = {**_base_request(str(payload.get("action") or "")), **payload}
    process = await _create_bridge_subprocess()
    encoded = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    communication = asyncio.create_task(
        asyncio.to_thread(process.communicate, encoded, timeout=timeout)
    )
    try:
        stdout, stderr = await asyncio.shield(communication)
    except subprocess.TimeoutExpired:
        _kill_process(process)
        stdout, stderr = await asyncio.to_thread(process.communicate)
        await asyncio.to_thread(process.wait)
        del stdout, stderr
        raise CoomiBridgeError(f"Storydex Coomi bridge timed out after {timeout:g}s")
    except asyncio.CancelledError:
        await _kill_and_reap(process)
        await asyncio.gather(communication, return_exceptions=True)
        raise
    except BaseException:
        await _kill_and_reap(process)
        await asyncio.gather(communication, return_exceptions=True)
        raise
    packets = _decode_lines(stdout)
    error_packet = next((packet for packet in reversed(packets) if packet.get("type") == "error"), None)
    if process.returncode != 0 or error_packet is not None:
        detail = ""
        if error_packet:
            detail = str((error_packet.get("data") or {}).get("message") or "")
        if not detail:
            detail = stderr.decode("utf-8", errors="replace").strip()
        raise CoomiBridgeError(detail or f"Storydex Coomi bridge exited with {process.returncode}")
    if not packets:
        raise CoomiBridgeError("Storydex Coomi bridge returned no packets")
    return packets[-1]


def request_status_sync(*, timeout: float = 30.0) -> Dict[str, Any]:
    """Run the read-only bridge status action from synchronous service code."""
    request = {**_base_request("status"), "action": "status"}
    process = subprocess.Popen(
        bridge_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPOSITORY_ROOT),
        bufsize=0,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
    )
    encoded = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        stdout, stderr = process.communicate(encoded, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process(process)
        stdout, stderr = process.communicate()
        del stdout, stderr
        raise CoomiBridgeError(f"Storydex Coomi bridge status timed out after {timeout:g}s") from exc
    packets = _decode_lines(stdout)
    error_packet = next((packet for packet in reversed(packets) if packet.get("type") == "error"), None)
    if process.returncode != 0 or error_packet is not None:
        detail = ""
        if error_packet:
            detail = str((error_packet.get("data") or {}).get("message") or "")
        if not detail:
            detail = stderr.decode("utf-8", errors="replace").strip()
        raise CoomiBridgeError(detail or f"Storydex Coomi bridge exited with {process.returncode}")
    if not packets:
        raise CoomiBridgeError("Storydex Coomi bridge returned no packets")
    return packets[-1]


class LiveBridgeProcess:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self._stderr = ""
        self._write_lock = asyncio.Lock()
        self._stdout_read_task: asyncio.Task[bytes] | None = None
        self._stderr_task = asyncio.create_task(self._read_stderr())

    @classmethod
    async def start(cls, payload: Dict[str, Any]) -> "LiveBridgeProcess":
        request = {**_base_request(str(payload.get("action") or "run")), **payload}
        process = await _create_bridge_subprocess()
        instance = cls(process)
        try:
            await instance.send(request)
        except BaseException:
            await instance.close()
            raise
        return instance

    async def _read_stderr(self) -> None:
        if self.process.stderr is None:
            return
        value = await asyncio.to_thread(self.process.stderr.read)
        self._stderr = value.decode("utf-8", errors="replace").strip()

    async def send(self, payload: Dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            try:
                await asyncio.to_thread(self._write_line, line.encode("utf-8"))
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise CoomiBridgeError(
                    f"Storydex Coomi bridge stdin write failed ({type(exc).__name__}): {exc}"
                ) from exc

    def _write_line(self, encoded: bytes) -> None:
        stdin = self.process.stdin
        if stdin is None or stdin.closed or self.process.poll() is not None:
            raise BrokenPipeError("bridge stdin is closed")
        stdin.write(encoded)
        stdin.flush()

    async def resolve(self, request_id: str, value: Dict[str, Any]) -> None:
        await self.send({"action": "resolve", "requestId": request_id, "value": value})

    async def cancel(self, *, steer: bool = False) -> None:
        if self.process.poll() is None:
            await self.send({"action": "steer" if steer else "cancel"})

    async def _read_stdout_line(self) -> bytes:
        if self.process.stdout is None:
            raise CoomiBridgeError("Storydex Coomi bridge stdout is unavailable")
        task = asyncio.create_task(asyncio.to_thread(self.process.stdout.readline))
        self._stdout_read_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._stdout_read_task is task:
                self._stdout_read_task = None

    async def events(self) -> AsyncIterator[Dict[str, Any]]:
        while True:
            raw_line = await self._read_stdout_line()
            if not raw_line:
                break
            packets = _decode_lines(raw_line)
            for packet in packets:
                yield packet
        return_code = await asyncio.to_thread(self.process.wait)
        await self._stderr_task
        if return_code != 0:
            raise CoomiBridgeError(self._stderr or f"Storydex Coomi bridge exited with {return_code}")

    async def close(self) -> None:
        async with self._write_lock:
            stdin = self.process.stdin
            if stdin is not None and not stdin.closed:
                try:
                    await asyncio.to_thread(stdin.close)
                except (BrokenPipeError, OSError):
                    pass
        if self.process.poll() is None:
            try:
                await asyncio.to_thread(self.process.wait, 2.0)
            except subprocess.TimeoutExpired:
                await _kill_and_reap(self.process)
        stdout_task = self._stdout_read_task
        if stdout_task is not None and not stdout_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(stdout_task), timeout=2.0)
            except asyncio.TimeoutError:
                stdout_task.cancel()
        if stdout_task is not None:
            await asyncio.gather(stdout_task, return_exceptions=True)
            if self._stdout_read_task is stdout_task:
                self._stdout_read_task = None
        if not self._stderr_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=2.0)
            except asyncio.TimeoutError:
                self._stderr_task.cancel()
        await asyncio.gather(self._stderr_task, return_exceptions=True)
        for pipe in (self.process.stdout, self.process.stderr):
            if pipe is not None and not pipe.closed:
                try:
                    pipe.close()
                except OSError:
                    pass


def _provider_document() -> Dict[str, Any]:
    path = ensure_storydex_coomi_config()
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CoomiBridgeError(f"Invalid Coomi provider config: {exc}") from exc
    if not isinstance(value, dict):
        raise CoomiBridgeError("Coomi provider config must be a JSON object")
    return value


def _provider_settings(provider_id: str | None = None, *, use_fast_model: bool = False) -> SimpleNamespace:
    document = _provider_document()
    providers = document.get("providers") if isinstance(document.get("providers"), dict) else {}
    selected = str(provider_id or document.get("active") or "").strip()
    if selected not in providers:
        raise CoomiBridgeError("No active Storydex Coomi provider is configured")
    value = providers[selected] if isinstance(providers[selected], dict) else {}
    raw_model = value.get("fast_model") if use_fast_model else value.get("model")
    model = str(raw_model or "").strip()
    if not model:
        model = str(value.get("model") or "").strip()
    return SimpleNamespace(
        id=selected,
        type=str(value.get("type") or "openai_compatible"),
        display=str(value.get("display") or selected),
        model=model,
        fast_model=str(value.get("fast_model") or ""),
        base_url=str(value.get("base_url") or ""),
        context_window=int(value.get("context_window") or 256_000),
        effective_context_window_percent=int(value.get("effective_context_window_percent") or 95),
        max_output_tokens=int(value.get("max_output_tokens") or 8192),
    )


def _wire_tools(tools: Iterable[Dict[str, Any]] | None) -> list[Dict[str, Any]]:
    output = []
    for value in tools or []:
        if not isinstance(value, dict):
            continue
        function = value.get("function") if isinstance(value.get("function"), dict) else value
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        output.append(
            {
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters") if isinstance(function.get("parameters"), dict) else {},
            }
        )
    return output


def _wire_messages(messages: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    output = []
    for value in messages:
        if not isinstance(value, dict):
            continue
        message = {
            "role": str(value.get("role") or "user"),
            "content": str(value.get("content") or ""),
        }
        if isinstance(value.get("tool_calls"), list):
            message["tool_calls"] = value["tool_calls"]
        if value.get("tool_call_id"):
            message["tool_call_id"] = str(value["tool_call_id"])
        output.append(message)
    return output


def _required_tool_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    function = value.get("function") if isinstance(value.get("function"), dict) else value
    return str(function.get("name") or "").strip() or None


class BridgeProvider:
    def __init__(
        self,
        provider_id: str | None = None,
        *,
        use_fast_model: bool = False,
        reasoning_effort: str = "auto",
    ) -> None:
        self.config = _provider_settings(provider_id, use_fast_model=use_fast_model)
        self.provider_id = self.config.id
        self.model = self.config.model
        self.use_fast_model = use_fast_model
        self.reasoning_effort = _normalize_reasoning_effort(reasoning_effort)

    def get_model_display_name(self) -> str:
        return f"{self.config.display} / {self.model}"

    @staticmethod
    def storydex_revision_budget_policy() -> Dict[str, Any]:
        return {
            "name": "storydex_coomi_rust_http",
            "deadlineRatio": 1.25,
            "deadlineMinimumSeconds": 30,
            "deadlineMaximumSeconds": 60,
        }

    @staticmethod
    def storydex_intent_request_options() -> Dict[str, Any]:
        """Bound the one-shot Rust bridge metadata request.

        Direct SDK providers use JSON mode with thinking disabled and finish
        reliably within 384 output tokens.  The bridge protocol cannot yet
        forward those provider-native fields; the pinned DeepSeek route needs
        a larger cap to finish the same compact JSON object instead of
        returning an empty, budget-exhausted completion.
        """

        return {
            "max_output_tokens": 1536,
            "reasoning_effort": "low",
        }

    async def chat(
        self,
        messages: list[Dict[str, Any]],
        tools: list[Dict[str, Any]] | None = None,
        max_completion_tokens: int | None = None,
        max_output_tokens: int | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> BridgeLLMResponse:
        output_token_limit = int(
            max_completion_tokens
            or kwargs.get("max_tokens")
            or max_output_tokens
            or 0
        )
        required_tool = _required_tool_name(tool_choice)
        reasoning_effort = _normalize_reasoning_effort(
            kwargs.get("reasoning_effort", self.reasoning_effort)
        )
        if tool_choice == "required" and len(tools or []) == 1:
            required_tool = str(
                ((tools or [{}])[0].get("function") or {}).get("name")
                or (tools or [{}])[0].get("name")
                or ""
            ).strip() or None
        packet = await request_once(
            {
                **_base_request("complete"),
                "action": "complete",
                "provider": self.provider_id,
                "useFastModel": self.use_fast_model,
                "messages": _wire_messages(messages),
                "tools": _wire_tools(tools),
                "requiredTool": required_tool,
                "maxOutputTokens": output_token_limit or None,
                "reasoningEffort": reasoning_effort,
            }
        )
        if packet.get("type") != "completion":
            raise CoomiBridgeError(f"Unexpected completion packet: {packet.get('type')}")
        data = packet.get("data") if isinstance(packet.get("data"), dict) else {}
        tool_calls = []
        for value in data.get("toolCalls") or []:
            if not isinstance(value, dict):
                continue
            arguments = value.get("arguments") if isinstance(value.get("arguments"), dict) else {}
            tool_calls.append(
                BridgeToolCall(
                    id=str(value.get("id") or ""),
                    name=str(value.get("name") or ""),
                    arguments=arguments,
                )
            )
        raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        input_tokens = int(raw_usage.get("input_tokens") or 0)
        output_tokens = int(raw_usage.get("output_tokens") or 0)
        usage = {
            **raw_usage,
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        return BridgeLLMResponse(
            content=str(data.get("content") or ""),
            tool_calls=tool_calls or None,
            usage=usage,
            reasoning_request_plan=(
                data.get("reasoningRequestPlan")
                if isinstance(data.get("reasoningRequestPlan"), dict)
                else None
            ),
            metadata=(
                data.get("metadata")
                if isinstance(data.get("metadata"), dict)
                else None
            ),
        )

    async def chat_stream(self, messages: list[Dict[str, Any]], **kwargs: Any) -> AsyncIterator[str]:
        response = await self.chat(messages, None, **kwargs)
        if response.content:
            yield response.content

    async def chat_stream_with_tools(
        self,
        messages: list[Dict[str, Any]],
        tools: list[Dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        response = await self.chat(messages, tools, **kwargs)
        if response.content:
            yield {"type": "content", "data": response.content}
        for call in response.tool_calls or []:
            yield {
                "type": "tool_call",
                "data": {"id": call.id, "name": call.name, "arguments": call.arguments},
            }
        yield {"type": "usage", "data": response.usage or {}}


def get_bridge_provider(
    provider_id: str | None = None,
    *,
    fast: bool = False,
    reasoning_effort: str = "auto",
) -> BridgeProvider:
    return BridgeProvider(
        provider_id,
        use_fast_model=fast,
        reasoning_effort=reasoning_effort,
    )
