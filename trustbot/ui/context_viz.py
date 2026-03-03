"""
Real-time context pipeline visualization.

Generates an animated SVG showing the 3-column architecture:
  Left:   Producer agents feeding raw context (Code, Deps, Docs, Style, Changes, Issues)
  Center: Semantic knowledge graph (Neo4j) with active file labels
  Right:  Consumer agents pulling curated context (Completions, Code Review, Agents)

The SVG uses CSS `<animate>` for particle motion -- no JavaScript needed.
Call `build_context_svg(...)` with current pipeline state to get an updated SVG string.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field


ACCENT = "#22c55e"
ACCENT_DIM = "rgba(34,197,94,0.15)"


@dataclass
class ContextVizState:
    """Snapshot of pipeline state for the visualization."""
    phase: str = "not_started"
    active_files: list[str] = field(default_factory=list)
    total_sources: int = 0
    relevant_sources: int = 0
    producer_labels: list[str] = field(default_factory=lambda: [
        "Code", "Dependencies", "Documentation", "Style", "Recent changes", "Issues",
    ])
    consumer_labels: list[str] = field(default_factory=lambda: [
        "Completions", "Code Review", "Remote Agents", "Agents",
    ])


def _file_position(name: str, idx: int, total: int) -> tuple[float, float, float]:
    """Deterministic position inside the circle based on file name hash."""
    h = int(hashlib.md5(name.encode()).hexdigest()[:8], 16)
    angle = (h % 360) * math.pi / 180
    r_frac = 0.3 + (h % 1000) / 1000 * 0.6
    cx = 450 + math.cos(angle) * 130 * r_frac
    cy = 250 + math.sin(angle) * 130 * r_frac
    opacity = max(0.3, 1.0 - idx * 0.12)
    return cx, cy, opacity


def _truncate(name: str, max_len: int = 22) -> str:
    return name if len(name) <= max_len else name[:max_len - 1] + "\u2026"


def build_context_svg(state: ContextVizState | None = None) -> str:
    """Build the full animated SVG string."""
    if state is None:
        state = ContextVizState()

    W, H = 900, 480
    lines: list[str] = []

    lines.append(
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:100%;font-family:\'Cascadia Code\',Consolas,monospace;background:#111827;border-radius:12px;">'
    )

    _add_dot_grid(lines, W, H)
    _add_left_column(lines, state)
    _add_center_column(lines, state)
    _add_right_column(lines, state)

    lines.append(
        f'<text x="{W - 30}" y="{H - 30}" fill="white" font-size="9" '
        f'opacity="0.25" text-anchor="end">Fig. 1.1</text>'
    )
    lines.append("</svg>")
    return "\n".join(lines)


def _add_dot_grid(lines: list[str], w: int, h: int) -> None:
    lines.append("<g>")
    for x in range(16, w, 32):
        for y in range(16, h, 32):
            lines.append(
                f'<circle cx="{x}" cy="{y}" r="1" fill="white" opacity="0.04"/>'
            )
    lines.append("</g>")


def _add_left_column(lines: list[str], state: ContextVizState) -> None:
    lines.append("<g>")
    lines.append(
        '<text x="32" y="48" fill="white" font-size="10" opacity="0.4" '
        'font-weight="600" letter-spacing="0.05em">REALTIME RAW CONTEXT</text>'
    )

    rows = state.producer_labels[:6]
    y_positions = [100, 160, 220, 280, 340, 400]

    for i, (label, y) in enumerate(zip(rows, y_positions)):
        delay = i * 0.35
        lines.append("<g>")
        lines.append(
            f'<text x="32" y="{y}" fill="white" font-size="10" opacity="0.45" '
            f'dominant-baseline="middle">{label}</text>'
        )
        for j, ox in enumerate([150, 162, 174, 186]):
            op = 0.10 + j * 0.06
            lines.append(
                f'<circle cx="{ox}" cy="{y}" r="3" fill="white" opacity="{op:.2f}"/>'
            )

        target_y = 250
        mid_x, mid_y = 260, (y + target_y) / 2
        lines.append(
            f'<circle r="3" fill="{ACCENT}" opacity="0">'
            f'<animate attributeName="cx" values="200;{mid_x};290" dur="3s" '
            f'begin="{delay:.2f}s" repeatCount="indefinite" '
            f'keyTimes="0;0.3;1" calcMode="spline" keySplines="0.4 0 0.2 1;0.4 0 0.2 1"/>'
            f'<animate attributeName="cy" values="{y};{mid_y:.0f};{target_y}" dur="3s" '
            f'begin="{delay:.2f}s" repeatCount="indefinite" '
            f'keyTimes="0;0.3;1" calcMode="spline" keySplines="0.4 0 0.2 1;0.4 0 0.2 1"/>'
            f'<animate attributeName="opacity" values="0.8;0.5;0" dur="3s" '
            f'begin="{delay:.2f}s" repeatCount="indefinite"/>'
            f'</circle>'
        )
        lines.append("</g>")

    guide = " ".join(
        f"M 200 {y} Q {250 + (i % 3) * 5} {(y + 250) // 2} 290 250"
        for i, y in enumerate(y_positions)
    )
    lines.append(
        f'<path d="{guide}" stroke="white" stroke-width="1" fill="none" opacity="0.03"/>'
    )
    lines.append("</g>")


def _add_center_column(lines: list[str], state: ContextVizState) -> None:
    lines.append("<g>")
    lines.append(
        '<text x="380" y="48" fill="white" font-size="10" opacity="0.4" '
        'font-weight="600" letter-spacing="0.05em">SEMANTIC UNDERSTANDING</text>'
    )

    lines.append(
        f'<circle cx="450" cy="250" r="140" fill="none" stroke="white" '
        f'stroke-width="1" opacity="0.06"/>'
    )
    for ey in [166, 222, 278, 334]:
        ry = 34 if ey in (166, 334) else 41
        rx = 112 if ey in (166, 334) else 137
        lines.append(
            f'<ellipse cx="450" cy="{ey}" rx="{rx}" ry="{ry}" '
            f'fill="none" stroke="white" stroke-width="0.5" opacity="0.04"/>'
        )

    _add_scatter_nodes(lines)

    active = state.active_files[:12]
    for idx, fname in enumerate(active):
        cx, cy, opacity = _file_position(fname, idx, len(active))
        display = _truncate(fname)
        tw = len(display) * 5.5 + 12
        lines.append(
            f'<g opacity="{opacity:.2f}">'
            f'<rect x="{cx - 2}" y="{cy - 8}" height="16" rx="3" '
            f'fill="{ACCENT}" width="{tw:.0f}"/>'
            f'<text x="{cx + 3}" y="{cy}" fill="white" font-size="8" '
            f'font-weight="500" dominant-baseline="middle">{display}</text>'
            f'</g>'
        )

    lines.append("</g>")


def _add_scatter_nodes(lines: list[str]) -> None:
    """Add decorative scatter dots inside the knowledge graph circle."""
    import random
    rng = random.Random(42)
    for _ in range(80):
        angle = rng.uniform(0, 2 * math.pi)
        r = rng.uniform(0.1, 0.95) * 130
        cx = 450 + math.cos(angle) * r
        cy = 250 + math.sin(angle) * r
        sr = rng.uniform(1.5, 4.5)
        op = rng.uniform(0.02, 0.12)
        color = ACCENT if rng.random() < 0.15 else "white"
        if color == ACCENT:
            op = rng.uniform(0.15, 0.5)
        lines.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{sr:.1f}" '
            f'fill="{color}" opacity="{op:.2f}"/>'
        )


def _add_right_column(lines: list[str], state: ContextVizState) -> None:
    lines.append("<g>")
    lines.append(
        '<text x="680" y="48" fill="white" font-size="10" opacity="0.4" '
        'font-weight="600" letter-spacing="0.05em">CURATED CONTEXT</text>'
    )

    consumers = state.consumer_labels[:4]
    y_positions = [120, 200, 280, 360]

    lines.append("<defs>")
    for i, y in enumerate(y_positions):
        qy = (250 + y) / 2 - (250 - y) * 0.1
        lines.append(
            f'<path id="out-{i}" d="M 610 250 Q 650 {qy:.0f} 700 {y}"/>'
        )
    lines.append("</defs>")

    paths = " ".join(
        f"M 610 250 Q 650 {(250 + y) / 2 - (250 - y) * 0.1:.0f} 700 {y}"
        for y in y_positions
    )
    lines.append(
        f'<path d="{paths}" stroke="{ACCENT}" stroke-width="1" fill="none" opacity="0.12"/>'
    )

    for i, (label, y) in enumerate(zip(consumers, y_positions)):
        delay = 0.5 + i * 0.4
        lines.append(
            f'<circle r="3" fill="{ACCENT}" opacity="0">'
            f'<animateMotion dur="2.5s" begin="{delay}s" repeatCount="indefinite" '
            f'calcMode="spline" keyTimes="0;1" keySplines="0.4 0 0.2 1">'
            f'<mpath href="#out-{i}"/></animateMotion>'
            f'<animate attributeName="opacity" values="0;0.8;0.8;0" '
            f'dur="2.5s" begin="{delay}s" repeatCount="indefinite" '
            f'keyTimes="0;0.1;0.8;1"/>'
            f'</circle>'
        )

        lines.append(
            f'<circle cx="700" cy="{y}" r="6" fill="{ACCENT}" opacity="0.85">'
            f'<animate attributeName="opacity" values="0.7;0.95;0.7" '
            f'dur="3s" begin="{i * 0.3}s" repeatCount="indefinite"/>'
            f'</circle>'
        )
        lines.append(
            f'<text x="718" y="{y}" fill="white" font-size="10" opacity="0.7" '
            f'dominant-baseline="middle" font-weight="500">{label}</text>'
        )

    total = state.total_sources or 0
    relevant = state.relevant_sources or 0
    bar_w = 160
    fill_w = int(bar_w * (relevant / total)) if total > 0 else 0

    lines.append(
        f'<text x="700" y="420" fill="white" font-size="10" opacity="0.5">'
        f'{total:,} sources \u2192 {relevant:,} relevant</text>'
    )
    lines.append(
        f'<rect x="700" y="434" width="{bar_w}" height="4" rx="2" '
        f'fill="white" opacity="0.08"/>'
    )
    lines.append(
        f'<rect x="700" y="434" width="{fill_w}" height="4" rx="2" '
        f'fill="{ACCENT}" opacity="0.7"/>'
    )
    lines.append("</g>")
