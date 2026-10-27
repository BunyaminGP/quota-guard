#!/usr/bin/env python3
"""Quota-resilient task runner for Google Antigravity CLI (agy).

Pure Python stdlib implementation supporting both Windows and Unix:
  - Reads arbitrary-length tasks from positional arguments or stdin
  - Pipes prompts via stdin (one stream-json user event) to
    `agy --mode <mode> --input-format stream-json --output-format stream-json -p=`
  - Captures exact conversation ID from stream-json output or hook session state
  - Resumes strictly via `--conversation <ID>` (never `-c` / `--continue`)
  - Allows bounded startup retries with the original task ONLY if Round 1 fails
    before a conversation ID is created; fails explicitly if resume lacks an ID
  - Terminates the entire CLI process tree on round timeout
  - Sleeps until `resets_at` + buffer when `.agy-quota/STOP.json` appears
  - Terminates cleanly when `^AGY_QUOTA_DONE` appears in `.agy-quota/progress.md`
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import i18n  # noqa: E402
import notify  # noqa: E402
from stream_render import extract_conversation_id, render_event  # noqa: E402
from usage import sleep_seconds_until  # noqa: E402

STATE_DIR_NAME = ".agy-quota"
DONE_MARKER_RE = re.compile(r"^AGY_QUOTA_DONE(?:\b|$)", re.MULTILINE)


def _print_help() -> None:
    print(
        "Usage: agy-run [--model <model>] [--mode <mode>] [--] [task...]\n"
        "       agy-run < task.md\n\n"
        "Quota-aware auto-resume runner for Google Antigravity CLI (agy).\n\n"
        "Options:\n"
        "  --model <name>   Pass --model <name> to agy and filter quota buckets\n"
        "  --mode <mode>    Permission mode for agy (default: accept-edits)\n"
        "  -h, --help       Show this help message and exit\n\n"
        "Environment overrides:\n"
        "  AGY_CLI_BIN              Path to agy executable (default: agy)\n"
        "  AGY_MODEL                Model name override\n"
        "  AGY_PERMISSION_MODE      Permission mode (default: accept-edits)\n"
        "  AGY_CLI_ARGS             Additional whitespace-separated args for agy\n"
        "  AGY_RESUME_BUFFER        Extra seconds after reset before resume (default: 60)\n"
        "  AGY_FALLBACK_SLEEP       Fallback sleep seconds if resets_at missing (default: 600)\n"
        "  AGY_MAX_RETRIES          Max consecutive non-quota retries (default: 5)\n"
        "  AGY_RETRY_BACKOFF        Linear backoff base seconds (default: 30)\n"
        "  AGY_ROUND_TIMEOUT        Per-round timeout seconds, 0 disables (default: 3600)\n"
        "  AGY_HARD_ABORT           Set to 1 to enable opt-in git stash on hard stop\n"
        "  AGY_NOTIFY_URL           Optional ntfy/webhook URL for status notifications\n"
        "  AGY_LANG                 Message language: en | tr\n"
    )


def _parse_cli_args(argv: list[str]) -> tuple[str | None, str | None, list[str], bool]:
    model: str | None = None
    mode: str | None = None
    task_parts: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            return None, None, [], True
        if arg == "--":
            task_parts.extend(argv[i + 1 :])
            break
        if arg == "--model" and i + 1 < len(argv):
            model = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--model="):
            model = arg.split("=", 1)[1]
            i += 1
            continue
        if arg == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
            i += 1
            continue
        task_parts.append(arg)
        i += 1
    return model, mode, task_parts, False


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    pid = proc.pid
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                input="",
                text=True,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        time.sleep(0.2)
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def _write_text_atomic(path: Path, text: str) -> None:
    """Fresh mkstemp file + os.replace(): replaces `path` itself instead of
    writing through a symlink a cloned repo may have planted there."""
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".md")
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


def _unsafe_state_path(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    return path.exists() and not (path.is_dir() if path.name == STATE_DIR_NAME else path.is_file())


def _ensure_progress_file(proj: Path) -> Path:
    qdir = proj / STATE_DIR_NAME
    pf = qdir / "progress.md"
    if _unsafe_state_path(qdir) or _unsafe_state_path(pf):
        print(f"❌ [agy-quota-guard] Refusing to run: {STATE_DIR_NAME}/ or {STATE_DIR_NAME}/progress.md "
              "is a symlink or not a regular file/folder (it may have come with a cloned repo).", file=sys.stderr)
        sys.exit(2)
    qdir.mkdir(parents=True, exist_ok=True)
    tmpl = PLUGIN_ROOT / "templates" / "progress.md"
    if not pf.exists():
        if tmpl.exists():
            _write_text_atomic(pf, tmpl.read_text(encoding="utf-8"))
        else:
            _write_text_atomic(pf, "# Task Progress\n\n")
    else:
        text = pf.read_text(encoding="utf-8")
        if DONE_MARKER_RE.search(text):
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            updated = DONE_MARKER_RE.sub(f"AGY_QUOTA_DONE_ARCHIVED ({stamp})", text)
            _write_text_atomic(pf, updated)
            print("ℹ️  Archived previous AGY_QUOTA_DONE marker in .agy-quota/progress.md", flush=True)
    return pf


def _has_done_marker(pf: Path) -> bool:
    if pf.is_symlink() or not pf.exists():
        return False
    try:
        return bool(DONE_MARKER_RE.search(pf.read_text(encoding="utf-8")))
    except Exception:
        return False


def _read_hook_session_id(proj: Path) -> str | None:
    sf = proj / STATE_DIR_NAME / "session.json"
    if not sf.exists():
        return None
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        cid = data.get("conversation_id") if isinstance(data, dict) else None
        return cid.strip() if isinstance(cid, str) and cid.strip() else None
    except Exception:
        return None


def _clear_hook_session_id(proj: Path) -> None:
    sf = proj / STATE_DIR_NAME / "session.json"
    if sf.exists():
        try:
            sf.unlink()
        except Exception:
            pass


def _mark_runner(proj: Path) -> None:
    """Tell the hooks that agy-run owns this project's waits, so they don't also
    schedule a chat wake-up (env for hooks run as our child, a PID file for
    hooks run by a long-lived Antigravity process)."""
    import atexit

    os.environ["AGY_QUOTA_RUNNER"] = "1"
    marker = proj / STATE_DIR_NAME / "runner.json"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        import tempfile

        fd, tmp = tempfile.mkstemp(dir=str(marker.parent), prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started_at": int(time.time())}, f)
        os.replace(tmp, str(marker))
    except OSError:
        return

    def _cleanup() -> None:
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
            if info.get("pid") == os.getpid():
                marker.unlink()
        except Exception:
            pass

    atexit.register(_cleanup)


def _run_agy_round(
    agy_bin: str,
    cmd_args: list[str],
    prompt_text: str,
    proj: Path,
    timeout_sec: float,
    stream_enabled: bool,
) -> tuple[int, str | None]:
    popen_kwargs: dict = {
        "cwd": str(proj),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen([agy_bin, *cmd_args], **popen_kwargs)
    captured_cid: list[str] = []

    def _feed_stdin() -> None:
        try:
            if proc.stdin:
                # agy 1.4.x: `-p` takes its prompt as a value, so the prompt goes in as
                # one stream-json user event instead (any length, no quoting issues).
                proc.stdin.write(json.dumps(
                    {"event": "user", "message": {"role": "user", "content": prompt_text}},
                    ensure_ascii=False) + "\n")
                proc.stdin.close()
        except Exception:
            pass

    def _read_stdout() -> None:
        if not proc.stdout:
            return
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if isinstance(ev, dict):
                    cid = extract_conversation_id(ev)
                    if cid and not captured_cid:
                        captured_cid.append(cid)
                    if stream_enabled:
                        for rendered in render_event(ev):
                            print(rendered, flush=True)
                    continue
            except Exception:
                pass
            if stream_enabled:
                print(raw_line, end="", flush=True)

    def _read_stderr() -> None:
        if not proc.stderr:
            return
        for raw_line in proc.stderr:
            sys.stderr.write(raw_line)
            sys.stderr.flush()

    t_in = threading.Thread(target=_feed_stdin, daemon=True)
    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_in.start()
    t_out.start()
    t_err.start()

    start_ts = time.monotonic()
    last_beat = start_ts
    timed_out = False

    while proc.poll() is None:
        now = time.monotonic()
        elapsed = now - start_ts
        if timeout_sec > 0 and elapsed >= timeout_sec:
            timed_out = True
            _kill_process_tree(proc)
            break
        if now - last_beat >= 15.0:
            last_beat = now
            print(f"  … [working, {int(elapsed)}s elapsed]", flush=True)
        time.sleep(0.1)

    t_in.join(timeout=2.0)
    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)

    cid = captured_cid[0] if captured_cid else _read_hook_session_id(proj)
    if timed_out:
        return 124, cid
    return (proc.returncode if proc.returncode is not None else 1), cid


def _interruptible_sleep(seconds: int) -> None:
    remaining = max(0, int(seconds))
    end_time = time.monotonic() + remaining
    while True:
        left = end_time - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(1.0, left))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    args = sys.argv[1:] if argv is None else argv
    cli_model, cli_mode, task_parts, show_help = _parse_cli_args(args)
    if show_help:
        _print_help()
        return 0

    if task_parts:
        initial_task = " ".join(task_parts)
    elif not sys.stdin.isatty():
        initial_task = sys.stdin.read()
    else:
        _print_help()
        return 2

    if not initial_task or not initial_task.strip():
        print("[agy-quota-guard] Error: empty task provided.", file=sys.stderr)
        return 2

    proj = Path(os.environ.get("AGY_PROJECT_DIR") or os.getcwd()).resolve()
    lang = i18n.resolve_lang(proj)
    agy_bin = os.environ.get("AGY_CLI_BIN", "").strip() or "agy"
    model = cli_model or os.environ.get("AGY_MODEL", "").strip() or None
    mode = cli_mode or os.environ.get("AGY_PERMISSION_MODE", "").strip() or "accept-edits"
    extra_raw = os.environ.get("AGY_CLI_ARGS", "").strip()
    extra_args = shlex.split(extra_raw) if extra_raw else []

    resume_buffer = int(os.environ.get("AGY_RESUME_BUFFER", "60"))
    fallback_sleep = int(os.environ.get("AGY_FALLBACK_SLEEP", "600"))
    max_retries = int(os.environ.get("AGY_MAX_RETRIES", "5"))
    retry_backoff = int(os.environ.get("AGY_RETRY_BACKOFF", "30"))
    round_timeout = float(os.environ.get("AGY_ROUND_TIMEOUT", "3600"))
    stream_enabled = os.environ.get("AGY_STREAM", "1").strip() not in ("0", "false", "no")

    if model:
        os.environ["AGY_MODEL"] = model

    progress_file = _ensure_progress_file(proj)
    stop_marker_path = proj / STATE_DIR_NAME / "STOP.json"
    _mark_runner(proj)
    _clear_hook_session_id(proj)

    append_instructions = i18n.msg("append_prompt", lang=lang)
    full_initial_prompt = f"{initial_task}{append_instructions}"
    current_prompt = full_initial_prompt

    conversation_id: str | None = None
    round_num = 0
    consec_Wait_failures = 0

    while True:
        round_num += 1
        cmd: list[str] = []
        if conversation_id is not None:
            cmd.extend(["--conversation", conversation_id])
        cmd.extend(["--mode", mode, "--input-format", "stream-json", "--output-format", "stream-json", "-p="])
        if model:
            cmd.extend(["--model", model])
        cmd.extend(extra_args)

        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
        cid_label = conversation_id[:12] if conversation_id else "new"
        print(
            f"▶️  [agy-quota-guard] Round #{round_num} — {time.strftime('%Y-%m-%d %H:%M:%S')} (conversation={cid_label})",
            flush=True,
        )
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

        rc, detected_cid = _run_agy_round(
            agy_bin=agy_bin,
            cmd_args=cmd,
            prompt_text=current_prompt,
            proj=proj,
            timeout_sec=round_timeout,
            stream_enabled=stream_enabled,
        )
        if detected_cid and not conversation_id:
            conversation_id = detected_cid

        # Case 1: Quota stop marker written by hook
        if stop_marker_path.exists():
            consec_Wait_failures = 0
            try:
                marker = json.loads(stop_marker_path.read_text(encoding="utf-8"))
                if not isinstance(marker, dict):
                    marker = {}
            except Exception:
                marker = {}

            resets_at = marker.get("resets_at")
            stop_mode = str(marker.get("mode") or "soft")
            window_label = str(marker.get("window_label") or marker.get("window") or "quota")
            pct_val = marker.get("pct", "?")
            reverted = bool(marker.get("reverted"))

            # Strict conversation ID check: NEVER fall back to -c / --continue
            if not conversation_id:
                err_msg = i18n.msg("err_missing_conversation_id", lang=lang)
                print(err_msg, file=sys.stderr, flush=True)
                notify.send(
                    i18n.msg("notify_no_conversation_id", lang=lang),
                    title="agy-quota-guard ERROR",
                    priority="high",
                    tags="x",
                )
                return 3

            sleep_sec = sleep_seconds_until(resets_at, buffer_sec=resume_buffer)
            if sleep_sec is None:
                sleep_sec = fallback_sleep

            print("", flush=True)
            print(
                f"⏸️  [agy-quota-guard] Quota stop ({stop_mode}): {window_label} at {pct_val}%",
                flush=True,
            )
            print(
                f"⏳  Reset time: {resets_at or 'unknown'} — sleeping {sleep_sec}s (includes {resume_buffer}s buffer)",
                flush=True,
            )

            _interruptible_sleep(sleep_sec)
            try:
                stop_marker_path.unlink()
            except Exception:
                pass

            print(
                f"🔔  [agy-quota-guard] Quota reset window reached ({time.strftime('%Y-%m-%d %H:%M:%S')}). Resuming {conversation_id}...",
                flush=True,
            )
            notify.send(
                i18n.msg("notify_resumed", lang=lang, which=window_label, round=round_num + 1),
                title="agy-quota-guard RESUMED",
                priority="default",
                tags="arrow_forward",
            )

            if stop_mode == "hard":
                if reverted:
                    current_prompt = i18n.msg("resume_hard_reverted", lang=lang)
                else:
                    current_prompt = i18n.msg("resume_hard_unreverted", lang=lang)
            else:
                current_prompt = i18n.msg("resume_soft", lang=lang)
            continue

        # Case 2: Task completed with AGY_QUOTA_DONE marker
        if _has_done_marker(progress_file):
            print("", flush=True)
            print("✅ [agy-quota-guard] Task completed (AGY_QUOTA_DONE found in .agy-quota/progress.md).", flush=True)
            notify.send(
                i18n.msg("notify_done", lang=lang, rounds=round_num),
                title="agy-quota-guard DONE",
                priority="default",
                tags="white_check_mark",
            )
            return 0

        # Case 3: Non-zero exit without STOP.json (crash, network error, or timeout)
        if rc != 0:
            consec_Wait_failures += 1
            if rc == 124:
                print(
                    f"⏱️  [agy-quota-guard] Round timed out after {int(round_timeout)}s (attempt {consec_Wait_failures}/{max_retries}).",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"⚠️  [agy-quota-guard] agy exited with code {rc} (attempt {consec_Wait_failures}/{max_retries}).",
                    file=sys.stderr,
                    flush=True,
                )

            if consec_Wait_failures > max_retries:
                print(
                    f"⛔ [agy-quota-guard] Exhausted {max_retries} retries. Aborting.",
                    file=sys.stderr,
                    flush=True,
                )
                notify.send(
                    i18n.msg("notify_failed", lang=lang, retries=max_retries, rc=rc),
                    title="agy-quota-guard FAILED",
                    priority="high",
                    tags="x",
                )
                return rc

            wait_s = consec_Wait_failures * retry_backoff
            print(f"⏳  Backing off for {wait_s}s before retry...", flush=True)
            _interruptible_sleep(wait_s)

            # If Round 1 failed before producing a conversation ID, retry from scratch
            # with the original prompt. If a conversation ID was already captured,
            # resume that exact conversation ID.
            if conversation_id is None:
                current_prompt = full_initial_prompt
            else:
                current_prompt = i18n.msg("continue_incomplete", lang=lang)
            continue

        # Case 4: Exit 0, no STOP.json, and no AGY_QUOTA_DONE yet
        if not conversation_id:
            err_msg = i18n.msg("err_missing_conversation_id", lang=lang)
            print(err_msg, file=sys.stderr, flush=True)
            notify.send(
                i18n.msg("notify_no_conversation_id", lang=lang),
                title="agy-quota-guard ERROR",
                priority="high",
                tags="x",
            )
            return 3

        consec_Wait_failures += 1
        if consec_Wait_failures > max_retries:
            print(
                "ℹ️  [agy-quota-guard] agy exited 0 without AGY_QUOTA_DONE and max continuation attempts reached.",
                flush=True,
            )
            notify.send(
                i18n.msg("notify_incomplete", lang=lang),
                title="agy-quota-guard STOPPED",
                priority="default",
                tags="warning",
            )
            return 0

        current_prompt = i18n.msg("continue_incomplete", lang=lang)


if __name__ == "__main__":
    sys.exit(main())
