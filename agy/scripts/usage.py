#!/usr/bin/env python3
"""Antigravity CLI (agy) quota reader and normalizer.

Reads quota information exclusively via `agy -p /usage --output-format json`
(or `AGY_USAGE_FIXTURE` for offline tests) without touching any credential or
key files. Preserves the complete list of quota windows across model groups and
filters relevant buckets based on `modelName` / `--model`.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CACHE_TTL = 30
STALE_CACHE_MAX_AGE = 300
SUBPROCESS_TIMEOUT = 15


def _cache_file() -> Path:
    override = os.environ.get("AGY_QUOTA_CACHE_FILE", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".gemini" / "antigravity-cli" / ".agy-quota-cache.json"


def parse_iso(iso_ts: str | int | float | None) -> datetime | None:
    if iso_ts is None:
        return None
    if isinstance(iso_ts, (int, float)):
        if not math.isfinite(iso_ts) or iso_ts <= 0:
            return None
        try:
            return datetime.fromtimestamp(float(iso_ts), tz=timezone.utc)
        except Exception:
            return None
    if not isinstance(iso_ts, str):
        return None
    raw = iso_ts.strip()
    if not raw:
        return None
    if raw.isdigit():
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except Exception:
            return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _extract_groups(raw_obj: object) -> list[dict] | None:
    if not isinstance(raw_obj, dict):
        return None
    # Direct command.data.groups
    cmd = raw_obj.get("command")
    if isinstance(cmd, dict):
        data = cmd.get("data")
        if isinstance(data, dict) and isinstance(data.get("groups"), list):
            return data["groups"]
    # stream-json result.command.data.groups
    res = raw_obj.get("result")
    if isinstance(res, dict):
        rcmd = res.get("command")
        if isinstance(rcmd, dict):
            rdata = rcmd.get("data")
            if isinstance(rdata, dict) and isinstance(rdata.get("groups"), list):
                return rdata["groups"]
    # Direct data.groups or groups
    if isinstance(raw_obj.get("data"), dict) and isinstance(raw_obj["data"].get("groups"), list):
        return raw_obj["data"]["groups"]
    if isinstance(raw_obj.get("groups"), list):
        return raw_obj["groups"]
    return None


def _parse_raw_usage_text(text: str) -> dict | None:
    if not text or not text.strip():
        return None
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict) and (_extract_groups(obj) is not None or isinstance(obj.get("windows"), list)):
            return obj
    except Exception:
        pass
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and _extract_groups(obj) is not None:
                return obj
        except Exception:
            continue
    return None


def _classify_kind(window_raw: str, bucket_id: str) -> str:
    w = (window_raw or "").strip().lower()
    bid = (bucket_id or "").strip().lower()
    if w in ("5h", "5hr", "five_hour", "session") or "5h" in bid:
        return "session"
    if w in ("weekly", "7d", "seven_day", "week") or "weekly" in bid or "week" in bid:
        return "weekly"
    return "session"


def _pick_representative(windows: list[dict], kind: str) -> dict:
    candidates = [w for w in windows if w.get("kind") == kind and w.get("pct") is not None]
    if not candidates:
        return {"pct": None, "resets_at": None, "id": None, "group": None}
    best = max(
        candidates,
        key=lambda w: (
            float(w["pct"]),
            parse_iso(w.get("resets_at")) or datetime.min.replace(tzinfo=timezone.utc),
        ),
    )
    return {
        "pct": best["pct"],
        "resets_at": best.get("resets_at"),
        "id": best.get("id"),
        "group": best.get("group"),
    }


def _normalize(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    # Already normalized structure with windows list
    if isinstance(raw.get("windows"), list) and all(isinstance(w, dict) for w in raw["windows"]):
        windows = []
        for w in raw["windows"]:
            pct = w.get("pct")
            if pct is None and w.get("remaining_fraction") is not None:
                try:
                    rem = float(w["remaining_fraction"])
                    if math.isfinite(rem):
                        pct = round(max(0.0, min(100.0, (1.0 - rem) * 100.0)), 4)
                except Exception:
                    pct = None
            elif pct is not None:
                try:
                    pct = round(max(0.0, min(100.0, float(pct))), 4)
                except Exception:
                    pct = None
            if pct is None:
                continue
            bid = str(w.get("id") or w.get("window") or "default")
            w_raw = str(w.get("window") or ("5h" if w.get("kind") == "session" else "weekly"))
            kind = w.get("kind") or _classify_kind(w_raw, bid)
            windows.append({
                "id": bid,
                "group": str(w.get("group") or "Default"),
                "name": str(w.get("name") or bid),
                "window": w_raw,
                "kind": kind,
                "pct": pct,
                "remaining_fraction": w.get("remaining_fraction"),
                "resets_at": w.get("resets_at") or w.get("reset_time"),
            })
    else:
        groups = _extract_groups(raw)
        if groups is None:
            return None
        windows = []
        for grp in groups:
            if not isinstance(grp, dict):
                continue
            grp_name = str(grp.get("name") or "Default")
            buckets = grp.get("buckets")
            if not isinstance(buckets, list):
                continue
            for b in buckets:
                if not isinstance(b, dict):
                    continue
                rem_raw = b.get("remaining_fraction")
                if rem_raw is None:
                    rem_raw = b.get("remainingFraction")
                pct_val = None
                rem_val = None
                if rem_raw is not None and not isinstance(rem_raw, bool):
                    try:
                        rem_val = float(rem_raw)
                        if math.isfinite(rem_val):
                            pct_val = round(max(0.0, min(100.0, (1.0 - rem_val) * 100.0)), 4)
                    except Exception:
                        pct_val = None
                if pct_val is None and b.get("used_percent") is not None:
                    try:
                        u = float(b["used_percent"])
                        if math.isfinite(u):
                            pct_val = round(max(0.0, min(100.0, u)), 4)
                    except Exception:
                        pct_val = None
                if pct_val is None:
                    continue
                bid = str(b.get("id") or b.get("bucketId") or f"{grp_name}:{b.get('window', 'unknown')}")
                w_raw = str(b.get("window") or "")
                kind = _classify_kind(w_raw, bid)
                resets_at = b.get("reset_time") or b.get("resetTime") or b.get("resets_at")
                windows.append({
                    "id": bid,
                    "group": grp_name,
                    "name": str(b.get("name") or bid),
                    "window": w_raw or ("5h" if kind == "session" else "weekly"),
                    "kind": kind,
                    "pct": pct_val,
                    "remaining_fraction": rem_val,
                    "resets_at": resets_at,
                })

    if not windows:
        return None

    session_rep = _pick_representative(windows, "session")
    weekly_rep = _pick_representative(windows, "weekly")
    argon_or_3p = [
        w for w in windows
        if w.get("kind") == "weekly" and (
            str(w.get("id", "")).startswith(("3p-", "gemini-argon"))
        )
    ]
    secondary_rep = _pick_representative(argon_or_3p, "weekly") if argon_or_3p else {"pct": None, "resets_at": None, "id": None, "group": None}

    return {
        "windows": windows,
        "session": session_rep,
        "week_all": weekly_rep,
        "week_secondary": secondary_rep,
        "fetched_at": int(time.time()),
    }


def filter_windows_for_model(usage: dict | None, model_name: str | None = None) -> list[dict]:
    """Return the subset of quota windows applicable to `model_name`.

    - If `model_name` is empty, 'auto', 'default', 'inherit', or 'unspecified',
      returns all windows across active groups.
    - If `model_name` indicates a 3rd-party model (claude, sonnet, opus, haiku,
      gpt, o1, o3, o4, 3p), returns 'Claude and GPT models' (`3p-*`) windows.
    - If `model_name` indicates Argon (`argon`), returns 'Gemini 4 Argon' and
      'Gemini Models' windows.
    - If `model_name` indicates Gemini (`gemini`, `flash`, `pro`), returns
      'Gemini Models' (`gemini-5h`, `gemini-weekly`) windows.
    - If filtering yields no windows (unrecognized custom model), falls back to
      all windows so quota protection is never silently bypassed.
    """
    if not isinstance(usage, dict):
        return []
    windows = usage.get("windows")
    if not isinstance(windows, list):
        return []

    m = (model_name or "").strip().lower()
    if not m or m in ("auto", "default", "inherit", "unspecified"):
        return list(windows)

    matched: list[dict] = []
    is_argon = "argon" in m
    is_3p = any(tok in m for tok in ("claude", "sonnet", "opus", "haiku", "gpt", "o1", "o3", "o4", "3p"))
    is_gemini = any(tok in m for tok in ("gemini", "flash")) or ("pro" in m and not is_3p)

    for w in windows:
        if not isinstance(w, dict):
            continue
        bid = str(w.get("id") or "").lower()
        grp = str(w.get("group") or "").lower()
        if is_argon:
            if bid.startswith("gemini-argon") or "argon" in grp or (
                bid.startswith("gemini-") and not bid.startswith("gemini-argon")
            ):
                matched.append(w)
        elif is_3p:
            if bid.startswith("3p-") or "claude" in grp or "gpt" in grp or "3p" in grp:
                matched.append(w)
        elif is_gemini:
            if (bid.startswith("gemini-") and not bid.startswith("gemini-argon")) or grp == "gemini models":
                matched.append(w)

    return matched if matched else list(windows)


def _fetch_from_cli() -> dict | None:
    fixture = os.environ.get("AGY_USAGE_FIXTURE", "").strip()
    if fixture:
        p = Path(fixture)
        if p.exists() and p.is_file():
            try:
                return _parse_raw_usage_text(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        return _parse_raw_usage_text(fixture)

    agy_bin = os.environ.get("AGY_CLI_BIN", "").strip() or "agy"
    try:
        proc = subprocess.run(
            [agy_bin, "-p", "/usage", "--output-format", "json"],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
        )
        if proc.returncode != 0:
            return None
        return _parse_raw_usage_text(proc.stdout)
    except Exception:
        return None


def _current_fixture_sig() -> str | None:
    fixture = os.environ.get("AGY_USAGE_FIXTURE", "").strip()
    if not fixture:
        return ""
    p = Path(fixture)
    if p.exists() and p.is_file():
        try:
            st = p.stat()
            return f"{p.resolve()}:{st.st_mtime_ns}:{st.st_size}"
        except Exception:
            return fixture
    if fixture.startswith("{"):
        return f"inline:{hash(fixture)}"
    return None


def get_usage(force_refresh: bool = False) -> dict | None:
    cache_path = _cache_file()
    now = time.time()
    cached_data: dict | None = None
    cur_sig = _current_fixture_sig()

    if cache_path.exists():
        try:
            raw_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(raw_cache, dict) and isinstance(raw_cache.get("windows"), list):
                cached_sig = raw_cache.get("fixture_sig", "")
                if cur_sig is None or cached_sig == cur_sig:
                    cached_data = raw_cache
                    age = now - float(raw_cache.get("fetched_at", 0))
                    if not force_refresh and 0 <= age < CACHE_TTL:
                        return cached_data
        except Exception:
            cached_data = None

    raw = _fetch_from_cli()
    norm = _normalize(raw) if raw else None
    if norm:
        norm["fixture_sig"] = cur_sig or ""
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(norm, indent=2), encoding="utf-8")
        except Exception:
            pass
        return norm

    if cached_data is not None:
        age = now - float(cached_data.get("fetched_at", 0))
        if age < STALE_CACHE_MAX_AGE:
            return cached_data
    return None


def sleep_seconds_until(iso_ts: str | int | float | None, buffer_sec: int = 60) -> int | None:
    target = parse_iso(iso_ts)
    if target is None:
        return None
    now = datetime.now(timezone.utc)
    delta = int((target - now).total_seconds())
    return max(0, delta) + int(buffer_sec)


def main() -> int:
    force = "--refresh" in sys.argv
    model_arg = None
    for i, a in enumerate(sys.argv[1:], start=1):
        if a == "--model" and i < len(sys.argv) - 1:
            model_arg = sys.argv[i + 1]
        elif a.startswith("--model="):
            model_arg = a.split("=", 1)[1]

    data = get_usage(force_refresh=force)
    if data is None:
        print(json.dumps({"error": "unavailable"}), file=sys.stderr)
        return 2
    if model_arg:
        filtered = filter_windows_for_model(data, model_arg)
        out = dict(data, windows=filtered)
    else:
        out = data
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
