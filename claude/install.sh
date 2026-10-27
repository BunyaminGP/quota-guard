#!/usr/bin/env bash
# install.sh — MANUAL fallback installer for Claude Code versions that
# don't have the plugin system yet. If your `claude` supports `/plugin`,
# prefer that instead:
#
#   /plugin marketplace add BunyaminGP/cc-quota-guard
#   /plugin install cc-quota-guard
#
# This script copies the tool into ~/.claude/cc-quota-guard/ and merges the
# PostToolUse / UserPromptSubmit / Stop / StopFailure hooks into
# ~/.claude/settings.json (idempotent, via python3).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude/cc-quota-guard"
SETTINGS="$HOME/.claude/settings.json"

command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }

echo "→ Copying files to: $DEST"
mkdir -p "$DEST/scripts" "$DEST/bin" "$DEST/templates" "$DEST/locales"
cp "$SRC/scripts/quota_gate.py"    "$DEST/scripts/"
cp "$SRC/scripts/usage.py"         "$DEST/scripts/"
cp "$SRC/scripts/i18n.py"          "$DEST/scripts/"
cp "$SRC/scripts/notify.py"        "$DEST/scripts/"
cp "$SRC/scripts/chat_resume.py"   "$DEST/scripts/"
cp "$SRC/../common/resume_core.py"  "$DEST/scripts/"
cp "$SRC/scripts/stream_render.py" "$DEST/scripts/" 2>/dev/null || true
cp "$SRC/locales/"*.json           "$DEST/locales/"
cp "$SRC/bin/cc-run"               "$DEST/bin/"
cp "$SRC/templates/progress.md"    "$DEST/templates/" 2>/dev/null || true
chmod +x "$DEST/bin/cc-run" "$DEST/scripts/quota_gate.py" "$DEST/scripts/usage.py" "$DEST/scripts/i18n.py" "$DEST/scripts/notify.py"

HOOK_SCRIPT="$DEST/scripts/quota_gate.py"

echo "→ Updating settings.json: $SETTINGS"
python3 - "$SETTINGS" "$HOOK_SCRIPT" <<'PY'
import json, os, sys
settings_path, hook_script = sys.argv[1], sys.argv[2]
os.makedirs(os.path.dirname(settings_path), exist_ok=True)
try:
    with open(settings_path) as f:
        cfg = json.load(f)
except Exception:
    cfg = {}

hooks = cfg.setdefault("hooks", {})
ptu = hooks.setdefault("PostToolUse", [])

# A single "*" matcher (every tool call) is all that's needed: the hook
# script itself distinguishes TodoWrite calls internally via tool_name to
# record todo state + check the SOFT threshold, and checks the HARD
# threshold on every call regardless of which tool it was (so a quota
# spike in the middle of an item is caught promptly). An older install may
# still have a separate "TodoWrite" matcher entry from before this hook
# script started dispatching internally — that's now redundant (it would
# just make the hook run twice on every TodoWrite call) and is removed
# below if present.
WANTED_MATCHERS = ["*"]
LEGACY_MATCHERS = ["TodoWrite"]

def matches_us(entry):
    return any(h.get("command") == "python3" and h.get("args") == [hook_script] for h in entry.get("hooks", []))

def has_entry(matcher):
    return any(entry.get("matcher") == matcher and matches_us(entry) for entry in ptu)

added = []
for matcher in WANTED_MATCHERS:
    if not has_entry(matcher):
        ptu.append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": "python3", "args": [hook_script], "timeout": 20}],
        })
        added.append(matcher)

removed = []
for entry in list(ptu):
    if entry.get("matcher") in LEGACY_MATCHERS and matches_us(entry):
        ptu.remove(entry)
        removed.append(entry.get("matcher"))

# Chat auto-resume: the same script also handles these events (the wake-up
# prompt, turn end, and a real rate-limit error). None = no matcher.
for event, matcher in (("UserPromptSubmit", None), ("Stop", None), ("StopFailure", "rate_limit")):
    entries = hooks.setdefault(event, [])
    if not any(e.get("matcher") == matcher and matches_us(e) for e in entries):
        entry = {"hooks": [{"type": "command", "command": "python3", "args": [hook_script], "timeout": 20}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        entries.append(entry)
        added.append(event)

if added or removed:
    with open(settings_path, "w") as f:
        json.dump(cfg, f, indent=2)
    parts = []
    if added:
        parts.append(f"added: {', '.join(added)}")
    if removed:
        parts.append(f"removed redundant: {', '.join(removed)}")
    print("   " + "; ".join(parts))
else:
    print("   Hooks already up to date, nothing changed.")
PY

# Optional PATH shortcut for cc-run (only if ~/.local/bin exists)
if [[ -d "$HOME/.local/bin" ]]; then
  ln -sf "$DEST/bin/cc-run" "$HOME/.local/bin/cc-run"
  echo "→ Linked 'cc-run' into ~/.local/bin."
else
  echo "→ Run it directly: $DEST/bin/cc-run"
fi

echo
echo "✅ Install complete."
echo
echo "By default, the HARD threshold only forces a clean stop — nothing is"
echo "reverted automatically. To opt into auto-revert (git stash) of the"
echo "in-progress item, pass --enable-hard-abort to cc-run, or set"
echo "CC_HARD_ABORT=1 / .cc-quota/config.json { \"hard_abort_enabled\": true }."
echo "Read the README before turning this on."
echo
echo "Verify:"
echo "  1) Is usage readable:     python3 $DEST/scripts/usage.py"
echo "  2) API schema (if needed): python3 $DEST/scripts/usage.py --probe"
echo "  3) Set thresholds (e.g.):  cc-run --threshold 80 --session-hard 95 --weekly-hard 98 \"task...\""
echo
echo "Note: to upgrade an older install (registered on 'TodoWrite' and '*'"
echo "      separately, or missing '*' altogether), just re-run this script —"
echo "      it adds the '*' matcher if missing and removes the now-redundant"
echo "      'TodoWrite' one."
