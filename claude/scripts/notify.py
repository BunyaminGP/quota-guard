#!/usr/bin/env python3
"""
notify.py — optional push notification for cc-quota-guard's key moments:
a SOFT/HARD quota threshold stopping Claude, cc-run resuming after a sleep,
and the task finishing (or cc-run giving up after retries).

Tested against ntfy.sh (https://ntfy.sh/<topic>, POST body = message text,
`Title` header = a short ASCII tag) — see README's Notifications section
for setup. Any endpoint that accepts a raw-text HTTP POST body works the
same way. This does NOT attempt to speak Slack's or Discord's JSON webhook
shape (`{"text": ...}` / `{"content": ...}`) — that's untested and not
implemented; document the limitation rather than claim compatibility that
was never verified.

FAIL-OPEN, same as the rest of this project: no URL configured, a bad URL,
no network, or a timeout must NEVER raise into the caller or block a hook/
cc-run round. Short timeout so a dead endpoint can't hang anything.

Privacy: this module sends whatever message string it's given — it is the
CALLER's job to only pass already-privacy-safe text. In practice, every
call site in this project reuses text that was already going to be shown
to the human anyway (the hook's systemMessage, cc-run's own status lines)
— never task content or file paths.

URL resolution mirrors hard_abort_enabled's security pattern (see
quota_gate.py's docstring): CC_NOTIFY_URL env var, or the plugin's own
userConfig screen (CLAUDE_PLUGIN_OPTION_NOTIFY_URL, only ever set for hook
invocations — never seen by cc-run itself, same asymmetry as CC_LANG).
Deliberately NEVER read from `.cc-quota/config.json`: unlike the
percentage thresholds, a notification URL is an exfiltration channel — a
cloned/untrusted repo shipping a config.json with its own URL could
otherwise silently siphon your session status (quota %, reset times, when
you're active) to a server you don't control. Only something YOU set
yourself may configure this.

Library usage (quota_gate.py):
    import notify
    notify.notify("some already-safe status line", title="cc-quota-guard")

CLI usage (bin/cc-run, which has no urllib access of its own):
    python3 notify.py <message> [title]
    -> always exits 0; failures are swallowed, never reported as an error,
       so a bad/missing CC_NOTIFY_URL can never break the caller's flow.
"""

import os
import sys
import urllib.request

TIMEOUT = float(os.environ.get("CC_NOTIFY_TIMEOUT", "5"))


def _shared_url():
    """quota-guard's per-user settings file (~/.quota-guard/settings.json),
    shared by the Claude Code, Codex and Antigravity hooks. Home directory
    only, never the project. QUOTA_GUARD_CONFIG overrides the path (tests)."""
    import json
    path = os.environ.get("QUOTA_GUARD_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".quota-guard", "settings.json")
    try:
        if os.path.islink(path):
            return None
        with open(path, encoding="utf-8") as f:
            value = json.load(f).get("notify_url")
        return value if isinstance(value, str) and value.startswith(("https://", "http://")) else None
    except Exception:
        return None


def _resolve_url():
    return (
        os.environ.get("CC_NOTIFY_URL")
        or os.environ.get("CLAUDE_PLUGIN_OPTION_NOTIFY_URL")
        or _shared_url()
        or None
    )


def notify(message, title=None, url=None):
    """
    Best-effort push notification. Returns True if the POST was accepted
    (2xx), False for every other outcome (not configured, network error,
    timeout, non-2xx) — callers should treat the return value as
    informational only, never as something to act on, since this must
    never become a reason to change hook/cc-run behavior.
    """
    url = url or _resolve_url()
    if not url:
        return False
    try:
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if title:
            # Titles are only ever short ASCII literals set by call sites
            # in this project (e.g. "cc-quota-guard") — never translated,
            # never derived from user/task text — so encoding a header
            # value here is always safe. HTTP headers are historically
            # latin-1-only; message content (the body) has no such
            # restriction and is sent as UTF-8 below.
            headers["Title"] = title
        req = urllib.request.Request(
            url, data=message.encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False  # fail-open: a notification failure must never break the caller


def _cli():
    if len(sys.argv) < 2:
        print("usage: notify.py <message> [title]", file=sys.stderr)
        sys.exit(0)  # still exit 0 — a caller shelling out to this must never see a failure here
    message = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else None
    notify(message, title=title)
    sys.exit(0)


if __name__ == "__main__":
    _cli()
