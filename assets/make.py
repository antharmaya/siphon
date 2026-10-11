"""Build siphon's logo and its motion states from one geometry.  Run: python assets/make.py

The mark: a complete golden spiral (growth φ per quarter turn), 2.4 turns, whose stroke holds
its weight on the outer turn and then sharpens to a point as it winds into the centre, the way a
pencil is sharpened. One colour. The point is where a download lands.

The mark is alive: every state is the same body, its geometry recomputed per frame.
  mark-animated / lockup-animated   idle: breathes (winds a little tighter, relaxes); the tail slithers
  states/resolving.svg              finding: winds itself into a deep sink and back out
  states/downloading.svg            downloading: swallows; a bulge travels down the body into the centre
  states/done.svg                   saved: a last gulp reaches the centre, the tail curls once, a ripple
  states/error.svg                  failed: recoils, uncoils and goes limp, in the error colour
Every state has a still copy for prefers-reduced-motion.

Small sizes (≤32 px) use a heavier cut of the same spiral. Colours: Antharmaya Labs tokens
(labs/antharmaya-labs/DESIGN.md) — paper #f9f7f4, warm ink #241c17, ink blue #1256b2
(dark #6da5f8), drench #0c172a. Wordmark: Bricolage Grotesque (SIL OFL 1.1) at wght 700, opsz 96,
Antharmaya Labs' own display face; its slight stroke contrast and ink-trapped joins rhyme with the
spiral's taper. Outlined to wordmark.path (GitHub won't load fonts inside an <img> SVG).
"""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
WEB = ROOT / "src/siphon/web"

PAPER, INK, BLUE, BLUE_DARK, FG_DARK, DRENCH = "#f9f7f4", "#241c17", "#1256b2", "#6da5f8", "#eeece9", "#0c172a"
BAD, BAD_DARK = "#a2362c", "#f08a7e"
GOLDEN_K = math.log((1 + 5 ** 0.5) / 2) / (math.pi / 2)

WORD_D = (HERE / "wordmark.path").read_text().strip()
WORD_ADVANCE, WORD_XHEIGHT = 2968, 525
WORD_SCALE = 22 / WORD_XHEIGHT  # x-height spans y 24 → 46


def fmt(p):
    return f"{p[0]:.2f} {p[1]:.2f}"


class Spiral:
    """The golden spiral, sharpened, fitted into a box. All coordinates are baked (no transforms),
    so CSS motion paths and masks line up with the drawn shape exactly."""

    def __init__(self, W=6.6, turns=2.4, taper=.5, ease=.8, phi0=-160, n=700, box=(4, 4, 60, 60), about_eye=False):
        R = 26.5
        th_end = turns * 2 * math.pi
        raw = []
        for i in range(n + 1):
            th = th_end * i / n
            r = R * math.exp(-GOLDEN_K * th)
            a = math.radians(phi0) + th  # clockwise on screen as it shrinks
            raw.append((r * math.cos(a), r * math.sin(a)))
        s = [0.0]
        for i in range(n):
            s.append(s[-1] + math.dist(raw[i], raw[i + 1]))
        total, Lt = s[-1], s[-1] * taper

        def width(si):
            left = total - si
            return W if left >= Lt else W * (left / Lt) ** ease

        left, right = [], []
        for i, (x, y) in enumerate(raw):
            j0, j1 = max(0, i - 1), min(n, i + 1)
            tx, ty = raw[j1][0] - raw[j0][0], raw[j1][1] - raw[j0][1]
            tl = math.hypot(tx, ty) or 1
            tx, ty = tx / tl, ty / tl
            w = width(s[i]) / 2
            left.append((x - ty * w, y + tx * w))
            right.append((x + ty * w, y - tx * w))
        # fit the ink (outline + the round outer cap) into the box, centred
        cap = (raw[0][0], raw[0][1], W / 2)
        xs = [p[0] for p in left + right] + [cap[0] - cap[2], cap[0] + cap[2]]
        ys = [p[1] for p in left + right] + [cap[1] - cap[2], cap[1] + cap[2]]
        bw, bh = max(xs) - min(xs), max(ys) - min(ys)
        if about_eye:  # spiral centre at the box centre, sized so a full turn never leaves the box
            reach = max(math.hypot(x, y) for x, y in zip(xs, ys))
            k = ((box[2] - box[0]) / 2) / reach
            ox, oy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        else:
            k = min((box[2] - box[0]) / bw, (box[3] - box[1]) / bh)
            ox = box[0] + ((box[2] - box[0]) - bw * k) / 2 - min(xs) * k
            oy = box[1] + ((box[3] - box[1]) - bh * k) / 2 - min(ys) * k
        T = lambda p: (ox + p[0] * k, oy + p[1] * k)
        self.fit = (k, ox, oy)
        self.k = k
        self.W = W * k
        self.centre = T((0.0, 0.0))
        self.cap = (*T(cap[:2]), cap[2] * k)
        self.line = [T(p) for p in raw]
        self.outline = "M" + "L".join(fmt(T(p)) for p in left) + "L" + "L".join(fmt(T(p)) for p in reversed(right)) + "Z"
        self.tip = self.line[-1]

    def body(self, cls="ink") -> str:
        cx, cy, r = self.cap
        return f'<path class="{cls}" d="{self.outline}"/><circle class="{cls}" cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}"/>'

    def centreline(self, step=4) -> str:
        pts = self.line[::step] + [self.line[-1]]
        return "M" + "L".join(fmt(p) for p in pts)


MARK = Spiral()
SMALL = Spiral(W=9.5, taper=.5, turns=2.2)
EYE = Spiral(about_eye=True, box=(3, 3, 61, 61))  # states turn about the eye, so they're placed by it


# ------------------------------------------------------------------ themes and motion

def theme(mode: str, extra_light: str = "", extra_dark: str = "") -> str:
    """auto follows prefers-color-scheme; light/dark are fixed (for GitHub's <picture>, since an
    SVG's own media query follows the OS, not GitHub's theme setting)."""
    light = f".ink{{fill:{BLUE}}} .word{{fill:{INK}}} .ghost{{fill:{BLUE};opacity:.14}} .bad{{fill:{BAD}}} .rip{{stroke:{BLUE}}} .chev{{stroke:{BLUE}}}{extra_light}"
    dark = f".ink{{fill:{BLUE_DARK}}} .word{{fill:{FG_DARK}}} .ghost{{fill:{BLUE_DARK};opacity:.16}} .bad{{fill:{BAD_DARK}}} .rip{{stroke:{BLUE_DARK}}} .chev{{stroke:{BLUE_DARK}}}{extra_dark}"
    if mode == "auto":
        return f"\n  {light}\n  @media (prefers-color-scheme: dark) {{ {dark} }}"
    return "\n  " + (light if mode == "light" else dark)


STILL = "\n  @media (prefers-reduced-motion: reduce) { * { animation: none !important; } }"


# ------------------------------------------------------------------ the living mark
# Motion is the body itself, not decoration on it: every frame is the same spiral outline,
# recomputed with a few parameters changed (how far it winds, a travelling wiggle in the tail,
# a bulge moving down the body). Frames share one point count so SVG <animate> can morph the
# path; that works in every browser and inside <img>, where CSS path morphing doesn't (Safari).

def pose(turns=2.4, W=6.6, taper=.5, ease=.8, phi0=-160, wiggle=0.0, phase=0.0, waves=1.6,
         bulge_at=None, bulge=0.0, sigma=.07, curl=0.0, n=120, fit=None) -> str:
    """One frame of the outline. wiggle (grid units) grows toward the tip and travels with
    `phase`; bulge_at (0..1 of the length) swells the body by `bulge`; curl adds extra wind at
    the very tip."""
    k, ox, oy = fit or MARK.fit
    R = 26.5
    th_end = turns * 2 * math.pi
    raw = []
    for i in range(n + 1):  # uniform in angle: the tight inner coils get their share of points
        th = th_end * i / n
        r = R * math.exp(-GOLDEN_K * th)
        a = math.radians(phi0) + th + curl * (i / n) ** 6
        raw.append((r * math.cos(a), r * math.sin(a)))
    s = [0.0]
    for i in range(n):
        s.append(s[-1] + math.dist(raw[i], raw[i + 1]))
    total = s[-1]
    t0 = total * (1 - taper)
    left, right = [], []
    for i, (x, y) in enumerate(raw):
        j0, j1 = max(0, i - 1), min(n, i + 1)
        tx, ty = raw[j1][0] - raw[j0][0], raw[j1][1] - raw[j0][1]
        tl = math.hypot(tx, ty) or 1
        tx, ty = tx / tl, ty / tl
        nx, ny = -ty, tx
        rest = total - s[i]
        w = W if rest >= total * taper else W * (rest / (total * taper)) ** ease
        if bulge_at is not None:
            w *= 1 + bulge * math.exp(-(((s[i] / total) - bulge_at) / sigma) ** 2)
        u = max(0.0, (s[i] - t0) / (total - t0))  # 0 where the taper starts, 1 at the tip
        env = u * u * (3 - 2 * u)
        off = wiggle / k * env * math.sin(2 * math.pi * (waves * u - phase))
        cx, cy = x + nx * off, y + ny * off
        left.append((cx + nx * w / 2, cy + ny * w / 2))
        right.append((cx - nx * w / 2, cy - ny * w / 2))
    # round outer cap as part of the outline: from the right edge, round the back, to the left edge
    (x0, y0), (x1, y1) = raw[0], raw[1]
    bx, by = x0 - x1, y0 - y1
    bl = math.hypot(bx, by) or 1
    bx, by = bx / bl, by / bl
    nx, ny = by, -bx  # normal on the left side at the start
    cap = []
    for j in range(1, 8):
        ang = math.pi * j / 8
        # sweep from -n (right) through the back direction to +n (left)
        cxv = -nx * math.cos(ang) + bx * math.sin(ang)
        cyv = -ny * math.cos(ang) + by * math.sin(ang)
        cap.append((x0 + cxv * W / 2, y0 + cyv * W / 2))
    pts = [(ox + x * k, oy + y * k) for x, y in left + right[::-1] + cap]
    # quadratic smoothing through midpoints: smooth, compact, same command count every frame
    d = f"M{pts[0][0]:.1f} {pts[0][1]:.1f}"
    for a, b in zip(pts[1:-1], pts[2:]):
        d += f"Q{a[0]:.1f} {a[1]:.1f} {(a[0] + b[0]) / 2:.1f} {(a[1] + b[1]) / 2:.1f}"
    return d + f"L{pts[-1][0]:.1f} {pts[-1][1]:.1f}Z"


def alive(frames: list[str], dur: float, once: bool = False, cls: str = "ink", still: str | None = None) -> str:
    """A morphing body (its outline includes the round cap) plus a still copy for reduced motion."""
    vals = ";".join(frames if once else frames + frames[:1])
    rep = 'repeatCount="1" fill="freeze"' if once else 'repeatCount="indefinite"'
    return (f'<g class="alive"><path class="{cls}" d="{frames[0]}"><animate attributeName="d" dur="{dur}s" {rep} '
            f'calcMode="spline" keySplines="{";".join([".45 0 .55 1"] * (len(frames) - (1 if once else 0)))}" values="{vals}"/></path></g>'
            f'<g class="still"><path class="{cls}" d="{still or frames[0]}"/></g>')


ALIVE_CSS = """
  .still { display: none; }
  @media (prefers-reduced-motion: reduce) { .alive { display: none; } .still { display: inline; } }"""


def frames_idle(n=20):
    """Idle: breathes (winds a little tighter, relaxes) while the tail slithers like a worm's."""
    return [pose(turns=2.4 + .12 * math.sin(2 * math.pi * i / n), wiggle=2.2, waves=1.4, phase=2 * i / n, taper=.55)
            for i in range(n)]


def frames_sink(n=24):
    """Finding: swirls down the drain — turns about its eye while winding deeper and back out."""
    E = EYE.fit
    return [pose(fit=E, phi0=-160 + 360 * i / n, turns=2.2 + .6 * (.5 - .5 * math.cos(2 * math.pi * i / n)),
                 wiggle=1.6, phase=3 * i / n, taper=.55) for i in range(n)]


def frames_swallow(n=18):
    """Downloading: swallows — a bulge travels down the body into the centre; the tail keeps moving."""
    E = EYE.fit
    return [pose(fit=E, bulge_at=.03 + .95 * i / n, bulge=.6, wiggle=1.4, phase=2 * i / n) for i in range(n)]


def frames_done(n=14):
    """Saved: a last gulp reaches the centre, the tail curls tight once, then rest."""
    E, out = EYE.fit, []
    for i in range(n + 1):
        t = i / n
        gulp = .03 + .95 * min(1, t / .6)
        curl = 2.2 * math.sin(math.pi * (t - .55) / .45) if t > .55 else 0
        out.append(pose(fit=E, bulge_at=gulp, bulge=.55 * (1 - max(0, (t - .6) / .4)), curl=curl))
    return out


def frames_error(n=14):
    """Failed: recoils — uncoils, droops and goes limp."""
    E, out = EYE.fit, []
    for i in range(n + 1):
        t = i / n
        e = 1 - (1 - t) ** 3
        out.append(pose(fit=E, turns=2.4 - .6 * e, phi0=-160 + 28 * e, wiggle=2.4 * (1 - e) * math.sin(7 * t), phase=t))
    return out


def svg(w: float, h: int, body: str, css: str, title: str = "siphon") -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h}" width="{w * 4:.0f}" height="{h * 4}" '
            f'role="img" aria-label="{title}"><title>{title}</title><style>{css}\n</style>{body}</svg>\n')


def word(x: float) -> str:
    return f'<path class="word" transform="translate({x:.2f} 46) scale({WORD_SCALE:.6f})" d="{WORD_D}"/>'


def states() -> dict[str, str]:
    """The product's states, all the same living body."""
    tx, ty = EYE.tip
    T = theme("auto") + ALIVE_CSS
    limp = pose(fit=EYE.fit, turns=2.4 - .6, phi0=-160 + 28)
    return {
        "resolving": svg(64, 64, alive(frames_sink(), 2.8), T, "finding"),
        "downloading": svg(64, 64, alive(frames_swallow(), 1.8), T, "downloading"),
        "done": svg(64, 64, alive(frames_done(), 1.3, once=True) +
                    f'<circle class="rip" cx="{tx:.2f}" cy="{ty:.2f}" r="3" fill="none" stroke-width="1.6"/>', T + """
  .rip { opacity: 0; transform-box: fill-box; transform-origin: center; animation: rip .8s 1.05s cubic-bezier(.22,1,.36,1) forwards; }
  @keyframes rip { 0% { opacity: .9; transform: scale(.4); } 100% { opacity: 0; transform: scale(3.4); } }
  @media (prefers-reduced-motion: reduce) { .rip { animation: none; } }""", "saved"),
        "error": svg(64, 64, alive(frames_error(), 1.1, once=True, cls="bad", still=limp), T, "failed"),
    }


def icon(sp: Spiral, scale: float) -> str:
    """App / extension icon: the mark in light ink on a drenched square (presentation attributes,
    so rasterisers that skip CSS still draw it)."""
    cx, cy, r = sp.cap
    inner = (f'<path d="{sp.outline}" fill="{BLUE_DARK}"/>'
             f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="{BLUE_DARK}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="512" height="512" role="img" aria-label="siphon">'
            f'<rect width="64" height="64" rx="14" fill="{DRENCH}"/>'
            f'<g transform="translate(32 32) scale({scale}) translate(-32 -32)">{inner}</g></svg>\n')


def main() -> None:
    word_x = 74
    lock_w = word_x + WORD_ADVANCE * WORD_SCALE + 6
    idle = alive(frames_idle(), 3.2)
    out = {
        "mark.svg": svg(64, 64, MARK.body(), theme("auto")),
        "mark-animated.svg": svg(64, 64, idle, theme("auto") + ALIVE_CSS),
        "lockup.svg": svg(lock_w, 64, MARK.body() + word(word_x), theme("auto")),
        "icon.svg": icon(MARK, .82),
        "icon-small.svg": icon(SMALL, .9),
    }
    for mode in ("light", "dark"):  # README hero, chosen by <picture>
        out[f"lockup-{mode}.svg"] = svg(lock_w, 64, MARK.body() + word(word_x), theme(mode))
        out[f"lockup-animated-{mode}.svg"] = svg(lock_w, 64, idle + word(word_x), theme(mode) + ALIVE_CSS)
    for name, text in out.items():
        (HERE / name).write_text(text)
    (HERE / "states").mkdir(exist_ok=True)
    (WEB / "states").mkdir(parents=True, exist_ok=True)
    for name, text in states().items():
        (HERE / "states" / f"{name}.svg").write_text(text)
        (WEB / "states" / f"{name}.svg").write_text(text)
    (WEB / "mark.svg").write_text(out["mark.svg"])
    (WEB / "lockup.svg").write_text(out["lockup.svg"])
    # The extension's button is always dark, so its states use the fixed dark palette.
    ext = ROOT / "extension/states"
    ext.mkdir(exist_ok=True)
    T = theme("dark") + ALIVE_CSS
    ext_states = {"idle": svg(64, 64, alive(frames_idle(), 3.2), T)}
    for name, text in states().items():
        ext_states[name] = text.replace(theme("auto"), theme("dark"))
    for name, text in ext_states.items():
        (ext / f"{name}.svg").write_text(text)
    if rsvg := shutil.which("rsvg-convert"):
        targets = {ROOT / "extension/icons/16.png": 16, ROOT / "extension/icons/48.png": 48,
                   ROOT / "extension/icons/128.png": 128, HERE / "icon-192.png": 192, HERE / "icon-512.png": 512,
                   WEB / "icon-192.png": 192, WEB / "icon-512.png": 512}
        for png, size in targets.items():
            src = HERE / ("icon-small.svg" if size <= 32 else "icon.svg")
            subprocess.run([rsvg, "-w", str(size), "-h", str(size), str(src), "-o", str(png)], check=True)
    print("built:", ", ".join(out), "+ states/", "+ PNG icons" if rsvg else "(no rsvg-convert: PNGs skipped)")


if __name__ == "__main__":
    main()
