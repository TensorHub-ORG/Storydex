from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List

from core.bounded_text_io import read_text_limited, read_text_preview


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]+")


def tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    for raw in _TOKEN_RE.findall(str(text or "").lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw):
            if len(raw) <= 3:
                tokens.append(raw)
            for size in (2, 3):
                if len(raw) >= size:
                    tokens.extend(raw[index:index + size] for index in range(len(raw) - size + 1))
        else:
            tokens.append(raw)
    seen: set[str] = set()
    result: List[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def bm25_search(query: str, file_paths: List[Path], workspace_root: Path, limit: int = 5) -> List[Dict[str, Any]]:
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    docs: List[Dict[str, Any]] = []
    doc_freq: Dict[str, int] = {}
    for path in file_paths:
        if not path.is_file():
            continue
        try:
            content = read_text_limited(path, 12000, preserve_tail=True).text
        except Exception:
            continue
        tokens = tokenize(content)
        if not tokens:
            continue
        frequencies: Dict[str, int] = {}
        for token in tokens:
            frequencies[token] = frequencies.get(token, 0) + 1
        for token in frequencies:
            doc_freq[token] = doc_freq.get(token, 0) + 1
        try:
            doc_id = path.relative_to(workspace_root).as_posix()
        except ValueError:
            doc_id = path.as_posix()
        docs.append({"doc_id": doc_id, "path": path, "tokens": tokens, "frequencies": frequencies})

    if not docs:
        return []
    avg_len = sum(len(doc["tokens"]) for doc in docs) / max(1, len(docs))
    results: List[Dict[str, Any]] = []
    for doc in docs:
        score = 0.0
        doc_len = len(doc["tokens"])
        for token in query_tokens:
            tf = doc["frequencies"].get(token, 0)
            if tf <= 0:
                continue
            df = doc_freq.get(token, 0)
            idf = math.log((len(docs) - df + 0.5) / (df + 0.5) + 1.0)
            score += idf * (tf * 2.5) / (tf + 1.5 * (1 - 0.75 + 0.75 * doc_len / max(avg_len, 1.0)))
        if score <= 0:
            continue
        snippet = ""
        try:
            snippet = _build_snippet(read_text_preview(doc["path"], max_chars=4000), query_tokens)
        except Exception:
            pass
        results.append(
            {
                "doc_id": doc["doc_id"],
                "score": round(score, 4),
                "metadata": {"snippet": snippet},
            }
        )
    results.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("doc_id") or "")))
    return results[: max(1, min(limit, 20))]


def hybrid_search(query: str, file_paths: List[Path], workspace_root: Path, limit: int = 5) -> List[Dict[str, Any]]:
    results = bm25_search(query=query, file_paths=file_paths, workspace_root=workspace_root, limit=limit)
    for result in results:
        result["engine"] = "storydex_bm25"
    return results


def _build_snippet(content: str, keywords: List[str]) -> str:
    lowered = content.lower()
    first_index = -1
    for token in keywords:
        first_index = lowered.find(token.lower())
        if first_index >= 0:
            break
    if first_index < 0:
        return " ".join(content[:220].split())
    start = max(0, first_index - 100)
    end = min(len(content), first_index + 220)
    return " ".join(content[start:end].split())
