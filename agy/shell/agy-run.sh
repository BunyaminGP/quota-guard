# agy-run shell function wrapper for bash / zsh
agy-run() {
  local plugin_root="${AGY_QUOTA_GUARD_HOME:-$HOME/.agents/plugins/agy-quota-guard}"
  if [ -x "$plugin_root/bin/agy-run" ]; then
    "$plugin_root/bin/agy-run" "$@"
  elif command -v python3 >/dev/null 2>&1 && [ -f "$plugin_root/scripts/agy_runner.py" ]; then
    python3 "$plugin_root/scripts/agy_runner.py" "$@"
  else
    echo "[agy-quota-guard] Error: agy-run is not installed in $plugin_root" >&2
    return 127
  fi
}
