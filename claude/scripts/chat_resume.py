#!/usr/bin/env python3
"""
chat_resume.py — automatic resume for ordinary Claude Code CHAT sessions
(no cc-run wrapper needed).

How it works:
  1. A threshold stop (quota_gate.py) also tells Claude to schedule its own
     wake-up with Claude Code's built-in CronCreate tool: one one-shot cron
     at the quota reset time (+ a small buffer), carrying RESUME_PROMPT.
  2. The Stop hook checks that the wake-up really exists (`session_crons`)
     and nudges Claude once if it doesn't.
  3. When the cron fires, Claude Code submits RESUME_PROMPT as a new turn.
     The UserPromptSubmit hook sees RESUME_TAG, re-reads usage and sends
     the "quota reset" notification.
  4. The first tool call of that turn sends "working again"; the end of the
     turn (Stop hook) sends "done".

The cron lives inside the running Claude Code session, so the window has to
stay open until the reset. cc-run runs set CC_QUOTA_RUNNER=1, which turns
all of this off: cc-run already sleeps and resumes on its own.

This module holds the pure helpers (planning, prompt text, state files);
the hook-event handlers live in quota_gate.py next to the threshold logic.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# Plugin layout: <root>/claude/scripts -> <root>/common. Manual install
# (install.sh) copies resume_core.py next to this file instead.
for _path in (_HERE, os.path.normpath(os.path.join(_HERE, "..", "..", "common"))):
    if _path not in sys.path:
        sys.path.append(_path)

import resume_core  # noqa: E402
from resume_core import (  # noqa: E402,F401  (re-exported for quota_gate.py and tests)
    JITTER_MINUTES as _JITTER_MINUTES, STATE_MAX_AGE, claim_once, clear_state, fmt_local,
    parse_reset, plan_resume, read_state, state_path, working_flag_path,
)

RESUME_TAG = "[cc-quota-guard:auto-resume]"
DONE_LINE = "CC_QUOTA_DONE"

RESUME_PROMPT = (
    RESUME_TAG + " The usage quota has reset. Continue the task you were working on "
    "before the quota stop: read .cc-quota/progress.md to see where you left off, then "
    "keep going from there. If the task is already complete, say so briefly and end the "
    "turn. When the whole task is finished, append a line containing only "
    + DONE_LINE + " to .cc-quota/progress.md."
)


def schedule_instructions(plan):
    """Claude-facing (always English) instructions appended to a stop reason."""
    return (
        "\n\nAUTO-RESUME (chat mode) — this replaces 'don't call any more tools' for ONE call: "
        "as the very LAST step, after the steps above, schedule this conversation to continue "
        "by itself once the quota resets. Call the CronCreate tool exactly once with "
        f"cron \"{plan['cron']}\", recurring false, and this exact prompt:\n"
        f"{RESUME_PROMPT}\n"
        "If CronCreate isn't in your tool list yet, load it first with ToolSearch "
        "(query \"select:CronCreate\"). After it succeeds, tell the user in one line that the chat "
        f"will continue automatically at {plan['display']} (local time) as long as this Claude Code "
        "window stays open, then END THE TURN."
    )


def nudge_instructions(plan):
    """Stop-hook nudge when Claude ended the turn without scheduling the wake-up."""
    return (
        "[cc-quota-guard] The automatic resume is not scheduled yet. Call the CronCreate tool "
        f"once now with cron \"{plan['cron']}\", recurring false, and this exact prompt:\n"
        f"{RESUME_PROMPT}\n"
        "(load it with ToolSearch query \"select:CronCreate\" if needed), then end the turn. "
        "Don't do anything else."
    )


def not_reset_instructions(plan, label, pct):
    """UserPromptSubmit context when the wake-up fired but the quota hasn't reset yet."""
    return (
        f"[cc-quota-guard] The quota has NOT reset yet ({label} at {pct:.0f}%). Do NOT continue the "
        "task now. Call the CronCreate tool once with cron "
        f"\"{plan['cron']}\", recurring false, and this exact prompt:\n{RESUME_PROMPT}\n"
        "(load it with ToolSearch query \"select:CronCreate\" if needed), then end the turn with a "
        f"one-line note that work continues at {plan['display']}."
    )


def is_resume_prompt(text):
    return isinstance(text, str) and RESUME_TAG in text


def crons_have_resume(session_crons):
    """True if any session cron (from the Stop hook input) carries our tag."""
    if not isinstance(session_crons, list):
        return False
    return any(isinstance(c, dict) and RESUME_TAG in str(c.get("prompt") or "") for c in session_crons)


# --------------------------------------------------------------------------- state files

def progress_done_since(quota_dir, since):
    """True if progress.md has a CC_QUOTA_DONE line written at/after `since`."""
    return resume_core.progress_done_since(quota_dir, since, DONE_LINE)


def archive_done_progress(quota_dir):
    """A new chat stop starts a new cycle: a leftover CC_QUOTA_DONE from an
    earlier finished task would make the resume turn think it's already done."""
    resume_core.archive_done_progress(quota_dir, DONE_LINE)
