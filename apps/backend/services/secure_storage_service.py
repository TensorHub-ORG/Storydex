from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict


class SecureStorageError(RuntimeError):
    pass


class SecureStorageService:
    def __init__(self, *, root: Path) -> None:
        self.root = Path(root)

    def encrypt_json(self, payload: Dict[str, Any], *, user_id: str) -> Dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise SecureStorageError("user_id_required")

        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if os.name == "nt":
            encrypted = _dpapi_protect(raw, entropy=normalized_user_id.encode("utf-8"))
            scheme = "dpapi-v1"
        else:
            encrypted = self._fallback_encrypt(raw, user_id=normalized_user_id)
            scheme = "local-secret-v1"

        return {
            "version": 1,
            "scheme": scheme,
            "userId": normalized_user_id,
            "ciphertext": base64.b64encode(encrypted).decode("ascii"),
        }

    def decrypt_json(self, payload: Dict[str, Any], *, user_id: str) -> Dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise SecureStorageError("user_id_required")

        scheme = str(payload.get("scheme") or "").strip().lower()
        encrypted_b64 = str(payload.get("ciphertext") or "").strip()
        if not encrypted_b64:
            raise SecureStorageError("ciphertext_required")

        try:
            encrypted = base64.b64decode(encrypted_b64.encode("ascii"))
        except Exception as exc:  # pragma: no cover - defensive guard
            raise SecureStorageError("ciphertext_invalid") from exc

        if scheme == "dpapi-v1":
            raw = _dpapi_unprotect(encrypted, entropy=normalized_user_id.encode("utf-8"))
        elif scheme == "local-secret-v1":
            raw = self._fallback_decrypt(encrypted, user_id=normalized_user_id)
        else:
            raise SecureStorageError(f"unsupported_scheme:{scheme or 'unknown'}")

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # pragma: no cover - defensive guard
            raise SecureStorageError("payload_decode_failed") from exc
        if not isinstance(decoded, dict):
            raise SecureStorageError("payload_invalid")
        return decoded

    def _fallback_encrypt(self, raw: bytes, *, user_id: str) -> bytes:
        key = self._fallback_key(user_id=user_id)
        nonce = secrets.token_bytes(16)
        ciphertext = _xor_bytes(raw, _keystream(key=key, nonce=nonce, length=len(raw)))
        mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
        return nonce + ciphertext + mac

    def _fallback_decrypt(self, encrypted: bytes, *, user_id: str) -> bytes:
        if len(encrypted) < 48:
            raise SecureStorageError("ciphertext_too_short")
        key = self._fallback_key(user_id=user_id)
        nonce = encrypted[:16]
        mac = encrypted[-32:]
        ciphertext = encrypted[16:-32]
        expected = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise SecureStorageError("ciphertext_mac_invalid")
        return _xor_bytes(ciphertext, _keystream(key=key, nonce=nonce, length=len(ciphertext)))

    def _fallback_key(self, *, user_id: str) -> bytes:
        secret_path = self.root / "auth" / "local-secret.bin"
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        if not secret_path.exists():
            secret_path.write_bytes(secrets.token_bytes(32))
        secret = secret_path.read_bytes()
        return hmac.new(secret, str(user_id).encode("utf-8"), hashlib.sha256).digest()


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_from_bytes(raw: bytes) -> tuple[_DATA_BLOB, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(raw)
    blob = _DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _bytes_from_blob(blob: _DATA_BLOB) -> bytes:
    if not blob.cbData:
        return b""
    pointer = ctypes.cast(blob.pbData, ctypes.POINTER(ctypes.c_ubyte))
    return bytes(pointer[index] for index in range(int(blob.cbData)))


def _dpapi_protect(raw: bytes, *, entropy: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buffer = _blob_from_bytes(raw)
    entropy_blob = None
    entropy_buffer = None
    if entropy:
        entropy_blob, entropy_buffer = _blob_from_bytes(entropy)
    out_blob = _DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "Storydex",
        ctypes.byref(entropy_blob) if entropy_blob is not None else None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise SecureStorageError("dpapi_protect_failed")
    try:
        return _bytes_from_blob(out_blob)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)
        del in_buffer
        del entropy_buffer


def _dpapi_unprotect(raw: bytes, *, entropy: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buffer = _blob_from_bytes(raw)
    entropy_blob = None
    entropy_buffer = None
    if entropy:
        entropy_blob, entropy_buffer = _blob_from_bytes(entropy)
    out_blob = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob) if entropy_blob is not None else None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise SecureStorageError("dpapi_unprotect_failed")
    try:
        return _bytes_from_blob(out_blob)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)
        del in_buffer
        del entropy_buffer


def _keystream(*, key: bytes, nonce: bytes, length: int) -> bytes:
    chunks: list[bytes] = []
    counter = 0
    while sum(len(chunk) for chunk in chunks) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        chunks.append(block)
        counter += 1
    return b"".join(chunks)[:length]


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))
