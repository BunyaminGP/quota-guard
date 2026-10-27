#!/usr/bin/env python3
"""Minimal JSON-based i18n helper for agy-quota-guard.

Supported languages: 'en' (default), 'tr'.
Resolution order:
  1. explicit `lang` argument
  2. `AGY_LANG` environment variable
  3. `AGY_PLUGIN_OPTION_LANGUAGE` environment variable
  4. `.agy-quota/config.json` -> `"language"`
  5. fallback `'en'`
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
SUPPORTED = ("en", "tr")
DEFAULT_LANG = "en"

_CACHE: dict[str, dict[str, str]] = {}


def _normalize(code: str | None) -> str:
    if not code or not isinstance(code, str):
        return DEFAULT_LANG
    base = code.strip().lower().split("_")[0].split("-")[0]
    return base if base in SUPPORTED else DEFAULT_LANG


def resolve_lang(project_dir: str | Path | None = None, explicit: str | None = None) -> str:
    if explicit:
        return _normalize(explicit)
    for var in ("AGY_LANG", "AGY_PLUGIN_OPTION_LANGUAGE"):
        val = os.environ.get(var)
        if val:
            return _normalize(val)
    if project_dir:
        cfg = Path(project_dir) / ".agy-quota" / "config.json"
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("language"):
                    return _normalize(data["language"])
            except Exception:
                pass
    return DEFAULT_LANG


def _load(lang: str) -> dict[str, str]:
    lang = _normalize(lang)
    if lang in _CACHE:
        return _CACHE[lang]
    path = LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        path = LOCALES_DIR / f"{DEFAULT_LANG}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _CACHE[lang] = data
    return data


def msg(key: str, lang: str | None = None, **kwargs) -> str:
    catalog = _load(lang or DEFAULT_LANG)
    template = catalog.get(key)
    if template is None and (lang or DEFAULT_LANG) != DEFAULT_LANG:
        template = _load(DEFAULT_LANG).get(key)
    if template is None:
        return key
    if kwargs:
        try:
            return template.format(**kwargs)
        except Exception:
            return template
    return template


def main() -> int:
    if len(sys.argv) < 2:
        return 1
    key = sys.argv[1]
    lang = resolve_lang(os.getcwd())
    kv = {}
    for arg in sys.argv[2:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            kv[k] = v
    sys.stdout.write(msg(key, lang=lang, **kv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
