from __future__ import annotations

import re
import time
from typing import Tuple

from lib.persist import load_json_file, save_json_file


def kb_tokens(text: str) -> set[str]:
    t = (text or "").lower()
    tokens: set[str] = set()
    for w in re.findall(r"[a-z0-9_]{3,}", t):
        tokens.add(w)
    for w in re.findall(r"[\u4e00-\u9fff]{2,}", text or ""):
        tokens.add(w)
    return tokens


def kb_score_item(query_tokens: set[str], item: dict) -> int:
    if not query_tokens:
        return 0
    q = str(item.get("question", "") or "")
    findings = item.get("key_findings") or []
    if not isinstance(findings, list):
        findings = []
    blob = q + "\n" + "\n".join(str(x) for x in findings[:20])
    blob_low = blob.lower()
    score = 0
    for tok in query_tokens:
        if not tok:
            continue
        if tok.lower() in blob_low:
            score += 3
    return score


def kb_build_context(
        question: str,
        kb_items: list[dict],
        max_items: int = 3,
        max_chars: int = 1400,
) -> tuple[str, list[dict]]:
    if not kb_items:
        return "", []
    q_tokens = kb_tokens(question)
    scored: list[tuple[int, dict]] = []
    for it in kb_items:
        if not isinstance(it, dict):
            continue
        s = kb_score_item(q_tokens, it)
        if s > 0:
            scored.append((s, it))
    if not scored:
        return "", []
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [it for _, it in scored[:max_items]]
    lines: list[str] = ["【知识库参考（仅供提示，结论需用工具再次取证）】"]
    used = len(lines[0]) + 1
    for it in picked:
        kid = str(it.get("id", "") or "").strip()
        q = str(it.get("question", "") or "").strip()
        if kid or q:
            head = f"- {kid} {q}".strip()
            if used + len(head) + 1 > max_chars:
                break
            lines.append(head)
            used += len(head) + 1
        findings = it.get("key_findings") or []
        if isinstance(findings, list) and findings:
            for f in findings[:6]:
                s = str(f).strip()
                if not s:
                    continue
                row = f"  * {s}"
                if used + len(row) + 1 > max_chars:
                    break
                lines.append(row)
                used += len(row) + 1
        md = str(it.get("final_markdown", "") or "").strip()
        if md:
            excerpt = re.sub(r"\s+", " ", md)[:220]
            row = f"  * 摘要: {excerpt}..."
            if used + len(row) + 1 <= max_chars:
                lines.append(row)
                used += len(row) + 1
    return "\n".join(lines).strip(), picked


def contains_dsml_markup(text: str) -> bool:
    t = text or ""
    return (
            ("<｜DSML｜" in t)
            or ("<|DSML|" in t)
            or ("</｜DSML｜" in t)
            or ("</|DSML|" in t)
            or (re.search(r"<[|｜]DSML[|｜](invoke|parameter)\b", t, flags=re.IGNORECASE) is not None)
    )


def extract_markdown_section(md: str, heading: str) -> str:
    text = (md or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""
    pat = re.compile(
        rf"(?m)^\s*##\s+{re.escape(heading)}\s*$\n(.*?)(?=^\s*##\s+|\Z)",
        flags=re.DOTALL,
    )
    m = pat.search(text)
    return (m.group(1) if m else "").strip()


def extract_key_findings(md: str, limit: int = 12) -> list[str]:
    sec = extract_markdown_section(md, "关键发现")
    if not sec:
        return []
    out: list[str] = []
    for raw in sec.split("\n"):
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+\.\s+", "", line)
        line = line.strip()
        if not line:
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def append_kb_item(kb_path: str, item: dict) -> None:
    kb = load_json_file(kb_path, {"items": []})
    if not isinstance(kb, dict):
        kb = {"items": []}
    items = kb.get("items")
    if not isinstance(items, list):
        items = []
        kb["items"] = items
    items.append(item)
    save_json_file(kb_path, kb)


def build_kb_item(question: str, final_markdown: str) -> dict:
    now = int(time.time())
    return {
        "id": f"kb_{now}",
        "created_at": now,
        "question": question,
        "key_findings": extract_key_findings(final_markdown),
        "final_markdown": final_markdown,
    }


def load_kb_items(kb_path: str) -> Tuple[list[dict], int]:
    kb = load_json_file(kb_path, {"items": []})
    kb_count = len(kb.get("items", [])) if isinstance(kb, dict) and isinstance(kb.get("items"), list) else 0
    kb_items: list[dict] = []
    if isinstance(kb, dict) and isinstance(kb.get("items"), list):
        kb_items = [x for x in kb.get("items", []) if isinstance(x, dict)]
    return kb_items, kb_count
