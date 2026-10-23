#!/usr/bin/env bash
# Install usage-budget for Claude Code.
#
#   ./install.sh              symlink CLI, create state dir, wire hooks
#   ./install.sh --no-hooks   everything except touching settings.json
#
# Idempotent: safe to re-run. settings.json is backed up before any edit, and
# hooks are only added if not already present.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$SCRIPT_DIR/usage_budget.py"
STATE_DIR="${USAGE_BUDGET_HOME:-$HOME/.claude/usage-budget}"
SETTINGS="$HOME/.claude/settings.json"
BIN_DIR="$HOME/.local/bin"
WIRE_HOOKS=1
[[ "${1:-}" == "--no-hooks" ]] && WIRE_HOOKS=0

command -v python3 >/dev/null || { echo "error: python3 not found"; exit 1; }
[[ -f "$TOOL" ]] || { echo "error: $TOOL missing"; exit 1; }

echo "==> state dir: $STATE_DIR"
mkdir -p "$STATE_DIR"

echo "==> CLI shim: $BIN_DIR/usage-budget"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/usage-budget" <<EOF
#!/bin/sh
exec python3 "$TOOL" "\$@"
EOF
chmod +x "$BIN_DIR/usage-budget" "$TOOL"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "    NOTE: $BIN_DIR is not on your PATH — add it to use \`usage-budget\` directly" ;;
esac

SKILL_SRC="$SCRIPT_DIR/skill/usage-budget"
SKILL_DST="$HOME/.claude/skills/usage-budget"
if [[ -d "$SKILL_SRC" ]]; then
  echo "==> skill: $SKILL_DST"
  mkdir -p "$HOME/.claude/skills"
  if [[ -L "$SKILL_DST" ]]; then
    ln -sfn "$SKILL_SRC" "$SKILL_DST"            # refresh our own symlink
    echo "    relinked"
  elif [[ -e "$SKILL_DST" ]]; then
    # A real directory here is someone else's file. Never clobber it.
    echo "    SKIPPED: $SKILL_DST already exists and is not a symlink."
    echo "    Move or remove it, then re-run, to install the skill."
  else
    ln -s "$SKILL_SRC" "$SKILL_DST"
    echo "    linked"
  fi
  echo "    (teaches the agent when to act on the injected budget block)"
fi

echo "==> initial scan (cold pass; ~1s for a large history)"
python3 "$TOOL" collect >/dev/null 2>&1 || echo "    (scan found no transcripts yet — fine on a fresh install)"

if [[ "$WIRE_HOOKS" == "1" ]]; then
  if [[ ! -f "$SETTINGS" ]]; then
    echo "==> creating $SETTINGS"
    mkdir -p "$(dirname "$SETTINGS")"
    echo '{}' > "$SETTINGS"
  fi
  echo "==> wiring hooks and the status line into $SETTINGS"
  TOOL="$TOOL" SETTINGS="$SETTINGS" python3 <<'PY'
import json, os, shlex, shutil, sys, time

settings, tool = os.environ["SETTINGS"], os.environ["TOOL"]
inject  = f'python3 {tool} inject'
collect = f'python3 {tool} collect >/dev/null 2>&1'
statusline = f'python3 {tool} statusline'

try:
    with open(settings) as fh:
        cfg = json.load(fh)
except json.JSONDecodeError as e:
    sys.exit(f"    ABORT: {settings} is not valid JSON ({e}). Fix it first — a broken "
             "settings file silently disables every setting in it.")
except FileNotFoundError:
    cfg = {}

hooks = cfg.setdefault("hooks", {})
added = []

def ensure(event, command, entry):
    """Append `entry` to the first matcher-less group for `event`, if absent."""
    groups = hooks.setdefault(event, [])
    for g in groups:
        if any(h.get("command") == command for h in g.get("hooks", [])):
            return False                      # already wired
    for g in groups:
        if "matcher" not in g:                # reuse the general group
            g.setdefault("hooks", []).append(entry)
            return True
    groups.append({"hooks": [entry]})
    return True

if ensure("UserPromptSubmit", inject, {
        "type": "command", "command": inject,
        "timeout": 10, "statusMessage": "Reading usage budget"}):
    added.append("UserPromptSubmit -> inject")

if ensure("Stop", collect, {
        "type": "command", "command": collect,
        "timeout": 8, "async": True, "statusMessage": "Updating usage budget"}):
    added.append("Stop -> collect")

# The status line is where the vendor's own meter arrives (FINDINGS 34). An
# existing status line is wrapped, never replaced: it still renders, and it gets
# the same JSON on stdin after the meter is recorded.
sl = cfg.get("statusLine")
current = sl.get("command") if isinstance(sl, dict) else None
if isinstance(current, str) and "usage_budget" in current and " statusline" in current:
    pass                                      # already wired
elif isinstance(current, str) and current.strip():
    sl["command"] = f"{statusline} -- sh -c {shlex.quote(current)}"
    added.append("statusLine -> statusline (wrapping your existing status line)")
elif sl is not None and not isinstance(sl, dict):
    print("    SKIPPED statusLine: it is not an object, so it was left alone. "
          "Wire `usage-budget statusline` by hand to get the vendor's meter.")
else:
    cfg["statusLine"] = dict(sl or {}, type="command", command=statusline)
    added.append("statusLine -> statusline")

if added:
    # Back up only when we are actually going to write. Creating it earlier meant
    # a same-second re-run regenerated the identical filename and its no-op
    # cleanup deleted the previous run's backup.
    backup = f"{settings}.bak-usage-budget-{time.strftime('%Y%m%d%H%M%S')}"
    n = 1
    while os.path.exists(backup):
        backup = f"{settings}.bak-usage-budget-{time.strftime('%Y%m%d%H%M%S')}-{n}"
        n += 1
    shutil.copy2(settings, backup)

    tmp = settings + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
    json.load(open(tmp))                      # never install unparseable JSON
    os.replace(tmp, settings)
    print("    added: " + ", ".join(added))
    print(f"    backup: {backup}")
else:
    print("    already wired, nothing to do")
PY
else
  echo "==> skipping hooks (--no-hooks)"
fi

cat <<EOF

Installed.

NEXT STEPS

  1. On a Pro or Max plan the status line now records the vendor's own meter
     after Claude's next reply, and the block reports EXACT percentages. Check
     it with:  usage-budget meter

  2. If your plan has usage credits (extra usage) turned on, say so, or every
     cap is forecast as a hard stop:

       usage-budget extra-usage on

  3. Calibrate once, for the turns the meter cannot cover (the first prompt
     after an idle gap, or a plan without the meter). Run /usage in Claude Code
     and feed the numbers back, including the reset times:

       usage-budget calibrate --pct 13 --resets "9:59pm" \\
                              --weekly-pct 26 --weekly-resets "Aug 17 6:59am"

     Add --expires "<date>" if a promotional limit is active.

  4. Check everything:  usage-budget verify

If hooks were just added mid-session, open /hooks once or restart to activate.
EOF
