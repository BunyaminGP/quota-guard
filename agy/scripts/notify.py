#!/usr/bin/env python3
"""Optional plain-text webhook notifications for agy-quota-guard.

Compatible with ntfy.sh and plain-text HTTP POST endpoints.
Reads target URL ONLY from user environment variables (`AGY_NOTIFY_URL` or
`AGY_PLUGIN_OPTION_NOTIFY_URL`) — never from `.agy-quota/config.json` to prevent
untrusted repository exfiltration. Sends status-only messages (never task content,
file paths, or secrets). Fail-open with a 5-second timeout.
"""
from __future__ import annotations

import os
import sys
import urllib.request

TIMEOUT = 5
PREFIX = "[agy-quota-guard]"


def resolve_url() -> str | None:
    for var in ("AGY_NOTIFY_URL", "AGY_PLUGIN_OPTION_NOTIFY_URL"):
        val = os.environ.get(var, "").strip()
        if val:
            # Only plain HTTP(S) webhooks; never file:// or other urllib handlers.
            return val if val.lower().startswith(("https://", "http://")) else None
    # quota-guard's per-user settings (~/.quota-guard/settings.json), shared by
    # the Claude Code, Codex and Antigravity hooks; home directory only.
    try:
        path = os.environ.get("QUOTA_GUARD_CONFIG") or os.path.join(os.path.expanduser("~"), ".quota-guard", "settings.json")
        if not os.path.islink(path):
            import json
            with open(path, encoding="utf-8") as f:
                val = str(json.load(f).get("notify_url") or "").strip()
            if val.lower().startswith(("https://", "http://")):
                return val
    except Exception:
        pass
    return None


def send(message: str, title: str | None = None, priority: str | None = None, tags: str | None = None) -> bool:
    url = resolve_url()
    if not url or not message:
        return False
    body = f"{PREFIX} {message}" if not message.startswith(PREFIX) else message
    headers = {"Content-Type": "text/plain; charset=utf-8", "User-Agent": "agy-quota-guard/1.0"}
    if title:
        headers["Title"] = title
    if priority:
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = tags
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def main() -> int:
    msg_text = " ".join(sys.argv[1:]).strip()
    if not msg_text and not sys.stdin.isatty():
        msg_text = sys.stdin.read().strip()
    if msg_text:
        send(msg_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
