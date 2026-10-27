#!/usr/bin/env python3
"""
i18n.py — shared message catalog for cc-quota-guard's user-facing text.

Only text a HUMAN reads is ever sourced from here: cc-run's own terminal
lines, and quota_gate.py's systemMessage. What tells Claude what to do (the
`reason` field, cc-run's RULES/PROMPT text) is never routed through this
module and stays English always, regardless of language — those are model
instructions, not something a person reads directly.

Adding a language: drop a new locales/<code>.json file with the same keys
as locales/en.json, translated. Keep the {placeholder} names exactly (they
feed Python's str.format(), which doesn't care what order they appear in —
put them wherever the language's word order needs) and translate
everything else. No code changes needed anywhere: the language becomes
selectable the moment the file exists, in cc-run (CC_LANG env var or
.cc-quota/config.json's "language" key), in the hook's systemMessage (same
two, plus the plugin's configure screen), and via available_languages()
below for anything that wants to list what's installed.

Library usage (quota_gate.py):
    from i18n import msg, available_languages
    msg("tr", "task_complete")
    msg("en", "soft_stop", label="5-hour session", pct=80, resets_at="...")

CLI usage (bin/cc-run, which has no JSON/string-formatting of its own):
    python3 i18n.py <lang> <key> [name=value ...]
    -> prints the formatted message, or the raw key if anything goes wrong.
       Fail-open by design: a missing locale file, a missing key, or a
       template that doesn't match the kwargs given must never crash the
       caller or hide a status line — worst case you see the bare key name
       instead of a sentence, not a traceback.
"""

import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCALES_DIR = os.path.join(os.path.dirname(_SCRIPT_DIR), "locales")
FALLBACK_LANG = "en"

_cache = {}


def _load(lang):
    if lang in _cache:
        return _cache[lang]
    path = os.path.join(_LOCALES_DIR, f"{lang}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    _cache[lang] = data
    return data


def available_languages():
    """Language codes with a locales/<code>.json file present on disk."""
    try:
        codes = sorted(
            fn[:-5] for fn in os.listdir(_LOCALES_DIR) if fn.endswith(".json")
        )
        return codes or [FALLBACK_LANG]
    except Exception:
        return [FALLBACK_LANG]


def is_known_language(lang):
    return bool(lang) and lang.lower() in available_languages()


def msg(lang, key, **kwargs):
    """
    Returns the formatted message for `key` in `lang`. Falls back to
    English, then to the raw key itself, if the language file is missing,
    the key is missing, or the template doesn't match the kwargs given —
    a translation problem must never raise into the caller.
    """
    for candidate in (lang, FALLBACK_LANG):
        template = _load(candidate).get(key)
        if template is not None:
            try:
                return template.format(**kwargs)
            except Exception:
                continue
    return key


def _cli():
    if len(sys.argv) < 3:
        print("usage: i18n.py <lang> <key> [name=value ...]", file=sys.stderr)
        sys.exit(1)
    lang, key = sys.argv[1], sys.argv[2]
    kwargs = {}
    for arg in sys.argv[3:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            kwargs[k] = v
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(msg(lang, key, **kwargs))


if __name__ == "__main__":
    _cli()
