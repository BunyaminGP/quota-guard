#!/usr/bin/env python3
"""Stream-JSON event parser and formatter for Antigravity CLI (agy)."""
from __future__ import annotations

import json
import sys


def extract_conversation_id(ev: dict) -> str | None:
    if not isinstance(ev, dict):
        return None
    for key in ("conversation_id", "conversationId", "session_id", "sessionId"):
        val = ev.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    res = ev.get("result")
    if isinstance(res, dict):
        for key in ("conversation_id", "conversationId", "session_id", "sessionId"):
            val = res.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def render_event(ev: dict) -> list[str]:
    if not isinstance(ev, dict):
        return []
    lines: list[str] = []
    etype = ev.get("event") or ev.get("type") or ""

    if etype in ("init", "system"):
        cid = extract_conversation_id(ev) or "?"
        model = ev.get("model") or ev.get("modelName") or "auto"
        lines.append(f"  [init] model={model} conversation={cid[:12]}")

    elif etype == "assistant":
        msg = ev.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else ev.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    txt = (block.get("text") or "").strip()
                    if txt:
                        first = txt.splitlines()[0][:140]
                        lines.append(f"  💬 {first}")
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    lines.append(f"  🔧 {name}")
        elif isinstance(content, str) and content.strip():
            lines.append(f"  💬 {content.strip().splitlines()[0][:140]}")

    elif etype in ("tool_call", "tool_use"):
        tc = ev.get("toolCall") or ev
        name = tc.get("name") if isinstance(tc, dict) else "?"
        lines.append(f"  🔧 {name}")

    elif etype == "result":
        res = ev.get("result") if isinstance(ev.get("result"), dict) else ev
        status = res.get("status") or ("ERROR" if res.get("is_error") else "SUCCESS")
        turns = res.get("num_turns", "?")
        dur = res.get("duration_seconds")
        resp = (res.get("response") or "").strip()
        if resp:
            first = resp.splitlines()[0][:140]
            lines.append(f"  💬 {first}")
        dur_str = f" duration={dur}s" if dur is not None else ""
        lines.append(f"  [done] status={status} turns={turns}{dur_str}")

    return lines


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        for line in render_event(ev):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
