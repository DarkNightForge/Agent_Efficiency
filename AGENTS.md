# Agent instructions — Agent_Efficiency

Two people build this repo in parallel, each with their own coding agent:

| GitHub | Runs on | Owns |
|---|---|---|
| `@DarkNightForge` | Claude Code | `claude-code/` + `hermes/` |
| `@Eclipse1231317` | Codex | `codex/` + `plugins/agent-efficiency/` |

That split is not arbitrary. **Whoever's agent runs on a provider owns that provider's implementation**, because only that agent can empirically verify the provider's own behaviour. Do not cross the line just because a change looks small.

**Before editing any file, check the ownership table below.** If the file is not yours, do not edit it — open an issue or comment on the relevant PR instead.

---

## 1. File ownership

### Hard-owned — edit freely, no coordination needed

| Path | Owner |
|---|---|
| `claude-code/**` | `@DarkNightForge` |
| `hermes/**` | `@DarkNightForge` |
| `.github/workflows/claude-code-tests.yml` | `@DarkNightForge` |
| `.github/workflows/hermes-tests.yml` | `@DarkNightForge` |
| `codex/**` | `@Eclipse1231317` |
| `plugins/agent-efficiency/**` | `@Eclipse1231317` |
| `.agents/plugins/marketplace.json` | `@Eclipse1231317` |
| `.github/workflows/codex-tests.yml` | `@Eclipse1231317` |

### Shared — coordinate before editing

| Path | Rule |
|---|---|
| `docs/FINDINGS.md` | Append-only by section. See §2. |
| `docs/PORTING.md` | Claim it in the issue you're working before editing. |
| `README.md` (root) | Claim it. Each half has its own README — prefer editing that instead. |
| `AGENTS.md`, `CLAUDE.md` | Either may edit; keep changes small and mention them in your PR body. |
| `.gitignore`, `LICENSE` | Rare. Mention in the PR. |

**This is where every conflict so far has actually happened** — not in code. One PR needed its merge *and* its rebase tested locally beforehand because both sides had edited the same READMEs, and another existed purely to fix doc drift. The code halves have never collided. Treat the shared docs as the risky surface and the code as the safe one.

---

## 2. `docs/FINDINGS.md` — append, don't rewrite

This is the most valuable file in the repo and the most contended. Rules:

1. **New findings get a new section. Never renumber or restructure existing ones.** Sections are numbered `1 … 10` with letter suffixes for follow-ups (`4b`, `4c`, `6b`, `6c`, `6d`, `9b`).
2. **Reserved ranges, so two people can add findings the same day without touching the same lines:**
   - **`@DarkNightForge` → `11` and upward.**
   - **`@Eclipse1231317` → `9c`, `9d`, … (extending the Codex section §9b).**
3. **Superseding a finding: mark it, don't delete it.** Add a `⚠️ SUPERSEDED <date>` banner plus a says/reality table, and leave the original text below. §4 and §6d are the worked examples — follow their shape.
4. **Correcting a finding you own is fine. Correcting one you don't own goes through an issue.**
5. Accurate historical narrative stays. Only present-tense claims that would mislead someone acting today are stale.

---

## 3. Git protocol

- **Branch naming:** `eash/<topic>` and `robbie/<topic>`. Never push to `main`.
- **Rebase before you push**, every time: `git fetch origin && git rebase origin/main`. Both merge conflicts to date came from branches that had fallen behind.
- **Small PRs, merged quickly.** A long-lived branch touching shared docs is the failure mode.
- **Rebase-merge or squash-merge only** — keep history linear.
- **Run all three suites before opening a PR:**
  ```bash
  cd claude-code && python -m unittest discover -s tests
  cd ../codex    && python -m unittest discover -s . -p "test_*.py"
  cd ..          && python -m unittest discover -s hermes/tests -p "test_*.py"
  ```
  All three must be green regardless of which half you changed — there are three
  suites and three CI workflows, not two. Currently **462 tests** (365 claude-code
  + 71 codex + 26 hermes), plus 12 behavioural evals (`claude-code/evals/`, not in
  the unit run).
- Deleting a rebase-merged branch: `git branch -d` will refuse it, because the replayed commits have new hashes and the tip is no longer an ancestor. Verify with `git cherry main <branch>` — a `-` on every line means the content is upstream and `-D` is safe.

---

## 4. Project conventions

- **Measure before modelling.** This project has retracted findings that were fitted to a single data point (§6b → §6d). If something is a hypothesis, label it one.
- **Repo-facing naming:** use `@Eclipse1231317` in anything committed or public.
- **Account-level figures get generalised to ratios before they land in the repo.** Ratios, percentages and single-session cost-formula validations (such as the to-the-cent matches against `/usage` in `docs/FINDINGS.md`) stay verbatim; capacity, per-turn and working-volume figures become ratios, and example output blocks use illustrative round figures. **This applies to issues too**, since they become public with the repo.
- `/usage` is authoritative. Local figures are a reconstruction.
