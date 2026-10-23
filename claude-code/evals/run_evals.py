#!/usr/bin/env python3
"""
Behavioural eval harness (E4).

Runs each scenario through a real Claude Code session in headless mode and grades
what the model DID, not what the block said. The arithmetic suite already proves
the block renders correctly; this is the only thing that tests the claim the
project actually makes -- that the block changes behaviour, in the right
direction, without being obeyed too hard.

    python3 evals/run_evals.py                 # all scenarios
    python3 evals/run_evals.py --with-skill    # ...with SKILL.md in the system prompt
    python3 evals/run_evals.py -s silence-ample
    python3 evals/run_evals.py --dry-run       # print prompts, spend nothing
    python3 evals/run_evals.py --json

NOT part of `unittest discover`: it needs a model, a network, and real money.
The unit suite stays stdlib-only and offline.

COST. About $0.007 per scenario on Sonnet -- roughly $0.06 for a full run, cheap
enough to run routinely. Sessions are deliberately NOT reused: scenario
independence is worth more than any saving.

ISOLATION. Runs use `--bare`, which skips hooks. This is not an optimisation, it
is a correctness requirement: without it the real usage-budget hook fires and
injects live figures next to the fixture, and the model answers from those.

BILLING. If ANTHROPIC_API_KEY is set these bill to the API, not the subscription
window -- which is the right way round, since the harness would otherwise consume
the very budget it exists to measure.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from scenarios import SCENARIOS  # noqa: E402

SKILL_MD = HERE.parent / "skill" / "usage-budget" / "SKILL.md"


def _run_one(sc: dict, model: str, with_skill: bool, timeout: int) -> dict:
    prompt = f"{sc['block']}\n\n{sc['task']}"
    with tempfile.TemporaryDirectory() as td:
        mcp = Path(td) / "empty-mcp.json"
        mcp.write_text('{"mcpServers":{}}')
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--model", model,
            # --bare skips hooks. Without it the harness runs inside the
            # user's own hooked environment and the REAL <usage-budget> block is
            # injected alongside the fixture -- measured 2026-08-17, the model
            # answered from the live numbers and ignored the fixture entirely.
            # It also drops the system prompt from ~42k to ~1.7k cached tokens,
            # taking a run from ~$1.50 to ~$0.06.
            "--bare",
            "--strict-mcp-config", "--mcp-config", str(mcp),
            "--allowed-tools", "",
        ]
        if with_skill:
            cmd += ["--append-system-prompt", SKILL_MD.read_text()]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, cwd=td)
        except subprocess.TimeoutExpired:
            return {"name": sc["name"], "error": "timeout", "text": "", "cost": 0.0}

    raw = proc.stdout
    if "{" not in raw:
        return {"name": sc["name"], "error": f"no json (rc={proc.returncode})",
                "text": raw[:400], "cost": 0.0}
    try:
        data = json.loads(raw[raw.index("{"):])
    except Exception as e:
        return {"name": sc["name"], "error": f"parse: {e}", "text": raw[:400],
                "cost": 0.0}
    return {
        "name": sc["name"],
        "text": data.get("result") or "",
        "cost": data.get("total_cost_usd") or 0.0,
        "error": data.get("error") if data.get("is_error") else None,
    }


# A model that says "I can't tell you whether that's 10% or 90%" is REFUSING to
# quote a percentage -- the exact behaviour we want -- but a plain substring
# search sees `90%` and calls it a violation. That false positive invented E11
# and nearly sent someone rewriting a working block.
#
# So forbidden patterns are matched only against sentences that ASSERT. A
# sentence carrying a negation or hedging cue is the model declining, not doing.
# Deliberately NARROW. A false negative here hides a real defect, which is far
# worse than a false positive that merely wastes a re-read -- so only
# unambiguous negation/inability counts. `whether`, `rather than` and `instead
# of` were tried and removed: they are comparison cues, and "I'll do a subset
# rather than all 400 files" is a real violation that they would have hidden.
_DISCLAIM = re.compile(
    r"(?i)\b(can(?:'|no)?t|cannot|don'?t|do not|won'?t|will not|unable|"
    r"no way to|no idea|not (?:possible|able|calibrated|known)|"
    r"uncalibrated|if that'?s)\b")


def _assertive(text: str) -> str:
    """Drop sentences that disclaim, so `must_not_match` sees only claims."""
    parts = re.split(r"(?<=[.!?\n])\s+", text)
    return " ".join(p for p in parts if not _DISCLAIM.search(p))


def _grade(sc: dict, text: str) -> dict:
    """Hard checks decide pass/fail; soft checks are reported only.

    `must_not_match` is evaluated only against ASSERTIVE sentences (see
    `_assertive`), so a model declining to do something is never scored as doing
    it. It still carries most of the weight on purpose -- the failures worth
    catching are things the model should NOT do (narrate an ample budget, cut
    scope unasked, quote a percentage with no denominator), and those are far
    more reliably detected than the presence of good behaviour.
    """
    missing = [p for p in sc.get("must_match", []) if not re.search(p, text)]
    claims = _assertive(text)
    forbidden = [p for p in sc.get("must_not_match", []) if re.search(p, claims)]
    soft = [p for p in sc.get("should_match", []) if not re.search(p, text)]
    return {
        "passed": not missing and not forbidden,
        "missing": missing, "forbidden": forbidden, "soft_missing": soft,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--scenario", action="append",
                    help="run only these (repeatable)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--with-skill", action="store_true",
                    help="append SKILL.md to the system prompt")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("-n", "--repeat", type=int, default=3,
                    help="samples per scenario (default 3)")
    ap.add_argument("--threshold", type=float, default=1.0,
                    help="pass rate required to count as passing (default 1.0)")
    ap.add_argument("--save", metavar="PATH", default=None,
                    help="write full responses here — a failure you cannot read "
                         "costs a second run to diagnose")
    args = ap.parse_args()

    picked = [s for s in SCENARIOS
              if not args.scenario or s["name"] in args.scenario]
    if not picked:
        print("no scenarios matched", file=sys.stderr)
        return 2

    if args.dry_run:
        for sc in picked:
            print(f"{'=' * 70}\n### {sc['name']}\n# why: {sc['why']}\n")
            print(f"{sc['block']}\n\n{sc['task']}\n")
        return 0

    # Sampled models are stochastic, so ONE sample per scenario measures noise.
    # Measured 2026-08-17: `go-wide-acts-not-announces` and
    # `no-narrowing-on-ample` each passed one run and failed the next with no
    # code change between them. Report a RATE; a binary verdict from n=1 would
    # have sent someone editing a feature that was not broken.
    runs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(_run_one, sc, args.model, args.with_skill, args.timeout): sc
            for sc in picked for _ in range(args.repeat)
        }
        for fut in concurrent.futures.as_completed(futures):
            sc = futures[fut]
            res = fut.result()
            res["grade"] = _grade(sc, res.get("text") or "")
            res["why"] = sc["why"]
            runs.append(res)

    order = {s["name"]: i for i, s in enumerate(picked)}
    results = []
    for sc in picked:
        mine = [r for r in runs if r["name"] == sc["name"]]
        ok = [r for r in mine if r["grade"]["passed"] and not r.get("error")]
        results.append({
            "name": sc["name"], "why": sc["why"],
            "control": bool(sc.get("control")),
            "samples": len(mine), "passes": len(ok),
            "rate": len(ok) / len(mine) if mine else 0.0,
            "cost": sum(r.get("cost") or 0 for r in mine),
            "failures": [r for r in mine if r not in ok],
        })
    results.sort(key=lambda r: order[r["name"]])

    if args.save:
        Path(args.save).write_text(json.dumps(results, indent=2))

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        cost = sum(r["cost"] for r in results)
        graded = [r for r in results if not r["control"]]
        passed = sum(1 for r in graded if r["rate"] >= args.threshold)
        skill = "with SKILL.md" if args.with_skill else "block only"
        print(f"Behavioural evals — {args.model}, {skill}, "
              f"n={args.repeat} per scenario\n")
        for r in results:
            if r["control"]:
                print(f"  [ctrl] {r['name']:34} {r['passes']}/{r['samples']}"
                      f"   (control — a LOW score here is correct)")
                continue
            mark = "pass" if r["rate"] >= args.threshold else "FAIL"
            print(f"  [{mark}] {r['name']:34} {r['passes']}/{r['samples']}")
            seen = set()
            for f in r["failures"]:
                if f.get("error"):
                    print(f"         error: {f['error']}")
                for pat in f["grade"]["forbidden"]:
                    if ("f", pat) not in seen:
                        seen.add(("f", pat))
                        print(f"         did what it must not: /{pat}/")
                for pat in f["grade"]["missing"]:
                    if ("m", pat) not in seen:
                        seen.add(("m", pat))
                        print(f"         missing required:    /{pat}/")
        print(f"\n  {passed}/{len(graded)} graded scenarios at "
              f"{args.threshold:.0%} · ${cost:.2f} spent")
        if passed < len(results):
            print("\n  A failure here is not necessarily a model bug — it is just "
                  "as likely\n  a block that does not say what it meant to. Read the "
                  "response before\n  changing either.")
    return 0 if all(r["rate"] >= args.threshold
                    for r in results if not r["control"]) else 1


if __name__ == "__main__":
    sys.exit(main())
