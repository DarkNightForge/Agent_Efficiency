# Codex release review preparation

Owner: @Eclipse1231317. Updated November 9, 2026.

PR #8 merged into upstream main at `7fdf061`. The follow-up branch
`robbie/codex-v1-release` stages Codex version `1.0.0` and its release
documentation. The all-provider validation run and final installation test await
the user's go-ahead, followed by a separate go-ahead for publication.
[PR #8](https://github.com/DarkNightForge/Agent_Efficiency/pull/8)

## Proposed release PR title

Prepare Codex v1.0.0 package identity and release documentation

## Proposed release PR body

This prepares the merged Codex implementation for a `1.0.0` release. The core,
manifest and widget version labels agree, version-dependent fixtures target the
new identity, and the payload inventory describes the release package. The
install guide includes hook review, upgrades and rollback. The final smoke test
ties an installed package to its reviewed commit.

The prior developer build passed its Desktop functional checks and PR #8's
Codex CI. The new release package's validation results must be filled in after
the user's step-4 approval.
[Release record and evidence limits](RELEASE-1.0.0.md)

The shared README and security wording correction remains proposed for Eashan
in issue #6, following the user's ownership choice. Independent live Desktop
launcher inspection and the historical Windows permission prompt remain release
decisions. A support report is optional.
[Issue #6](https://github.com/DarkNightForge/Agent_Efficiency/issues/6)

## Before publication

Complete the recorded release decisions and final validation. Inspect actual
checks for the proposed merge commit. The Claude and Hermes remote workflows
filter by provider paths, so report any absent checks separately from the local
three-suite results.
