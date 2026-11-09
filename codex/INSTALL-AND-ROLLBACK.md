# Install and roll back the Codex plugin

Target package version: `1.0.0`. Release validation is pending.

## Install or upgrade

Have Git, Python 3 and a Codex Desktop build with plugin support. Use a source
checkout outside a synced directory. During release testing, use the approved
release branch commit. After publication, use the published tag and confirm
that its commit matches the release notes.

Fully quit Desktop. In a shell, list the configured marketplaces and verify the
Agent Efficiency source. For a first install, register the absolute release
checkout root. For an upgrade from the same source root, keep the registration
and install the updated package.

~~~text
codex plugin marketplace list
codex plugin marketplace add "<absolute release checkout path>"
codex plugin marketplace list
codex plugin add agent-efficiency@agent-efficiency --json
~~~

Record the full installed version and cache path returned by the installer.
If `codex` is unavailable on PATH in PowerShell, use the verified full path to
the active Desktop build's bundled executable, for example
`& $codexExe plugin marketplace list`.
[OpenAI local installation instructions](https://developers.openai.com/plugins/build/plugins#install-a-local-plugin-manually)

If the existing marketplace points to another checkout, first record its root
and installed full version. To switch that one entry, run
`codex plugin marketplace remove agent-efficiency`, then add the selected release
checkout and verify the list before installing.

## Review hooks

Open Desktop and inspect Agent Efficiency's installed hook commands in its
Hooks panel or Settings > Hooks. Trust the hooks you want Codex to run.
The CLI also provides `/hooks`. Fully restart Desktop after a CLI install,
then start a fresh chat after the hook review.
[Codex hook trust](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)

Ask to open the Agent Efficiency control center. Recalculate appears next to
the Calculate dropdown above the mode cards. Use selected mode and Set as
default appear below the cards.

The plugin delegates authenticated allowance reads to Codex, including automatic
refresh. Turning telemetry off stops task measurement and rollout scanning,
while allowance refresh continues.
[Privacy and retained data](PRIVACY.md)

## Roll back

Disable Agent Efficiency in Desktop and fully quit the app. Use a trusted prior
checkout or published release whose full version you recorded. Point the
marketplace at that source using the source-switch steps above and reinstall.
Reopen Desktop, review its hooks, and verify the loaded version and saved
default in a fresh chat.

Preserve the existing state directory and checkouts during rollback. If the
older package's compatibility with that state is uncertain, keep the plugin
disabled until it is verified. Report sensitive problems through the repository's
private security channel.
