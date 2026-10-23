"""Build a synthetic usage-budget install so the demo shows REAL tool output
on INVENTED data. Nothing here touches the operator's own transcripts."""
import json, os, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent   # claude-code/
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

ROOT = Path(sys.argv[1])
STATE, PROJ, HOME = ROOT/"state", ROOT/"projects", ROOT/"home"
for d in (STATE, PROJ, HOME/".claude"/"skills"):
    d.mkdir(parents=True, exist_ok=True)

os.environ["USAGE_BUDGET_HOME"] = str(STATE)
os.environ["CLAUDE_PROJECTS_DIR"] = str(PROJ)
from test_usage_budget import turn, write_transcript   # noqa: E402

# A plausible heavy week: spend spread evenly across days so no single day
# looks anomalous, with the most recent stretch inside the current 5h window.
now = time.time()
lines = []
DAYS, PER_DAY = 4, 320
for d in range(DAYS):
    day_start = now - (DAYS - 1 - d) * 86400 - 8 * 3600
    for i in range(PER_DAY):
        # Last day's tail lands inside the live 5h window.
        t = day_start + i * (8 * 3600 / PER_DAY)
        if t > now:
            break
        lines.append(turn(int(t), model="claude-opus-5",
                          inp=1_600 + (i * 41) % 1_100,
                          out=850 + (i * 59) % 1_700,
                          cread=132_000 + (i * 1_900) % 56_000,
                          cw5=11_000 if i % 3 == 0 else 0))
write_transcript(PROJ, "demo-session", lines)

# Wire hooks exactly as install.sh would, so `verify` sees a real install.
tool = str(REPO / "usage_budget.py")
(HOME/".claude"/"settings.json").write_text(json.dumps({"hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command",
        "command": f"python3 {tool} inject"}]}],
    "Stop": [{"hooks": [{"type": "command",
        "command": f"python3 {tool} collect >/dev/null 2>&1"}]}],
}}, indent=2))
(HOME/".claude"/"skills"/"usage-budget").mkdir(exist_ok=True)

env = dict(os.environ, HOME=str(HOME), USAGE_BUDGET_HOME=str(STATE),
           CLAUDE_PROJECTS_DIR=str(PROJ))
env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)   # force the calibrated path, not a live probe

def run(*args, **kw):
    return subprocess.run([sys.executable, tool, *args], capture_output=True,
                          text=True, env=env, timeout=60, **kw)

run("collect")
# Round demo figures. Deliberately NOT the operator's real plan size.
reset = time.strftime("%-I:%M%p", time.localtime(now + 1.6*3600)).lower()
wreset = time.strftime("%b %-d %-I:%M%p", time.localtime(now + 84*3600)).lower()
print(run("calibrate", "--pct", "34", "--resets", reset,
          "--weekly-pct", "41", "--weekly-resets", wreset).stdout[-300:])

def normalise(text: str) -> str:
    """Show the paths a real install prints, not the sandbox's.

    The fixture lives under a scratch directory whose name carries a session
    UUID. That is cosmetic noise at best and a leak at worst, and it is not
    what the tool prints on a real machine -- there it prints the paths below.
    """
    return (text.replace(f"{STATE}", "~/.claude/usage-budget")
                .replace(f"{PROJ}", "~/.claude/projects")
                .replace(f"{HOME}/.claude", "~/.claude")
                .replace(f"{HOME}", "~"))

for name, args in (("verify", ["verify"]), ("status", ["status"]), ("inject", ["inject"])):
    r = run(*args, stdin=subprocess.DEVNULL)
    (ROOT/f"{name}.txt").write_text(normalise(r.stdout))
    print(f"--- {name} (rc={r.returncode}) ---")
    print(r.stdout)
