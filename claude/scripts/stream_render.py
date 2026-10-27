#!/usr/bin/env python3
"""
stream_render.py — reads Claude Code's `--output-format stream-json` event
stream from stdin and renders a readable, live view to stdout: which model
is actually running (aliases like "opus" resolve to a specific dated model
— this is the only place that resolution is visible), assistant text as it
arrives, which tools are being called, and a final cost/model-usage
summary.

Used by cc-run so a headless run isn't silent and so you can see exactly
which model version ran, not just which alias you asked for. This does not
cost anything extra — it's the same underlying run, just parsed instead of
printed as plain text.

Usage: claude ... --output-format stream-json --verbose | python3 stream_render.py
"""

import json
import sys

# On Windows, both stdin and stdout follow the system codepage (e.g. cp1254)
# unless told otherwise — but `claude --output-format stream-json` always
# writes genuine UTF-8. Only reconfiguring stdout (as this used to do) left
# stdin being decoded with the wrong codepage: multi-byte UTF-8 characters
# (ş, ğ, ü, emoji, ...) got misread as several wrong single-byte characters
# BEFORE this script ever saw them, so re-encoding correctly on the way out
# just faithfully printed that already-corrupted text (mojibake that no
# console-encoding fix downstream could undo). Reconfigure both ends to
# UTF-8; no-op on platforms where it already is. Requires Python 3.7+
# (already required elsewhere in this project).
try:
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass
# errors="replace" on the way out: a stray/invalid character (e.g. a lone
# UTF-16 surrogate from a malformed upstream event) must not be able to
# crash this script and cut off visibility into the rest of a run that may
# still be working fine — print a replacement character instead of raising
# UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    model_reported = False
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue

        etype = event.get("type")

        if etype == "system" and event.get("subtype") == "init":
            if not model_reported:
                print(f"🧠 [cc-run] model: {event.get('model', 'unknown')}", flush=True)
                model_reported = True

        elif etype == "assistant":
            content = ((event.get("message") or {}).get("content")) or []
            for block in content:
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    print(block["text"], flush=True)
                elif btype == "tool_use":
                    print(f"   [tool] {block.get('name', '?')}", flush=True)

        elif etype == "result":
            # `claude` can exit 0 even when the round didn't actually
            # finish the task (e.g. subtype "error_max_turns" or
            # "error_during_execution") — cc-run's retry logic only looks
            # at the process exit code, so it can't tell a real success
            # apart from this either; it just logs "ended on its own" and
            # stops the whole unattended run. Without this line, this
            # script would print the same cost summary either way, leaving
            # no visible sign anything went wrong.
            is_error = bool(event.get("is_error"))
            subtype = event.get("subtype")
            if is_error or (subtype and subtype != "success"):
                print(
                    f"⚠️  [cc-run] round ended abnormally (subtype={subtype or 'unknown'}) "
                    "— not a quota stop; check the output above",
                    flush=True,
                )
            usage = event.get("modelUsage") or {}
            models = ", ".join(usage.keys()) if usage else event.get("model", "unknown")
            cost = event.get("total_cost_usd")
            if cost is not None:
                print(f"💲 [cc-run] model(s) used: {models} — cost: ${cost:.4f}", flush=True)
            else:
                print(f"💲 [cc-run] model(s) used: {models}", flush=True)


if __name__ == "__main__":
    main()
