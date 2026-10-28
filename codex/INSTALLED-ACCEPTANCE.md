# Exact installed-candidate acceptance

Owner: @Eclipse1231317. Prepared October 28, 2026. Results: **not run**.

Candidate: `0.5.0+codex.20261028213407`, on `robbie/public-v1-hardening`.
The runtime baseline is migration commit `a1807d3`; the candidate changes only
the manifest's build identity and adds review/test preparation documents.
[ACCEPTANCE-CANDIDATE.json](ACCEPTANCE-CANDIDATE.json) fingerprints all 17
tracked plugin payload files, including configuration, launchers, UI and skill.
A base `v0.5` label or matching directory name alone is insufficient.

These are installed-host checks, not another unit-test run. Do not replace a
missing installed tool with a source import, synthetic protocol response, fake
hook event, fabricated usage reading or manually completed task record.

## Installation and restart

1. Preserve other work, and use this branch's checkout as the marketplace root.
   Confirm a clean `git status --short`; record its full HEAD. Do not install
   from public main, the old checkout or the September cache.
2. With the desktop app fully closed, register the exact local checkout from
   PowerShell (set `$repoRoot` to its absolute path):

   ```powershell
   codex plugin marketplace add "$repoRoot"
   codex plugin marketplace list
   ```

   Verify the `agent-efficiency` marketplace resolves to this checkout. If an
   existing source with that name resolves elsewhere, stop and report the
   conflict; do not remove unrelated marketplaces or hand-edit global config.
3. Reopen the desktop app, choose that local source in the Plugins Directory,
   and install/update Agent Efficiency. Confirm full candidate version
   `0.5.0+codex.20261028213407`. The full version may require installed metadata
   inspection rather than the display label. If unavailable or rejected, record
   a setup blocker; do not claim installation succeeded.
4. Review and trust this plugin's actual hook commands in the supported hook
   review UI. Installation does not automatically trust hooks. Do not use trust
   bypass flags, alter approval policy or edit cached plugin files.
5. Fully restart the app after installation/trust to unload old skill, hook and
   MCP registries. Open a **new chat** in this checkout after the restart. Attach
   this guide and run Prompt A below. Do not reuse the development chat.

The current local CLI also advertises
`codex plugin add agent-efficiency@agent-efficiency`; Desktop installation is
the documented primary procedure here. A CLI install would still require a
full desktop restart, ordinary hook review and the same payload checks.

Current official guidance:
[local installation and packaging](https://developers.openai.com/plugins/build/plugins#install-a-local-plugin-manually),
[hook trust](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks).
The compatibility manifest remains supported. Lifecycle-hook plugins are for
manual desktop installation and are not eligible for the universal public
directory; GitHub distribution is a separate path.

## Scope and evidence

The test permits normal plugin-managed state writes and delegated authenticated
allowance reads. With telemetry enabled, its background worker may scan local
rollout metadata beyond this chat. A new chat does not isolate that scan. The
agent's direct inspection must stay limited to this candidate, aggregate plugin
state, and this test's own current/child records; do not browse unrelated chats.
Keep any credential/passkey request denied, stop on a new unexpected sensitive
prompt, and report the stage. Do not investigate credential stores or retry to
reproduce it. Passing this test cannot explain a historical permission prompt.

Do not change telemetry or persistent defaults just to make a test pass. If
telemetry is disabled, report measurement checks blocked until the user chooses
to enable it. Saving the already-selected default in the UI is enough to test
that action without changing the user's preference. Do not wipe existing state.

Write the detailed report outside the repository in a user-approved local
artifact location, or return it in chat if no such location is available.
Keep local identifiers/raw usage private. Public summaries use generalized
ratios rather than account capacity or task-volume figures. Each result needs
PASS, FAIL, BLOCKED or NOT OBSERVED with its actual evidence.

## Payload and loaded-runtime checks

Locate the installed root using the host's installed-plugin metadata or loaded
skill path; do not assume the newest cache directory is active. Read only that
candidate directory. Compare its full manifest version and every file against
the inventory. Hash the source too, so an altered working tree cannot masquerade
as the recorded candidate. Inventory hashes describe exact local Windows bytes;
line-ending changes are a mismatch, not permission to silently normalize them.

This PowerShell fragment is inspection only. Set `$repoRoot` and
`$installedRoot` from the verified checkout and host metadata first:

```powershell
$inventory = Get-Content -Raw -LiteralPath (Join-Path $repoRoot 'codex/ACCEPTANCE-CANDIDATE.json') | ConvertFrom-Json
$installedManifest = Get-Content -Raw -LiteralPath (Join-Path $installedRoot '.codex-plugin/plugin.json') | ConvertFrom-Json
if ($installedManifest.version -ne $inventory.version) { throw 'Wrong installed candidate version' }
foreach ($entry in $inventory.files) {
    foreach ($base in @((Join-Path $repoRoot 'plugins/agent-efficiency'), $installedRoot)) {
        $file = Join-Path $base $entry.path
        if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing candidate file: $($entry.path)" }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLowerInvariant() -ne $entry.sha256) {
            throw "Candidate mismatch: $($entry.path)"
        }
    }
}
"Candidate payload matches: $($inventory.files.Count) files"
```

Inspect unexpected files too. Generated Python bytecode is not source evidence;
an additional launcher, configuration, script or unexplained file blocks parity
until understood. Do not delete anything to obtain a clean result. If the
actual installation transforms payload bytes, report that limitation and obtain
a reviewed packaging disposition instead of declaring byte equality.

Disk parity is only one check. Tie the loaded skill, enabled hook commands and
MCP server launcher/working directory to that same installed root using supported
host metadata. Confirm the live server advertises the expected 13 tools and its
UI resource is `ui://widget/agent-efficiency-control-center-v0.5.html`. If the
host hides this connection evidence, mark loaded provenance BLOCKED; a matching
disk directory alone is not a pass. Never claim all 13 tools were individually
executed merely because the tool listing contains them.

## Prompt A — preflight, real UI and measured work

Copy this into the new post-install/post-trust chat with this guide attached:

```text
Run the exact installed Agent Efficiency candidate acceptance described in
codex/INSTALLED-ACCEPTANCE.md. This is testing only: do not fix code, install
anything, publish, bypass permissions, inspect credentials/passkeys, or delete
plugin state. Stop and report any unexpected sensitive permission prompt.

1. Verify the clean checkout/HEAD and candidate
0.5.0+codex.20261028213407 using codex/ACCEPTANCE-CANDIDATE.json. Locate the
loaded installed root from host metadata/skill location and compare all 17
payload files, source and installed. Tie loaded skill, hooks and MCP server to
that root. Do not substitute source CLI calls for installed MCP availability.
Stop dependent tests on parity failure; mark missing runtime provenance BLOCKED.

2. Inspect the real installed MCP tool listing, security status, settings and
aggregate forecast diagnostics. Confirm 13 expected tools, current annotations
(only security status and renderer read-only), delegated/automatic request
disclosures, and no paths/identifiers in security/diagnostic outputs. Record
original preferences without changing them. Do not dump unrelated history.

3. Call the installed refresh_usage and show_control_center for a short
non-sensitive arithmetic task with estimate_scope all, capacity_scope all and
picker_requested true. Do not supply invented rate_limits. A successful refresh
needs a genuine new capture/poll outcome; stale/failed data must stay labeled.
Render the returned result with installed render_control_center and show the
real interactive picker. Verify all three mode descriptions and estimated
time/tokens/capacity or an honest unavailable state, plus privacy disclosures.
Do not claim the UI worked from HTML inspection alone.

4. Tell me to select my current saved default and click Set as default, then
Recalculate, then select a DIFFERENT mode and click Use selected mode. Wait for
these user actions. On the widget follow-up, continue this same acceptance:
confirm the one-time token was consumed by the real prompt hook, the selected
mode applies to that follow-up, and the saved default stayed unchanged. Inspect
settings after the save; do not simulate widget clicks with direct MCP calls.
If the UI or follow-up fails, preserve that failure rather than repairing it.

5. Do bounded read-only work to generate genuine hook evidence: evaluate
17*19 with one shell tool call. I authorize one small read-only subagent, if
supported, to calculate 23*29 independently without tools or file access. Check
only this test's records for counters and parent/child separation; absent host
features are BLOCKED/NOT OBSERVED. Do not force compaction or run expensive work.
Never manually call start_task/complete_task or inject hook events to replace
automatic measurements. A Stop hook runs after the response ends, so leave
completion/token reconciliation verification for Prompt B.

6. Record an aggregate poll-health/capture baseline after the explicit/UI
refreshes. End the turn and tell me when to send Prompt B: at least five minutes
later, keeping this chat and MCP connection open, without pressing Recalculate
or calling explicit refresh again. Do not poll tools repeatedly or silently
wait in one long blocking call.

Use PASS/FAIL/BLOCKED/NOT OBSERVED per check, distinguish observed behavior from
source expectations, and save private evidence outside the repo. Do not claim
the test complete until the UI follow-up and Prompt B have been reviewed.
```

## User actions and the second turn

Use the actual picker, not another text selection. First choose the currently
saved default, save it, and recalculate. Then choose a different mode and use it
for one task. The automatic follow-up should resume the test. A later ordinary
prompt should return to the saved default rather than reuse that one-time token.

After the test agent ends its work turn, leave that chat open for at least five
minutes, then paste Prompt B. Hooks for Prompt B may themselves refresh usage:
poll timestamps alone cannot prove that the background worker ran. The agent
must use worker-attributable evidence or mark cadence NOT OBSERVED.

## Prompt B — completion, worker evidence and final report

```text
Finish the same installed candidate acceptance from Prompt A. At least five
minutes have elapsed with this chat open. Continue testing only; do not fix,
reinstall, publish, inspect credentials, or delete/repair state.

Recheck candidate identity and provenance if the host changed. Verify the
previous actual Stop hook completed this test's turn after its response ended.
Inspect only this test's scoped numeric measurement metadata/current-rollout
usage records; exclude conversation bodies and unrelated records from reports.
Verify per-model-call accounting (do not sum cumulative totals), parent/child
separation, counters and completion/audit availability without inventing values.
Use an installed reconciliation call only when needed for late metadata and
identify that as reconciliation, not proof the Stop hook ran. Missing hook
evidence stays BLOCKED/FAIL as appropriate; do not manually complete the task.

Check whether the long-lived MCP worker refreshed automatically across the
idle interval. Distinguish worker evidence from SessionStart/UserPromptSubmit,
explicit refresh and UI refresh. Without attributable evidence mark cadence
NOT OBSERVED, even if the snapshot is fresh. Keep stale/failed data honest.
Confirm this ordinary prompt uses the saved default rather than the consumed
one-time mode, and verify preferences stayed unchanged.

Confirm security status/diagnostics contain aggregate data rather than local
paths/identifiers and no source files changed. Do not claim a full filesystem
audit from a clean git status. Compaction, SessionEnd and truncation handling
not exercised live must be NOT OBSERVED, with synthetic coverage separate.

Return a table of PASS/FAIL/BLOCKED/NOT OBSERVED for installation/loaded parity,
13-tool discovery/annotations, real UI/save/recalculate/follow-up, hook
activation/completion, scoped tokens and parent/child attribution, automatic
polling evidence, privacy outputs, default persistence and repository cleanliness.
List bugs and missing evidence separately, indicate whether the installed
acceptance gate passed, and name remaining review/CI/shared-doc/permission-
prompt gates. A clean test cannot establish the cause of the old Windows prompt.
Provide the private report's location if saved. Do not post raw account usage,
local identifiers or transcripts publicly, and do not modify plugin source.
```

An unavailable picker, wrong loaded version, missing tools, untrusted/skipped
hooks, disabled measurement or missing attribution evidence is an incomplete
gate, not a successful fallback. No result here authorizes merging or release.
An optional user-initiated support report remains available for the historical
permission prompt; drafting or submitting one is not part of this test.
