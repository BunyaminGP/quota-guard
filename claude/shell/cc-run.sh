# cc-quota-guard: makes `cc-run` callable as a bare command from your own
# bash/zsh terminal on macOS/Linux.
#
# Why this is needed: a plugin's bin/ directory is only added to PATH for
# Claude Code's OWN internal Bash tool calls — not for your regular
# terminal. This function resolves the plugin's current install path each
# time it runs, so it keeps working across plugin updates (the cache path
# changes on every version bump).
#
# Setup (one time): source this file from your shell rc file, then restart
# your terminal (or source it in the current session):
#
#   cat shell/cc-run.sh >> ~/.bashrc   # or ~/.zshrc
#   source ~/.bashrc
#
# Alternative: if you installed via install.sh instead of the plugin
# system, `~/.claude/cc-quota-guard/bin/cc-run` is already a stable path —
# just add `~/.local/bin` (where install.sh symlinks it) to your PATH and
# skip this function entirely.

cc-run() {
  local script
  script=$(claude plugin list --json 2>/dev/null | python3 -c '
import json, sys
try:
    plugins = json.load(sys.stdin)
    for p in plugins:
        if p.get("id") == "quota-guard@quota-guard":
            print(p["installPath"] + "/claude/bin/cc-run")
            break
except Exception:
    pass
')
  if [[ -z "$script" ]]; then
    echo "quota-guard plugin not found. Run: claude plugin list" >&2
    return 1
  fi
  bash "$script" "$@"
}
