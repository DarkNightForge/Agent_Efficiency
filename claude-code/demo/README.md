# Demo GIF

Regenerates `docs/demo.gif` — the animation at the top of the root README.

```bash
python3 fixture.py fx     # build a synthetic install, capture real tool output
python3 render.py         # draw the frames, write ../../docs/demo.gif
```

## Why it is built this way

**The data is synthetic, the output is real.** `fixture.py` writes invented
transcripts, wires hooks into a throwaway `settings.json`, and calibrates
against invented percentages — then runs the actual CLI and captures whatever it
prints. Nothing in the GIF is typed by hand, so it cannot drift from what the
tool really does; and nothing in it is anyone's real usage, so publishing it
leaks nothing. That second half matters: the shipped `.gitignore` treats real
usage state as a privacy concern, and a demo recorded from a live session would
walk straight around it.

Paths are rewritten to `~/.claude/...` before rendering. That is what the tool
prints on a real machine — only the sandbox location differs, and the sandbox
path carries a session UUID that has no business in a published artifact.

**Frames are drawn, not recorded.** There is no `vhs` or `asciinema` here, so
`render.py` composes frames with PIL. It buys one thing a recorder does not:
per-frame durations, so typing runs at 45 ms while a settled screen holds for
2.5–4.5 s. A fixed frame rate would either rush the readable parts or triple
the file size.

Frames share a single 16-colour palette. Per-frame adaptive palettes defeat the
GIF optimizer's inter-frame diffing — that alone was 971 KB vs 556 KB.

## Changing it

Edit the `SCENES` list in `render.py` — each entry is
`(command, body, hold_ms, per_line_ms)`. Keep the canvas at 96 columns; wider
text stops being legible once GitHub scales the GIF into a README column.
