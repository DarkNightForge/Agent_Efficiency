#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SOURCE="$SCRIPT_DIR/agent-efficiency"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_ROOT="$HERMES_HOME/plugins"
TARGET="$PLUGIN_ROOT/agent-efficiency"
ENABLE=true

if [[ "${1:-}" == "--no-enable" ]]; then
  ENABLE=false
elif [[ $# -gt 0 ]]; then
  printf 'Usage: %s [--no-enable]\n' "$0" >&2
  exit 2
fi

if [[ ! -f "$PLUGIN_SOURCE/plugin.yaml" ]]; then
  printf 'Plugin source is incomplete: %s\n' "$PLUGIN_SOURCE" >&2
  exit 1
fi

mkdir -p "$PLUGIN_ROOT"
if [[ -L "$TARGET" ]]; then
  CURRENT="$(readlink -f -- "$TARGET")"
  EXPECTED="$(readlink -f -- "$PLUGIN_SOURCE")"
  if [[ "$CURRENT" != "$EXPECTED" ]]; then
    printf 'Refusing to replace existing plugin symlink: %s -> %s\n' "$TARGET" "$CURRENT" >&2
    exit 1
  fi
elif [[ -e "$TARGET" ]]; then
  printf 'Refusing to replace existing plugin path: %s\n' "$TARGET" >&2
  exit 1
else
  ln -s "$PLUGIN_SOURCE" "$TARGET"
fi

printf 'Agent Efficiency plugin linked: %s -> %s\n' "$TARGET" "$PLUGIN_SOURCE"
if [[ "$ENABLE" == true ]]; then
  HERMES_HOME="$HERMES_HOME" hermes plugins enable agent-efficiency --no-allow-tool-override
  printf 'Enabled agent-efficiency. Start a new Hermes session to load it.\n'
else
  printf 'Not enabled (--no-enable). Enable later with: hermes plugins enable agent-efficiency\n'
fi
