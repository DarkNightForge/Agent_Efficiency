"""Render a terminal-style demo GIF from REAL usage-budget output.

There is no recorder on this box (no vhs, no asciinema), so frames are drawn
directly with PIL. Every line rendered below is captured verbatim from the tool
running against a SYNTHETIC fixture -- invented transcripts, invented plan size
-- so the demo never ships anyone's real usage figures.
"""
import textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

DEMO = Path(__file__).resolve().parent
FX = DEMO / "fx"
OUT = DEMO.parent.parent / "docs" / "demo.gif"

COLS, SIZE, PAD = 96, 15, 18
BG, FG = "#0d1117", "#c9d1d9"
GREEN, YELLOW, RED, BLUE, GREY, WHITE = (
    "#3fb950", "#d29922", "#f85149", "#58a6ff", "#6e7681", "#f0f6fc")

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
REG = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono.ttf", SIZE)
BOLD = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", SIZE)
CW = REG.getbbox("M")[2] - REG.getbbox("M")[0]
CH = SIZE + 7


def colour(line: str) -> str:
    s = line.strip()
    if s.startswith("$"):                                   return WHITE
    if "[PASS]" in line:                                    return GREEN
    if "[FAIL]" in line:                                    return RED
    if "[WARN]" in line:                                    return YELLOW
    if s.startswith(("READY", "GO WIDE")):                  return GREEN
    if s.startswith("NOT READY"):                           return RED
    if s.startswith(("<usage-budget>", "</usage-budget>")): return GREY
    if s.startswith("#"):                                   return GREY
    if s.startswith(("Affordance:", "Week pace:")):         return BLUE
    if s.startswith("verdict"):                             return GREEN
    return FG


def wrap(lines):
    """Wrap to COLS, carrying each line's colour onto its continuations.

    A continuation of an "Affordance:" line is still that line; letting it
    revert to body colour makes one block read as two.
    """
    out = []
    for ln in lines:
        parent = colour(ln)
        if len(ln) <= COLS:
            out.append((ln, parent))
            continue
        for piece in textwrap.wrap(ln, width=COLS, subsequent_indent="    ",
                                   break_long_words=False,
                                   break_on_hyphens=False) or [""]:
            out.append((piece, parent))
    return out


def read(name, keep=None):
    lines = [l.rstrip() for l in (FX / f"{name}.txt").read_text().splitlines()]
    if keep:
        lines = [l for l in lines if any(k in l for k in keep)]
    return wrap([l for l in lines if l.strip()])


verify = read("verify")
inject = wrap(["# what gets prepended to every prompt your agent sees", ""]) + read("inject")
status = wrap(["Claude usage budget (list-price-weighted estimate)", ""]) + read(
    "status", keep=("5h window spend", "5h capacity", "5h used", "5h remaining",
                    "window resets", "burn rate", "week used", "week remaining",
                    "median turn", "turns left", "fan-out cost", "fan-outs left",
                    "verdict"))

SCENES = [("usage-budget verify", verify, 2400, 110),
          ("usage-budget status", status, 3000, 95),
          ("usage-budget inject", inject, 4400, 130)]

# Size the canvas to the tallest scene: command + blank + body, plus one
# trailing blank so the final frame does not look clipped.
ROWS = max(len(body) for _, body, _, _ in SCENES) + 3
W, H = COLS * CW + 2 * PAD, ROWS * CH + 2 * PAD

frames, delays = [], []


def draw(view, cursor=False):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for i, (ln, c) in enumerate(view[-ROWS:]):
        head = ln.strip()
        f = BOLD if head.startswith(("$", "READY", "NOT READY", "GO WIDE")) else REG
        d.text((PAD, PAD + i * CH), ln, font=f, fill=c)
    if cursor and view:
        last, _ = view[-1]
        y = PAD + (len(view[-ROWS:]) - 1) * CH
        d.rectangle([PAD + len(last) * CW, y + 2,
                     PAD + (len(last) + 1) * CW - 1, y + CH - 3], fill=BLUE)
    return img


def add(view, ms, cursor=False):
    frames.append(draw(view, cursor))
    delays.append(ms)


for prompt, body, hold, per_line in SCENES:
    screen = []
    for i in range(2, len(prompt) + 1, 2):          # type two chars per frame
        add(screen + [("$ " + prompt[:i], WHITE)], 45, cursor=True)
    add(screen + [("$ " + prompt, WHITE)], 420, cursor=True)
    screen += [("$ " + prompt, WHITE), ("", FG)]
    for ln in body:
        screen.append(ln)
        add(screen, per_line)
    add(screen, hold)

# The page uses ~8 colours; saving at full depth triples the file for nothing.
# Quantise every frame against ONE shared palette so inter-frame diffs stay
# small -- per-frame adaptive palettes defeat the optimizer.
pal = frames[0].quantize(colors=16, method=Image.MEDIANCUT)
frames = [f.quantize(colors=16, palette=pal, dither=Image.Dither.NONE)
          for f in frames]
frames[0].save(OUT, save_all=True, append_images=frames[1:], duration=delays,
               loop=0, optimize=True, disposal=2)
print(f"{OUT.name}  {len(frames)} frames  {sum(delays)/1000:.1f}s  "
      f"{OUT.stat().st_size/1024:.0f} KB  {W}x{H}")
