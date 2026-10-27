#!/usr/bin/env python3
"""Antigravity CLI (agy) lifecycle hook for quota-aware execution gating.

Supports Antigravity's native hook events (`PreInvocation`, `PreToolUse`,
`PostToolUse`, `PostInvocation`) with camelCase stdin payloads (`workspacePaths`,
`toolCall`, `modelName`) as well as snake_case payloads for testing/CLI parity.
Fail-open on unexpected internal or quota-fetch errors.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n  # noqa: E402
import notify  # noqa: E402
from usage import filter_windows_for_model, get_usage, parse_iso  # noqa: E402

# Shared with the Claude Code and Codex hooks: <plugin root>/common (plugin
# layout) or a copy next to this file (standalone install).
_COMMON = Path(__file__).resolve().parent.parent.parent / "common"
if _COMMON.is_dir() and str(_COMMON) not in sys.path:
    sys.path.append(str(_COMMON))
try:
    import resume_core  # noqa: E402
except Exception:  # chat auto-resume is simply off without it
    resume_core = None

# Chat auto-resume (normal interactive `agy` chats, no agy-run): a stop also
# tells the agent to set a one-shot timer with Antigravity's own `schedule`
# tool. When the timer fires, the same conversation wakes up; the hook sees
# that as the `schedule` tool's PostToolUse, re-checks the quota, and either
# lets the chat continue ("quota reset" ping) or asks for another timer.
RESUME_TAG = "[agy-quota-guard:auto-resume]"
DONE_LINE = "AGY_QUOTA_DONE"
RESUME_PROMPT = (
    RESUME_TAG + " The usage quota has reset. Continue the task you were working on before the quota "
    "stop: read .agy-quota/progress.md to see where you left off, then keep going from there. If the "
    "task is already complete, say so briefly and end the turn. When the whole task is finished, write "
    + DONE_LINE + " on its own line in .agy-quota/progress.md."
)
MAX_TIMER_SECONDS = 6 * 3600  # a far-away weekly reset is reached in several hops
AFTER_SCHEDULE_GRACE = 3  # model calls a stopped chat may still make to wrap up after setting its timer
_CTX: dict = {"conversation_id": None, "cfg": {}, "model": None, "proj": None, "runner": None}

DEFAULT_CONFIG = {
    "session_soft_limit": 80.0,
    "session_hard_limit": 95.0,
    "weekly_soft_limit": 97.0,
    "weekly_hard_limit": 98.0,
    "hard_abort_enabled": False,
    "language": "en",
    "chat_auto_resume": True,
}

PLANNING_TOOLS = frozenset({
    "task_boundary",
    "plan_input",
    "checkpoint",
    "TodoWrite",
    "TaskCreate",
    "TaskUpdate",
    "TaskList",
    "update_plan",
})

FILE_WRITE_TOOLS = frozenset({
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "file_change",
    "code_action",
    "write_blob",
    "Write",
    "Edit",
    "MultiEdit",
})

FILE_READ_TOOLS = frozenset({
    "view_file",
    "view_file_outline",
    "view_content_chunk",
    "Read",
})

STATE_DIR_NAME = ".agy-quota"


def _quota_dir(proj: Path) -> Path:
    return proj / STATE_DIR_NAME


def _quota_dir_is_safe(proj: Path) -> bool:
    q = _quota_dir(proj)
    if q.is_symlink() or (hasattr(q, "is_junction") and q.is_junction()):
        return False
    try:
        st = q.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    import stat as _stat
    if _stat.S_ISLNK(st.st_mode):
        return False
    if not _stat.S_ISDIR(st.st_mode):
        return False
    return True


def _state_file(proj: Path) -> Path:
    return _quota_dir(proj) / "todos_state.json"


def _progress_file(proj: Path) -> Path:
    return _quota_dir(proj) / "progress.md"


def _stop_marker(proj: Path) -> Path:
    return _quota_dir(proj) / "STOP.json"


def _config_file(proj: Path) -> Path:
    return _quota_dir(proj) / "config.json"


def _parse_pct(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(n) or n <= 0.0 or n > 100.0:
        return None
    return n


def _is_truthy(val: object) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val == 1
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return False


def _load_config(proj: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)

    plugin_pct_map = {
        "AGY_PLUGIN_OPTION_SESSION_SOFT_LIMIT": "session_soft_limit",
        "AGY_PLUGIN_OPTION_SESSION_HARD_LIMIT": "session_hard_limit",
        "AGY_PLUGIN_OPTION_WEEKLY_SOFT_LIMIT": "weekly_soft_limit",
        "AGY_PLUGIN_OPTION_WEEKLY_HARD_LIMIT": "weekly_hard_limit",
    }
    for env_key, cfg_key in plugin_pct_map.items():
        if env_key in os.environ:
            parsed = _parse_pct(os.environ[env_key])
            if parsed is not None:
                cfg[cfg_key] = parsed

    if "AGY_PLUGIN_OPTION_HARD_ABORT" in os.environ:
        cfg["hard_abort_enabled"] = _is_truthy(os.environ["AGY_PLUGIN_OPTION_HARD_ABORT"])

    if "AGY_PLUGIN_OPTION_LANGUAGE" in os.environ and os.environ["AGY_PLUGIN_OPTION_LANGUAGE"].strip():
        cfg["language"] = os.environ["AGY_PLUGIN_OPTION_LANGUAGE"].strip()

    cf = _config_file(proj)
    if _quota_dir_is_safe(proj) and cf.exists() and not cf.is_symlink():
        try:
            user_cfg = json.loads(cf.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                for k in (
                    "session_soft_limit",
                    "session_hard_limit",
                    "weekly_soft_limit",
                    "weekly_hard_limit",
                ):
                    if k in user_cfg:
                        parsed = _parse_pct(user_cfg[k])
                        if parsed is not None:
                            cfg[k] = parsed
                if isinstance(user_cfg.get("language"), str) and user_cfg["language"].strip():
                    cfg["language"] = user_cfg["language"].strip()
                # Security: hard_abort_enabled and notify_url are intentionally
                # NEVER read from project-local .agy-quota/config.json.
        except Exception:
            pass

    env_pct_map = {
        "AGY_SESSION_SOFT_LIMIT": "session_soft_limit",
        "AGY_SESSION_HARD_LIMIT": "session_hard_limit",
        "AGY_WEEKLY_SOFT_LIMIT": "weekly_soft_limit",
        "AGY_WEEKLY_HARD_LIMIT": "weekly_hard_limit",
    }
    for env_key, cfg_key in env_pct_map.items():
        if env_key in os.environ:
            parsed = _parse_pct(os.environ[env_key])
            if parsed is not None:
                cfg[cfg_key] = parsed

    if "AGY_HARD_ABORT" in os.environ:
        cfg["hard_abort_enabled"] = _is_truthy(os.environ["AGY_HARD_ABORT"])

    if "AGY_LANG" in os.environ and os.environ["AGY_LANG"].strip():
        cfg["language"] = os.environ["AGY_LANG"].strip()

    # Chat auto-resume: env / plugin option / ~/.quota-guard/settings.json only,
    # never the project's config.json (like hard_abort_enabled).
    shared = resume_core.user_config() if resume_core else {}
    if cfg["language"] == DEFAULT_CONFIG["language"] and isinstance(shared.get("language"), str) \
            and not any(os.environ.get(k, "").strip() for k in ("AGY_LANG", "AGY_PLUGIN_OPTION_LANGUAGE")):
        cfg["language"] = shared["language"].strip() or cfg["language"]
    if "chat_auto_resume" in shared:
        cfg["chat_auto_resume"] = _is_truthy(shared["chat_auto_resume"])
    for env_key in ("AGY_PLUGIN_OPTION_CHAT_AUTO_RESUME", "AGY_CHAT_AUTO_RESUME"):
        if os.environ.get(env_key, "").strip():
            cfg["chat_auto_resume"] = _is_truthy(os.environ[env_key])

    cfg["language"] = i18n.resolve_lang(proj, explicit=cfg.get("language"))
    return cfg


def _git(
    proj: Path,
    *args: str,
    input_data: str | None = None,
    raw: bool = False,
) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(proj),
            input=input_data if input_data is not None else "",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        out = r.stdout if raw else r.stdout.strip()
        err = r.stderr if raw else r.stderr.strip()
        return r.returncode, out, err
    except Exception as exc:
        return 1, "", str(exc)


def _is_git_repo(proj: Path) -> bool:
    code, out, _ = _git(proj, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out == "true"


def _head_commit(proj: Path) -> str | None:
    code, out, _ = _git(proj, "rev-parse", "HEAD")
    return out if code == 0 and out else None


def _stash_ref(proj: Path) -> str | None:
    code, out, _ = _git(proj, "rev-parse", "--verify", "-q", "refs/stash")
    return out if code == 0 and out else None


def _changed_non_quota_paths(proj: Path) -> list[str] | None:
    """Return sorted relative paths of modified/staged/untracked files outside .agy-quota."""
    c1, changed, _ = _git(proj, "diff", "--relative", "--name-only", "-z", "HEAD", raw=True)
    c2, untracked, _ = _git(proj, "ls-files", "--others", "--exclude-standard", "-z", raw=True)
    if c1 != 0 or c2 != 0:
        return None
    paths: set[str] = set()
    for p in (changed + untracked).split("\0"):
        if not p:
            continue
        norm = p.replace("\\", "/")
        if norm == STATE_DIR_NAME or norm.startswith(f"{STATE_DIR_NAME}/"):
            continue
        paths.add(p)
    return sorted(paths)


def _is_working_tree_clean(proj: Path) -> bool:
    paths = _changed_non_quota_paths(proj)
    return paths is not None and len(paths) == 0


def _is_git_tracked(proj: Path, relpath: str) -> bool:
    code, _, _ = _git(proj, "ls-files", "--error-unmatch", "--", relpath)
    return code == 0


def _load_todos_state(proj: Path) -> dict:
    if not _quota_dir_is_safe(proj):
        return {}
    sf = _state_file(proj)
    if not sf.exists() or sf.is_symlink():
        return {}
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_write_text(path: Path, text: str) -> None:
    """Fresh random temp file (mkstemp = O_EXCL) + os.replace(): never writes
    through an existing path. `path.write_text()` would follow a symlink a
    cloned repo planted there (`exists()` is even False for a dangling one,
    so an is_symlink() check guarded by exists() misses it)."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write_todos_state(proj: Path, data: dict) -> None:
    if not _quota_dir_is_safe(proj):
        return
    try:
        _atomic_write_text(_state_file(proj), json.dumps(data, indent=2, ensure_ascii=False))
    except Exception:
        pass


def _record_in_progress(proj: Path, prev_in_progress: dict | None, content: str, extra: dict | None = None) -> dict:
    is_git = _is_git_repo(proj)
    if (
        isinstance(prev_in_progress, dict)
        and prev_in_progress.get("content") == content
        and "checkpoint_clean" in prev_in_progress
    ):
        entry = dict(prev_in_progress)
    else:
        entry = {
            "content": content,
            "started_at": int(time.time()),
            "checkpoint_commit": _head_commit(proj) if is_git else None,
            "checkpoint_clean": _is_working_tree_clean(proj) if is_git else False,
        }
    if extra:
        entry.update(extra)
    return entry


def _compute_todos_state_from_list(proj: Path, prev: dict, items: list, key_name: str = "content") -> dict:
    prev_ip = prev.get("in_progress") if isinstance(prev.get("in_progress"), dict) else None
    normalized_todos = []
    in_progress = None
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get(key_name) or item.get("content") or item.get("step") or item.get("subject") or "").strip()
        status = str(item.get("status") or "pending").strip()
        norm_item = {"content": text or "(unnamed item)", "status": status}
        normalized_todos.append(norm_item)
        if status == "in_progress" and in_progress is None:
            in_progress = _record_in_progress(proj, prev_ip, norm_item["content"])
    return {
        "todos": normalized_todos,
        "in_progress": in_progress,
        "updated_at": int(time.time()),
    }


def _compute_task_boundary_state(proj: Path, prev: dict, tool_input: dict) -> dict:
    todos = [dict(t) for t in (prev.get("todos") or []) if isinstance(t, dict)]
    prev_ip = prev.get("in_progress") if isinstance(prev.get("in_progress"), dict) else None

    task_name = str(
        tool_input.get("TaskName")
        or tool_input.get("taskName")
        or tool_input.get("name")
        or tool_input.get("subject")
        or tool_input.get("content")
        or ""
    ).strip()
    raw_status = str(
        tool_input.get("TaskStatus")
        or tool_input.get("taskStatus")
        or tool_input.get("status")
        or "in_progress"
    ).strip()
    status_lower = raw_status.lower()
    if status_lower in ("completed", "complete", "done", "finished"):
        norm_status = "completed"
    elif status_lower in ("pending", "todo", "not_started"):
        norm_status = "pending"
    else:
        norm_status = "in_progress"

    content = task_name or raw_status or "(unnamed item)"
    found = False
    for t in todos:
        if t.get("content") == content:
            t["status"] = norm_status
            found = True
            break
    if not found:
        todos.append({"content": content, "status": norm_status})

    if norm_status == "in_progress":
        in_progress = _record_in_progress(
            proj,
            prev_ip,
            content,
            extra={"task_status": raw_status, "mode": tool_input.get("Mode") or tool_input.get("mode")},
        )
    else:
        if prev_ip and prev_ip.get("content") == content:
            in_progress = None
        else:
            in_progress = prev_ip

    return {
        "todos": todos,
        "in_progress": in_progress,
        "updated_at": int(time.time()),
    }


def _compute_task_crud_state(proj: Path, prev: dict, tool_name: str, tool_input: dict, tool_response: object) -> dict:
    todos = [dict(t) for t in (prev.get("todos") or []) if isinstance(t, dict)]
    prev_ip = prev.get("in_progress") if isinstance(prev.get("in_progress"), dict) else None
    in_progress = prev_ip

    if tool_name == "TaskCreate":
        subj = (
            tool_input.get("subject")
            or tool_input.get("title")
            or tool_input.get("content")
            or "(unnamed item)"
        )
        tid = None
        if isinstance(tool_response, dict):
            tid = (
                tool_response.get("id")
                or tool_response.get("taskId")
                or (tool_response.get("task") or {}).get("id")
            )
        todos.append({"id": tid, "content": str(subj), "status": "pending"})

    elif tool_name == "TaskUpdate":
        tid = tool_input.get("taskId") or tool_input.get("id")
        new_status = tool_input.get("status")
        subj = tool_input.get("subject") or tool_input.get("content")
        matched = None
        for t in todos:
            if (tid is not None and str(t.get("id")) == str(tid)) or (subj and t.get("content") == subj):
                if new_status:
                    t["status"] = new_status
                if subj:
                    t["content"] = str(subj)
                matched = t
                break
        if matched is None and (tid is not None or subj):
            matched = {
                "id": tid,
                "content": str(subj or f"task-{tid}"),
                "status": str(new_status or "pending"),
            }
            todos.append(matched)
        if matched:
            if new_status == "in_progress":
                in_progress = _record_in_progress(proj, prev_ip, matched.get("content") or "(unnamed item)", {"id": tid})
            elif new_status in ("completed", "deleted", "pending"):
                if prev_ip and (
                    (tid is not None and str(prev_ip.get("id")) == str(tid))
                    or prev_ip.get("content") == matched.get("content")
                ):
                    in_progress = None

    elif tool_name == "TaskList" and isinstance(tool_response, dict):
        tasks = tool_response.get("tasks")
        if isinstance(tasks, list):
            return _compute_todos_state_from_list(proj, prev, tasks, key_name="subject")

    return {
        "todos": todos,
        "in_progress": in_progress,
        "updated_at": int(time.time()),
    }


def _compute_planning_state(proj: Path, prev: dict, tool_name: str, tool_input: dict, tool_response: object) -> dict:
    if tool_name == "TodoWrite" and isinstance(tool_input.get("todos"), list):
        return _compute_todos_state_from_list(proj, prev, tool_input["todos"], key_name="content")
    if tool_name == "update_plan" and isinstance(tool_input.get("plan"), list):
        return _compute_todos_state_from_list(proj, prev, tool_input["plan"], key_name="step")
    if tool_name in ("task_boundary", "plan_input", "checkpoint"):
        return _compute_task_boundary_state(proj, prev, tool_input)
    if tool_name in ("TaskCreate", "TaskUpdate", "TaskList"):
        return _compute_task_crud_state(proj, prev, tool_name, tool_input, tool_response)
    return prev


def _active_item_name(state: dict | None) -> str | None:
    if not isinstance(state, dict):
        return None
    ip = state.get("in_progress")
    if isinstance(ip, dict):
        content = ip.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def _sanitize_state_for_revert(proj: Path, state: dict, is_git: bool) -> dict:
    if not state or not state.get("in_progress") or not is_git:
        return state
    if _is_git_tracked(proj, f"{STATE_DIR_NAME}/todos_state.json"):
        return dict(state, in_progress=None)
    return state


def _append_progress_note(proj: Path, note: str) -> None:
    if not _quota_dir_is_safe(proj):
        return
    pf = _progress_file(proj)
    try:
        if pf.is_symlink():
            return
        existing = pf.read_text(encoding="utf-8") if pf.exists() else "# Progress\n\n"
        if not existing.endswith("\n"):
            existing += "\n"
        _atomic_write_text(pf, existing + "\n" + note)
    except Exception:
        pass


def _write_stop_marker(proj: Path, payload: dict) -> None:
    if not _quota_dir_is_safe(proj):
        return
    try:
        _atomic_write_text(_stop_marker(proj), json.dumps(payload, indent=2, ensure_ascii=False))
    except Exception:
        pass


def _read_stop_marker(proj: Path) -> dict | None:
    if not _quota_dir_is_safe(proj):
        return None
    sm = _stop_marker(proj)
    if not sm.exists() or sm.is_symlink():
        return None
    try:
        data = json.loads(sm.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _marker_still_valid(proj: Path) -> dict | None:
    data = _read_stop_marker(proj)
    if not data:
        return None
    resets_at = data.get("resets_at")
    target = parse_iso(resets_at)
    if target is None:
        return None
    if datetime.now(timezone.utc) >= target:
        return None
    return data


def _extract_target_path(tool_input: dict) -> str:
    for key in ("TargetFile", "target_file", "file_path", "path", "AbsolutePath", "filePath", "uri"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _targets_progress_file(tool_input: dict, proj: Path) -> bool:
    raw = _extract_target_path(tool_input)
    if not raw:
        return False
    norm = raw.replace("\\", "/")
    if norm.endswith(f"{STATE_DIR_NAME}/progress.md") or norm == f"./{STATE_DIR_NAME}/progress.md":
        return True
    try:
        resolved = Path(raw)
        if not resolved.is_absolute():
            resolved = (proj / resolved).resolve()
        else:
            resolved = resolved.resolve()
        return resolved == _progress_file(proj).resolve()
    except Exception:
        return False


def _is_checkpoint_tool_call(tool_name: str, tool_input: dict, proj: Path) -> bool:
    """Return True if a tool call is reading or updating the quota checkpoint."""
    if tool_name in FILE_WRITE_TOOLS and _targets_progress_file(tool_input, proj):
        return True
    if tool_name in FILE_READ_TOOLS and _targets_progress_file(tool_input, proj):
        return True
    if tool_name in PLANNING_TOOLS:
        if tool_name in ("task_boundary", "plan_input", "checkpoint"):
            status = str(
                tool_input.get("TaskStatus")
                or tool_input.get("taskStatus")
                or tool_input.get("status")
                or ""
            ).strip().lower()
            return status in ("completed", "complete", "done", "finished", "pending")
        if tool_name == "TaskUpdate":
            return str(tool_input.get("status") or "").lower() in ("completed", "pending")
        if tool_name in ("TodoWrite", "update_plan"):
            items = tool_input.get("todos") or tool_input.get("plan") or []
            if isinstance(items, list):
                return not any(
                    isinstance(x, dict) and str(x.get("status") or "").lower() == "in_progress"
                    for x in items
                )
    return False


def _evaluate_windows(windows: list[dict], cfg: dict) -> dict | None:
    """Evaluate all relevant quota windows without premature max-reduction.

    Returns None if no window exceeds a threshold, or a dict describing:
      - mode: 'hard' if ANY window exceeds its hard threshold, else 'soft'
      - primary: the highest-severity triggered window dict
      - resets_at: the latest (maximum) resets_at across ALL triggered windows
      - triggered_windows: list of all triggered window records
    """
    s_soft = float(cfg["session_soft_limit"])
    s_hard = float(cfg["session_hard_limit"])
    w_soft = float(cfg["weekly_soft_limit"])
    w_hard = float(cfg["weekly_hard_limit"])

    hard_triggered: list[dict] = []
    soft_triggered: list[dict] = []

    for w in windows:
        if not isinstance(w, dict):
            continue
        pct = w.get("pct")
        if pct is None or not isinstance(pct, (int, float)) or not math.isfinite(pct):
            continue
        kind = w.get("kind") or "session"
        if kind == "weekly":
            soft_thr, hard_thr = w_soft, w_hard
        else:
            soft_thr, hard_thr = s_soft, s_hard

        w_id = str(w.get("id") or kind)
        label = f"{w_id} ({kind})"
        rec = {
            "id": w_id,
            "group": w.get("group"),
            "kind": kind,
            "label": label,
            "pct": float(pct),
            "resets_at": w.get("resets_at"),
        }
        if pct >= hard_thr:
            rec["severity"] = "hard"
            rec["threshold"] = hard_thr
            hard_triggered.append(rec)
        elif pct >= soft_thr:
            rec["severity"] = "soft"
            rec["threshold"] = soft_thr
            soft_triggered.append(rec)

    all_triggered = hard_triggered + soft_triggered
    if not all_triggered:
        return None

    mode = "hard" if hard_triggered else "soft"
    pool = hard_triggered if hard_triggered else soft_triggered
    primary = max(
        pool,
        key=lambda r: (
            r["pct"] - r["threshold"],
            r["pct"],
            parse_iso(r.get("resets_at")) or datetime.min.replace(tzinfo=timezone.utc),
        ),
    )

    dated = [
        (parse_iso(r.get("resets_at")), r.get("resets_at"))
        for r in all_triggered
        if parse_iso(r.get("resets_at")) is not None
    ]
    if dated:
        latest_resets_at = max(dated, key=lambda pair: pair[0])[1]
    else:
        latest_resets_at = primary.get("resets_at")

    return {
        "mode": mode,
        "primary": primary,
        "resets_at": latest_resets_at,
        "triggered_windows": all_triggered,
    }


def _stash_task_changes(proj: Path, in_progress: dict, aborted_item: str, stamp: int) -> tuple[bool, str | None]:
    """Stash only non-.agy-quota changes when initial checkpoint was clean and HEAD matches.

    Uses `git diff --relative --name-only -z HEAD` + `git ls-files --others --exclude-standard -z`
    and passes literal NUL-separated pathspecs via `--pathspec-from-file=- --pathspec-file-nul`
    so `.agy-quota/` in `.gitignore` never causes pathspec exclusion errors.
    """
    if not in_progress.get("checkpoint_clean"):
        return False, None
    expected_commit = in_progress.get("checkpoint_commit")
    if not expected_commit:
        return False, None
    cur_head = _head_commit(proj)
    if not cur_head or cur_head != expected_commit:
        return False, None
    paths = _changed_non_quota_paths(proj)
    if not paths:
        return False, None

    before_ref = _stash_ref(proj)
    pathspec_nul = "".join(f":(literal){p}\0" for p in paths)
    code, out, _err = _git(
        proj,
        "stash",
        "push",
        "-u",
        "-m",
        f"agy-quota-guard: abort '{aborted_item[:60]}' @ {stamp}",
        "--pathspec-from-file=-",
        "--pathspec-file-nul",
        input_data=pathspec_nul,
    )
    after_ref = _stash_ref(proj)
    stash_created = (
        code == 0
        and bool(after_ref)
        and after_ref != before_ref
        and "No local changes to save" not in out
    )
    return stash_created, after_ref


def _revert_status(did_revert: bool, skipped: bool, has_item: bool, hard_abort_enabled: bool, is_git: bool) -> str:
    if did_revert:
        return "reverted"
    if skipped:
        return "skipped"
    if has_item and not hard_abort_enabled:
        return "disabled"
    if has_item and not is_git:
        return "no_git"
    return "no_item"


_REVERT_STATUS_KEYS = {
    "reverted": "hard_reason_reverted",
    "skipped": "hard_reason_revert_skipped_dirty",
    "disabled": "hard_reason_no_revert_disabled",
    "no_git": "hard_reason_no_revert_no_git",
    "no_item": "hard_reason_no_item",
}


# --------------------------------------------------------------------------- chat auto-resume

def _chat_enabled(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else _CTX.get("cfg") or {}
    if resume_core is None or not _CTX.get("conversation_id"):
        return False
    if _is_truthy(os.environ.get("AGY_QUOTA_RUNNER", "")) or _runner_active():
        return False  # agy-run sleeps and resumes on its own
    return bool(cfg.get("chat_auto_resume", True))


def _runner_active() -> bool:
    """agy-run is driving this project (.agy-quota/runner.json with a live PID).
    Checked in addition to AGY_QUOTA_RUNNER because hooks may be run by a
    long-lived Antigravity process that never saw the runner's environment."""
    if _CTX.get("runner") is None:
        proj = _CTX.get("proj")
        info = resume_core.read_json(str(_quota_dir(proj) / "runner.json")) if proj and resume_core else {}
        _CTX["runner"] = bool(info.get("pid")) and resume_core.pid_alive(info.get("pid"))
    return bool(_CTX["runner"])


def _wake_path(proj: Path) -> Path:
    return _quota_dir(proj) / "chat_wake.json"


def _is_resume_call(tool_name: str, tool_input: dict) -> bool:
    """Our own wake-up timer: the `schedule` tool with RESUME_TAG in its prompt
    (any argument, so a renamed Prompt field still matches)."""
    if "schedule" not in str(tool_name or "").lower():
        return False
    try:
        return RESUME_TAG in json.dumps(tool_input, ensure_ascii=False)
    except Exception:
        return False


def _chat_begin(proj: Path, prev_marker: dict | None) -> None:
    """A brand-new stop starts a new cycle (not a soft -> hard escalation)."""
    if not _chat_enabled() or (isinstance(prev_marker, dict) and prev_marker.get("chat")):
        return
    qd = str(_quota_dir(proj))
    resume_core.archive_done_progress(qd, DONE_LINE)
    resume_core.clear_state(qd)
    try:
        _wake_path(proj).unlink()
    except OSError:
        pass


def _chat_fields(marker: dict, prev_marker: dict | None = None) -> None:
    if not _chat_enabled():
        return
    kinds = [w.get("kind") for w in marker.get("triggered_windows") or [] if isinstance(w, dict)]
    buffer = 60
    try:
        buffer = max(0, int(os.environ.get("AGY_RESUME_BUFFER", "60")))
    except ValueError:
        pass
    plan = resume_core.plan_resume(marker.get("resets_at"), "weekly" if "weekly" in kinds else "session",
                                   buffer_seconds=buffer)
    prev = prev_marker if isinstance(prev_marker, dict) and prev_marker.get("chat") else {}
    marker.update(
        chat=True,
        conversation_id=_CTX["conversation_id"],
        resume_at=plan["fire_at"],
        resume_display=plan["display"],
        resume_scheduled=bool(prev.get("resume_scheduled")),
        resume_nudged=bool(prev.get("resume_nudged")),
        resume_warned=bool(prev.get("resume_warned")),
    )


def _chat_instructions(marker: dict | None) -> str:
    """Agent-facing (always English) wake-up instructions appended to a stop reason."""
    if not isinstance(marker, dict) or not marker.get("chat") or marker.get("resume_scheduled"):
        return ""
    secs = int(min(MAX_TIMER_SECONDS, max(60, float(marker.get("resume_at") or 0) - time.time())))
    return (
        "\n\nAUTO-RESUME (chat mode): as the very LAST step, call the `schedule` tool exactly once with "
        f"DurationSeconds={secs} and this exact Prompt (no CronExpression): \"{RESUME_PROMPT}\" "
        "It runs in the background. Then tell the user in one line that the chat continues by itself at "
        f"{marker.get('resume_display')} (local time) as long as this Antigravity window stays open, "
        "and END YOUR TURN."
    )


def _chat_suffix(marker: dict | None, lang: str) -> str:
    if not isinstance(marker, dict) or not marker.get("chat"):
        return ""
    return " " + i18n.msg("chat_suffix", lang=lang, time=marker.get("resume_display") or "?")


def _chat_on_wakeup(proj: Path, cfg: dict, raw_marker: dict | None) -> None:
    """The `schedule` timer fired: is the quota really back?"""
    conv = _CTX.get("conversation_id")
    if not _chat_enabled(cfg):
        return
    if not (isinstance(raw_marker, dict) and raw_marker.get("chat")):
        raw_marker = resume_core.read_json(str(_wake_path(proj))) or None
    try:
        _wake_path(proj).unlink()
    except OSError:
        pass
    if isinstance(raw_marker, dict) and raw_marker.get("conversation_id") not in (None, conv):
        return
    lang = cfg["language"]
    usage = get_usage(force_refresh=True)
    windows = filter_windows_for_model(usage, _CTX.get("model")) if usage else []
    eval_res = _evaluate_windows(windows, cfg) if usage else None
    if eval_res is not None:
        # Still over a threshold (the reset came late): stay stopped, ask for another timer.
        primary = eval_res["primary"]
        marker = dict(raw_marker) if isinstance(raw_marker, dict) else {"stopped_at": int(time.time())}
        marker.update(mode=eval_res["mode"], window=primary["id"], window_label=primary["label"],
                      pct=primary["pct"], threshold=primary["threshold"], resets_at=eval_res["resets_at"],
                      triggered_windows=eval_res["triggered_windows"], checkpoint_saved=True, soft_prompted=True)
        _chat_fields(marker)
        marker.update(resume_scheduled=False, resume_nudged=False, after_schedule_calls=0)
        _write_stop_marker(proj, marker)
        return
    try:
        _stop_marker(proj).unlink()
    except OSError:
        pass
    window_id = (raw_marker or {}).get("window")
    fresh = next((w for w in windows if str(w.get("id")) == str(window_id)), None)
    resume_core.write_state(str(_quota_dir(proj)), conv, window=window_id)
    if fresh is not None and isinstance(fresh.get("pct"), (int, float)):
        label = (raw_marker or {}).get("window_label") or window_id
        text = i18n.msg("chat_reset", lang=lang, label=label, pct=f"{float(fresh['pct']):.0f}")
    else:
        text = i18n.msg("chat_reset_nopct", lang=lang)
    notify.send(text, title="agy-quota-guard RESET", tags="battery")


def _chat_maybe_working(proj: Path, cfg: dict) -> None:
    if not _chat_enabled(cfg):
        return
    qd = str(_quota_dir(proj))
    if resume_core.read_state(qd, _CTX["conversation_id"]) and resume_core.claim_once(resume_core.working_flag_path(qd)):
        notify.send(i18n.msg("chat_working", lang=cfg["language"]), title="agy-quota-guard RESUMED", tags="arrow_forward")


def _chat_on_stop(proj: Path, cfg: dict, marker: dict | None) -> None:
    """End of a turn: warn once if the wake-up never got scheduled; 'done' after a resumed turn."""
    if not _chat_enabled(cfg):
        return
    lang = cfg["language"]
    conv = _CTX["conversation_id"]
    if isinstance(marker, dict) and marker.get("chat") and marker.get("conversation_id") == conv:
        if not marker.get("resume_scheduled") and not marker.get("resume_warned"):
            marker["resume_warned"] = True
            _write_stop_marker(proj, marker)
            notify.send(i18n.msg("chat_schedule_failed", lang=lang, time=marker.get("resume_display") or "?"),
                        title="agy-quota-guard WARNING", priority="high", tags="warning")
        return
    qd = str(_quota_dir(proj))
    state = resume_core.read_state(qd, conv)
    if not state:
        return
    done = resume_core.progress_done_since(qd, state["resumed_at"], DONE_LINE)
    notify.send(i18n.msg("chat_done" if done else "chat_turn_done", lang=lang),
                title="agy-quota-guard DONE", tags="white_check_mark")
    resume_core.clear_state(qd)


def _do_hard_stop(
    proj: Path,
    eval_res: dict,
    state: dict,
    is_git: bool,
    hard_abort_enabled: bool,
    lang: str = "en",
    prev_marker: dict | None = None,
) -> tuple[dict, str, str]:
    primary = eval_res["primary"]
    which = primary["label"]
    pct = primary["pct"]
    thr = primary["threshold"]
    resets_at = eval_res["resets_at"]

    _chat_begin(proj, prev_marker)
    in_progress = (state or {}).get("in_progress")
    aborted_item = (in_progress.get("content") if isinstance(in_progress, dict) else None) or "(unnamed item)"
    checkpoint_clean = bool(isinstance(in_progress, dict) and in_progress.get("checkpoint_clean") is True)

    stash_created = False
    did_revert = False
    skipped_dirty = False
    after_ref = None
    stamp = int(time.time())

    if hard_abort_enabled and isinstance(in_progress, dict) and is_git:
        stash_created, after_ref = _stash_task_changes(proj, in_progress, aborted_item, stamp)
        did_revert = stash_created

        if did_revert:
            todos = state.get("todos", [])
            for t in todos:
                if isinstance(t, dict) and t.get("content") == aborted_item:
                    t["status"] = "pending"
            _write_todos_state(proj, {"todos": todos, "in_progress": None, "updated_at": stamp})
            _append_progress_note(
                proj,
                "## ⛔ HARD quota threshold — item reverted\n"
                f"- Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"- Reverted item: {aborted_item}\n"
                f"- Method: git stash (stash created: {after_ref})\n"
                f"- Checkpoint commit: {in_progress.get('checkpoint_commit') or 'unknown'}\n"
                "- After the reset, this item must be redone FROM SCRATCH.\n",
            )
        else:
            skipped_dirty = True
            _append_progress_note(
                proj,
                "## ⚠️ HARD quota threshold — auto-revert skipped\n"
                f"- Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"- Item in progress: {aborted_item}\n"
                f"- Reason: {'working tree was already dirty at task start' if not checkpoint_clean else 'no new changes, HEAD moved, or git stash did not create a ref'}\n"
                "- Nothing was reverted automatically. Inspect `git status` and `git diff` on resume.\n",
            )
    elif in_progress and not hard_abort_enabled:
        _append_progress_note(
            proj,
            "## ⚠️ HARD quota threshold — auto-revert is disabled\n"
            f"- Item in progress: {aborted_item}\n"
            "- `hard_abort_enabled` is off (default), so nothing was reverted automatically.\n",
        )
    elif in_progress and not is_git:
        _append_progress_note(
            proj,
            "## ⚠️ HARD quota threshold — no git repository\n"
            f"- Item in progress: {aborted_item}\n"
            "- Not a git repository; automatic revert was skipped.\n",
        )

    marker = {
        "stopped_at": stamp,
        "mode": "hard",
        "window": primary["id"],
        "window_label": which,
        "pct": pct,
        "threshold": thr,
        "resets_at": resets_at,
        "triggered_windows": eval_res["triggered_windows"],
        "aborted_item": aborted_item if in_progress else None,
        "reverted": did_revert,
        "stash_created": stash_created,
        "revert_status": _revert_status(did_revert, skipped_dirty, bool(in_progress), hard_abort_enabled, is_git),
        "checkpoint_saved": False,
        "missing_checkpoint": False,
    }
    if isinstance(prev_marker, dict):
        # Soft -> hard escalation: keep checkpoint progress already recorded under the soft marker
        # so PostInvocation does not log a false "missing checkpoint" note.
        marker["escalated_from"] = prev_marker.get("mode")
        marker["checkpoint_saved"] = bool(prev_marker.get("checkpoint_saved"))
        marker["soft_prompted"] = bool(prev_marker.get("soft_prompted"))
    _chat_fields(marker, prev_marker)
    _write_stop_marker(proj, marker)

    stash_status = i18n.msg("stash_created" if stash_created else "stash_clean", lang=lang)
    fmt = {
        "which": which,
        "pct": f"{pct:.1f}",
        "thr": thr,
        "item": aborted_item,
        "stash_status": stash_status,
        "resets_at": resets_at or "unknown",
    }
    if did_revert:
        reason = i18n.msg("hard_reason_reverted", lang=lang, **fmt)
        user_msg = i18n.msg("hard_user_msg_reverted", lang=lang, **fmt)
    elif skipped_dirty:
        reason = i18n.msg("hard_reason_revert_skipped_dirty", lang=lang, **fmt)
        user_msg = i18n.msg("hard_user_msg_not_reverted", lang=lang, **fmt)
    elif in_progress and not hard_abort_enabled:
        reason = i18n.msg("hard_reason_no_revert_disabled", lang=lang, **fmt)
        user_msg = i18n.msg("hard_user_msg_not_reverted", lang=lang, **fmt)
    elif in_progress and not is_git:
        reason = i18n.msg("hard_reason_no_revert_no_git", lang=lang, **fmt)
        user_msg = i18n.msg("hard_user_msg_not_reverted", lang=lang, **fmt)
    else:
        reason = i18n.msg("hard_reason_no_item", lang=lang, **fmt)
        user_msg = i18n.msg("hard_user_msg_not_reverted", lang=lang, **fmt)

    reason += _chat_instructions(marker)
    user_msg += _chat_suffix(marker, lang)
    notify.send(
        i18n.msg("notify_hard", lang=lang, **fmt) + _chat_suffix(marker, lang),
        title="agy-quota-guard HARD STOP",
        priority="high",
        tags="octagonal_sign",
    )
    return marker, reason, user_msg


def _do_soft_stop(proj: Path, eval_res: dict, lang: str = "en") -> tuple[dict, str, str]:
    primary = eval_res["primary"]
    which = primary["label"]
    pct = primary["pct"]
    thr = primary["threshold"]
    resets_at = eval_res["resets_at"]
    stamp = int(time.time())
    _chat_begin(proj, None)

    marker = {
        "stopped_at": stamp,
        "mode": "soft",
        "window": primary["id"],
        "window_label": which,
        "pct": pct,
        "threshold": thr,
        "resets_at": resets_at,
        "triggered_windows": eval_res["triggered_windows"],
        "reverted": False,
        "checkpoint_saved": False,
        "soft_prompted": False,
        "missing_checkpoint": False,
    }
    _chat_fields(marker)
    _write_stop_marker(proj, marker)

    fmt = {
        "which": which,
        "pct": f"{pct:.1f}",
        "thr": thr,
        "resets_at": resets_at or "unknown",
    }
    reason = i18n.msg("soft_reason", lang=lang, **fmt) + _chat_instructions(marker)
    user_msg = i18n.msg("soft_user_msg", lang=lang, **fmt) + _chat_suffix(marker, lang)
    notify.send(
        i18n.msg("notify_soft", lang=lang, **fmt) + _chat_suffix(marker, lang),
        title="agy-quota-guard SOFT STOP",
        priority="default",
        tags="pause_button",
    )
    return marker, reason, user_msg


def _format_marker_reason(marker: dict, lang: str) -> str:
    return _format_marker_reason_base(marker, lang) + _chat_instructions(marker)


def _format_marker_reason_base(marker: dict, lang: str) -> str:
    mode = marker.get("mode", "soft")
    which = marker.get("window_label") or marker.get("window") or "quota"
    pct = marker.get("pct", 0.0)
    thr = marker.get("threshold", 0.0)
    resets_at = marker.get("resets_at") or "unknown"
    pct_str = f"{float(pct):.1f}" if isinstance(pct, (int, float)) else str(pct)
    if mode == "hard":
        item = marker.get("aborted_item") or "(unnamed item)"
        status = marker.get("revert_status")
        if status not in _REVERT_STATUS_KEYS:
            # Legacy markers without revert_status
            status = "reverted" if marker.get("reverted") else ("disabled" if marker.get("aborted_item") else "no_item")
        return i18n.msg(
            _REVERT_STATUS_KEYS[status],
            lang=lang,
            which=which,
            pct=pct_str,
            thr=thr,
            item=item,
            stash_status=i18n.msg("stash_created" if marker.get("stash_created") or marker.get("reverted") else "stash_clean", lang=lang),
            resets_at=resets_at,
        )
    return i18n.msg(
        "soft_reason",
        lang=lang,
        which=which,
        pct=pct_str,
        thr=thr,
        resets_at=resets_at,
    )


def _escalate_soft_marker(
    proj: Path,
    marker: dict | None,
    state: dict,
    cfg: dict,
    model_name: str | None,
    is_git: bool,
    lang: str,
) -> tuple[dict | None, str | None]:
    """Upgrade an active soft marker to hard if any relevant window now exceeds its hard limit.

    Returns (marker, hard_reason). hard_reason is None when no escalation happened.
    """
    if not isinstance(marker, dict) or marker.get("mode") == "hard":
        return marker, None
    usage = get_usage()
    if not usage:
        return marker, None
    eval_res = _evaluate_windows(filter_windows_for_model(usage, model_name), cfg)
    if eval_res is None or eval_res["mode"] != "hard":
        return marker, None
    safe_state = _sanitize_state_for_revert(proj, state, is_git)
    new_marker, reason, _ = _do_hard_stop(
        proj, eval_res, safe_state, is_git, bool(cfg["hard_abort_enabled"]), lang=lang, prev_marker=marker
    )
    return new_marker, reason


def _extract_context(hook: dict, cli_event: str | None) -> tuple[str, Path, str, dict, object, str | None]:
    ws_paths = hook.get("workspacePaths")
    cwd_str = None
    if isinstance(ws_paths, list) and ws_paths and isinstance(ws_paths[0], str) and ws_paths[0].strip():
        cwd_str = ws_paths[0].strip()
    elif isinstance(hook.get("cwd"), str) and hook["cwd"].strip():
        cwd_str = hook["cwd"].strip()
    else:
        cwd_str = os.environ.get("AGY_PROJECT_DIR") or os.getcwd()
    proj = Path(cwd_str)

    tool_call = hook.get("toolCall")
    if isinstance(tool_call, dict):
        tool_name = str(tool_call.get("name") or "")
        tool_input = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    else:
        tool_name = str(hook.get("tool_name") or "")
        tool_input = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}

    tool_response = hook.get("tool_response") or hook.get("result")

    model_name = (
        hook.get("modelName")
        or hook.get("model")
        or os.environ.get("AGY_MODEL")
    )
    if isinstance(model_name, str):
        model_name = model_name.strip() or None
    else:
        model_name = None

    event = (
        cli_event
        or hook.get("hookEventName")
        or hook.get("hook_event_name")
        or ""
    ).strip()
    if not event:
        if "toolCall" in hook or "tool_name" in hook:
            event = "PreToolUse"
        elif "invocationNum" in hook:
            event = "PreInvocation"
        else:
            event = "PostToolUse"

    return event, proj, tool_name, tool_input, tool_response, model_name


def _emit_json(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _handle_event(hook: dict, cli_event: str | None) -> int:
    event, proj, tool_name, tool_input, tool_response, model_name = _extract_context(hook, cli_event)

    if not _quota_dir_is_safe(proj):
        if event == "PreToolUse":
            return _emit_json({"decision": "allow"})
        return _emit_json({})

    # Clean up expired STOP.json marker (the raw one is kept for the chat wake-up check)
    marker = _read_stop_marker(proj)
    raw_marker = dict(marker) if isinstance(marker, dict) else None
    if marker is not None:
        valid_marker = _marker_still_valid(proj)
        if valid_marker is None:
            if raw_marker and raw_marker.get("chat") and resume_core is not None:
                try:
                    resume_core.write_json_atomic(str(_wake_path(proj)), raw_marker)
                except Exception:
                    pass
            try:
                _stop_marker(proj).unlink()
            except Exception:
                pass
            marker = None
        else:
            marker = valid_marker

    cfg = _load_config(proj)
    lang = cfg["language"]
    is_git = _is_git_repo(proj)
    conv_ctx = (hook.get("conversationId") or hook.get("conversation_id") or hook.get("session_id")
                or os.environ.get("ANTIGRAVITY_CONVERSATION_ID"))
    _CTX.update(conversation_id=conv_ctx.strip() if isinstance(conv_ctx, str) and conv_ctx.strip() else None,
                cfg=cfg, model=model_name, proj=proj, runner=None)

    # Persist conversationId if present in hook payload
    conv_id = hook.get("conversationId") or hook.get("conversation_id") or hook.get("session_id")
    if isinstance(conv_id, str) and conv_id.strip():
        try:
            _atomic_write_text(
                _quota_dir(proj) / "session.json",
                json.dumps({"conversation_id": conv_id.strip(), "updated_at": int(time.time())}, indent=2),
            )
        except Exception:
            pass

    # --- 1. PostToolUse: strictly returns {} and tracks state/checkpoints ---
    if event == "PostToolUse":
        has_error = bool(hook.get("error"))
        if _is_resume_call(tool_name, tool_input):
            # The wake-up timer fired (the schedule step completes when it does).
            if not has_error:
                _chat_on_wakeup(proj, cfg, raw_marker)
            return _emit_json({})
        if not has_error:
            _chat_maybe_working(proj, cfg)
        prev_state = _load_todos_state(proj)
        prev_active = _active_item_name(prev_state)
        state = prev_state

        if not has_error and tool_name in PLANNING_TOOLS:
            state = _compute_planning_state(proj, prev_state, tool_name, tool_input, tool_response)
            _write_todos_state(proj, state)

        next_active = _active_item_name(state)

        if not has_error and tool_name in FILE_WRITE_TOOLS and _targets_progress_file(tool_input, proj):
            if marker is not None:
                marker["checkpoint_saved"] = True
                marker["missing_checkpoint"] = False
                _write_stop_marker(proj, marker)

        usage = get_usage()
        if usage:
            windows = filter_windows_for_model(usage, model_name)
            eval_res = _evaluate_windows(windows, cfg)
            if eval_res is not None:
                if eval_res["mode"] == "hard" and (marker is None or marker.get("mode") != "hard"):
                    safe_state = _sanitize_state_for_revert(proj, state, is_git)
                    _do_hard_stop(
                        proj, eval_res, safe_state, is_git, bool(cfg["hard_abort_enabled"]), lang=lang, prev_marker=marker
                    )
                elif eval_res["mode"] == "soft" and marker is None:
                    # Soft boundary: only when no active item remains (e.g. item just completed or
                    # unplanned work) or when a planning tool switches to a different item.
                    # Never soft-stop mid-item while the same item is still in_progress.
                    soft_boundary = (next_active is None) or (
                        tool_name in PLANNING_TOOLS and next_active != prev_active
                    )
                    if soft_boundary:
                        _do_soft_stop(proj, eval_res, lang=lang)

        return _emit_json({})

    # --- 2. PreInvocation: injects checkpoint instructions before model runs ---
    if event == "PreInvocation":
        state = _load_todos_state(proj)
        active_item = _active_item_name(state)

        if marker is None:
            usage = get_usage()
            if usage:
                windows = filter_windows_for_model(usage, model_name)
                eval_res = _evaluate_windows(windows, cfg)
                if eval_res is not None:
                    if eval_res["mode"] == "hard":
                        safe_state = _sanitize_state_for_revert(proj, state, is_git)
                        marker, reason, _ = _do_hard_stop(
                            proj, eval_res, safe_state, is_git, bool(cfg["hard_abort_enabled"]), lang=lang
                        )
                        marker["soft_prompted"] = True
                        _write_stop_marker(proj, marker)
                        return _emit_json({"injectSteps": [{"userMessage": reason}]})
                    elif active_item is None:
                        # Soft stop in PreInvocation ONLY when no item is currently in_progress
                        marker, reason, _ = _do_soft_stop(proj, eval_res, lang=lang)
                        marker["soft_prompted"] = True
                        _write_stop_marker(proj, marker)
                        return _emit_json({"injectSteps": [{"userMessage": reason}]})

        if marker is not None:
            marker, _ = _escalate_soft_marker(proj, marker, state, cfg, model_name, is_git, lang)
            marker["soft_prompted"] = True
            _write_stop_marker(proj, marker)
            reason = _format_marker_reason(marker, lang)
            return _emit_json({"injectSteps": [{"userMessage": reason}]})

        return _emit_json({})

    # --- 3. PreToolUse: gates tool execution while preserving checkpoint writes ---
    if event == "PreToolUse":
        if _is_resume_call(tool_name, tool_input):
            # The one call a stopped chat still needs: its own wake-up timer.
            if marker is not None and marker.get("chat") and not marker.get("resume_scheduled"):
                marker["resume_scheduled"] = True
                _write_stop_marker(proj, marker)
            return _emit_json({"decision": "allow"})
        state = _load_todos_state(proj)
        prev_active = _active_item_name(state)
        is_ckpt_call = _is_checkpoint_tool_call(tool_name, tool_input, proj)

        if marker is not None:
            marker, hard_reason = _escalate_soft_marker(proj, marker, state, cfg, model_name, is_git, lang)
            if is_ckpt_call:
                return _emit_json({"decision": "allow"})
            if hard_reason:
                return _emit_json({"decision": "deny", "reason": hard_reason})
            deny_msg = i18n.msg(
                "deny_tool_after_stop",
                lang=lang,
                mode=str(marker.get("mode", "soft")).upper(),
                which=marker.get("window_label") or marker.get("window") or "quota",
                pct=f"{float(marker.get('pct', 0.0)):.1f}",
                resets_at=marker.get("resets_at") or "unknown",
            )
            return _emit_json({"decision": "deny", "reason": deny_msg})

        usage = get_usage()
        if not usage:
            return _emit_json({"decision": "allow"})

        windows = filter_windows_for_model(usage, model_name)
        eval_res = _evaluate_windows(windows, cfg)
        if eval_res is None:
            return _emit_json({"decision": "allow"})

        if eval_res["mode"] == "hard":
            safe_state = _sanitize_state_for_revert(proj, state, is_git)
            marker, reason, _ = _do_hard_stop(
                proj, eval_res, safe_state, is_git, bool(cfg["hard_abort_enabled"]), lang=lang
            )
            if is_ckpt_call:
                return _emit_json({"decision": "allow"})
            return _emit_json({"decision": "deny", "reason": reason})

        # Soft threshold in PreToolUse:
        # - If a planning tool is called:
        #   * Updating the SAME in_progress item -> allow (not a completion boundary)
        #   * Completing the active item (next_active is None) -> allow so PostToolUse records
        #     completion and then writes STOP.json
        #   * Starting a NEW/DIFFERENT in_progress item -> write STOP.json and deny
        # - If a non-planning tool is called:
        #   * If an item is currently in_progress -> allow so the active item can finish
        #   * If no item is in_progress (unplanned work) -> write STOP.json and deny (unless ckpt write)
        if tool_name in PLANNING_TOOLS:
            next_state = _compute_planning_state(proj, state, tool_name, tool_input, tool_response)
            next_active = _active_item_name(next_state)
            if prev_active is not None and next_active == prev_active:
                return _emit_json({"decision": "allow"})
            if next_active is None:
                return _emit_json({"decision": "allow"})
            marker, reason, _ = _do_soft_stop(proj, eval_res, lang=lang)
            return _emit_json({"decision": "deny", "reason": reason})

        if prev_active is None:
            marker, reason, _ = _do_soft_stop(proj, eval_res, lang=lang)
            if is_ckpt_call:
                return _emit_json({"decision": "allow"})
            return _emit_json({"decision": "deny", "reason": reason})

        return _emit_json({"decision": "allow"})

    # --- 4. PostInvocation: terminates only after checkpoint opportunity or on hard stop ---
    if event == "PostInvocation":
        state = _load_todos_state(proj)
        active_item = _active_item_name(state)

        if marker is None:
            usage = get_usage()
            if usage:
                windows = filter_windows_for_model(usage, model_name)
                eval_res = _evaluate_windows(windows, cfg)
                if eval_res is not None:
                    if eval_res["mode"] == "hard":
                        safe_state = _sanitize_state_for_revert(proj, state, is_git)
                        marker, _, _ = _do_hard_stop(
                            proj, eval_res, safe_state, is_git, bool(cfg["hard_abort_enabled"]), lang=lang
                        )
                    elif active_item is None:
                        marker, _, _ = _do_soft_stop(proj, eval_res, lang=lang)
        else:
            marker, _ = _escalate_soft_marker(proj, marker, state, cfg, model_name, is_git, lang)

        if marker is None:
            return _emit_json({})

        mode = marker.get("mode", "soft")
        ckpt_saved = bool(marker.get("checkpoint_saved"))
        soft_prompted = bool(marker.get("soft_prompted"))
        reason = _format_marker_reason(marker, lang)

        if mode == "soft" and not ckpt_saved and not soft_prompted:
            marker["soft_prompted"] = True
            _write_stop_marker(proj, marker)
            return _emit_json({"injectSteps": [{"userMessage": reason}]})

        if not ckpt_saved and not marker.get("missing_checkpoint_logged"):
            marker["missing_checkpoint"] = True
            marker["missing_checkpoint_logged"] = True
            _write_stop_marker(proj, marker)
            _append_progress_note(
                proj,
                i18n.msg(
                    "missing_checkpoint_note",
                    lang=lang,
                    time=time.strftime("%Y-%m-%d %H:%M:%S"),
                    mode=str(mode).upper(),
                    which=marker.get("window_label") or marker.get("window") or "quota",
                    pct=f"{float(marker.get('pct', 0.0)):.1f}",
                ),
            )

        if marker.get("chat") and not marker.get("resume_scheduled") and not marker.get("resume_nudged"):
            # One more model call so the agent can set its wake-up timer.
            marker["resume_nudged"] = True
            _write_stop_marker(proj, marker)
            return _emit_json({"injectSteps": [{"userMessage": reason}], "terminationBehavior": "force_continue"})

        if marker.get("chat") and marker.get("resume_scheduled") \
                and int(marker.get("after_schedule_calls") or 0) < AFTER_SCHEDULE_GRACE:
            # The wake-up timer is set: let the turn end on its own. "terminate" is
            # not used here so it can't take the background timer down with it;
            # tools stay denied, so a few calls at most.
            marker["after_schedule_calls"] = int(marker.get("after_schedule_calls") or 0) + 1
            _write_stop_marker(proj, marker)
            return _emit_json({})

        return _emit_json({
            "injectSteps": [{"userMessage": reason}],
            "terminationBehavior": "terminate",
        })

    # --- 5. Stop: chat auto-resume bookkeeping only (never blocks the stop) ---
    if event == "Stop":
        _chat_on_stop(proj, cfg, marker)
        return _emit_json({})

    return _emit_json({})


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    cli_event = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--event" and i + 1 < len(args):
            cli_event = args[i + 1]
        elif a.startswith("--event="):
            cli_event = a.split("=", 1)[1]

    try:
        raw = sys.stdin.read()
        hook = json.loads(raw) if raw.strip() else {}
        if not isinstance(hook, dict):
            hook = {}
    except Exception:
        hook = {}

    return _handle_event(hook, cli_event)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[agy-quota-guard] internal error (fail-open): {exc}", file=sys.stderr)
        sys.exit(0)
