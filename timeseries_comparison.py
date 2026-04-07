"""
timeseries_comparison.py

Visualize CGRA simulation timelines from two log formats:
  Top subplot:    RTL trace (trace_gemv_4x4_Mesh.jsonl), with stall highlighting in red
  Bottom subplot: Simulator log (gemv.json.log), Inst records only

Each operation is drawn as a uniform-size cell with the op name inside.
The figure is sized so every cycle is shown without omission.
"""

import json
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ── colour scheme ─────────────────────────────────────────────────────────────
OP_COLOR      = "#5B9BD5"   # blue: all active FU ops
NAH_IDLE      = "#d0d0d0"   # light grey: truly idle (no data moving)
NAH_ROUTING   = "#FFA040"   # orange: NAH but actively routing data through xbar
STALL_COLOR   = "#ff0000"   # red: stalled (overrides all others)
DEFAULT_COLOR = OP_COLOR

# Text contrast
_DARK_BG = {OP_COLOR, STALL_COLOR, NAH_ROUTING}

def text_color(bg_hex):
    return "white" if bg_hex in _DARK_BG else "black"


# ── stall detection ───────────────────────────────────────────────────────────
def is_stalled(tile):
    """
    Return True if the tile is stalled in either direction:

    Downstream stall — tile has valid data but consumer is not ready:
      • fu.outputs[i].val=1, rdy=0
      • send_data[i].val=1, rdy=0
      • routing_xbar.send[i].val=1, rdy=0

    Upstream stall — tile is ready to consume but producer has not sent data:
      • fu.inputs[i].rdy=1, val=0  (only checked for active / non-idle ops
        to avoid false positives on unused input ports of idle tiles)
    """
    op = tile["fu"]["operation_symbol"]

    # ── downstream: tile holds valid data that can't be forwarded ────────────
    for out in tile["fu"]["outputs"]:
        if out["val"] == 1 and out["rdy"] == 0:
            return True
    for sd in tile["send_data"]:
        if sd["val"] == 1 and sd["rdy"] == 0:
            return True
    for s in tile["routing_xbar"]["send"]:
        if s["val"] == 1 and s["rdy"] == 0:
            return True

    # ── upstream: active tile waiting for input data that hasn't arrived ──────
    if op not in ("(NAH)", "(start)"):
        for inp in tile["fu"]["inputs"]:
            if inp["rdy"] == 1 and inp["val"] == 0:
                return True

    return False


# ── routing-NAH detection ─────────────────────────────────────────────────────
def is_routing_nah(tile):
    """
    True when fu says NAH but data is visibly moving through the routing
    crossbar (recv or send ports carry valid data).
    """
    xbar = tile["routing_xbar"]
    return (
        any(p["val"] == 1 for p in xbar["send"]) or
        any(p["val"] == 1 for p in xbar["recv"])
    )


def cell_color(op, stalled, routing):
    if stalled:
        return STALL_COLOR
    if op == "(NAH)":
        return NAH_ROUTING if routing else NAH_IDLE
    return OP_COLOR


# ── parse RTL trace ───────────────────────────────────────────────────────────
trace_data = {}   # (cycle, row, col) -> (op_symbol, bg_color)
trace_ops  = set()

with open("trace_gemv_4x4_Mesh.jsonl") as f:
    for line in f:
        record = json.loads(line)
        cycle  = record["cycle"]
        for tile in record["tiles"]:
            row, col = tile["row"], tile["col"]
            op       = tile["fu"]["operation_symbol"]
            stalled  = is_stalled(tile)
            routing  = is_routing_nah(tile)
            bg       = cell_color(op, stalled, routing)
            trace_data[(cycle, row, col)] = (op, bg)
            trace_ops.add(op)

trace_cycles = [k[0] for k in trace_data]
trace_xmin, trace_xmax = min(trace_cycles), max(trace_cycles)


# ── parse simulator log ───────────────────────────────────────────────────────
# (time, row, col) -> list of opcodes (deduplicated, order preserved)
gemv_data = defaultdict(list)
gemv_ops  = set()

with open("gemv.json.log") as f:
    for line in f:
        record = json.loads(line)
        if record.get("msg") != "Inst":
            continue
        time = round(record["Time"])
        row  = record["Y"]   # Y = row
        col  = record["X"]   # X = col
        op   = record["OpCode"]
        key  = (time, row, col)
        if op not in gemv_data[key]:   # deduplicate within the same cell
            gemv_data[key].append(op)
        gemv_ops.add(op)

gemv_times = [k[0] for k in gemv_data]
gemv_xmin, gemv_xmax = min(gemv_times), max(gemv_times)


# ── shared axis range ─────────────────────────────────────────────────────────
global_xmin = min(trace_xmin, gemv_xmin)   # = 0
global_xmax = max(trace_xmax, gemv_xmax)   # = 443
n_cycles    = global_xmax - global_xmin + 1

ALL_TILES = [f"({r},{c})" for r in range(4) for c in range(4)]
tile_y    = {t: i for i, t in enumerate(ALL_TILES)}

# ── figure sizing: one cell per cycle, no size cap ───────────────────────────
CELL_W_IN   = 0.55                               # inch per cycle column
fig_width   = n_cycles * CELL_W_IN + 4.0        # ≈ 248 inches for 444 cycles
fig_height  = 22.0                               # tall enough for 2 × 16-tile subplots
LABEL_FS    = 7                                  # fontsize for op labels in cells
TICK_FS     = 7

fig, (ax_trace, ax_gemv) = plt.subplots(
    2, 1, figsize=(fig_width, fig_height), sharex=True,
    gridspec_kw={"hspace": 0.12},
)
fig.subplots_adjust(left=0.04, right=0.93, top=0.97, bottom=0.04)


# ── helper: draw one uniform cell ────────────────────────────────────────────
def draw_cell(ax, x, yi, label, bg, alpha=0.88):
    rect = plt.Rectangle(
        (x - 0.45, yi - 0.45), 0.9, 0.9,
        color=bg, alpha=alpha, linewidth=0.3, edgecolor="#888888",
    )
    ax.add_patch(rect)
    ax.text(
        x, yi, label,
        ha="center", va="center",
        fontsize=LABEL_FS, color=text_color(bg),
        clip_on=True,
    )


# ── draw RTL trace subplot ────────────────────────────────────────────────────
for (cycle, row, col), (op, bg) in trace_data.items():
    if op == "(start)":
        continue
    yi    = tile_y[f"({row},{col})"]
    alpha = 0.20 if bg == NAH_IDLE else 0.88
    label = "STALL\n" + op if bg == STALL_COLOR else op
    draw_cell(ax_trace, cycle, yi, label, bg, alpha)

ax_trace.set_xlim(global_xmin - 0.6, global_xmax + 0.6)
ax_trace.set_ylim(-0.5, len(ALL_TILES) - 0.5)
ax_trace.set_yticks(range(len(ALL_TILES)))
ax_trace.set_yticklabels(ALL_TILES, fontsize=8)
ax_trace.set_ylabel("Tile (row,col)", fontsize=10)
ax_trace.set_title(
    f"RTL Trace — trace_gemv_4x4_Mesh.jsonl  (cycles {trace_xmin}–{trace_xmax})",
    fontsize=12, fontweight="bold", loc="left",
)
ax_trace.grid(True, linestyle=":", linewidth=0.3, alpha=0.4)


# ── draw simulator log subplot ────────────────────────────────────────────────
# Multiple ops at the same (time, tile) are shown as a single cell whose label
# lists all ops; background uses the first op's colour.
for (time, row, col), ops in gemv_data.items():
    label = f"({row},{col})"
    if label not in tile_y:
        continue
    yi    = tile_y[label]
    bg    = OP_COLOR
    text  = "\n".join(ops) if len(ops) > 1 else ops[0]
    draw_cell(ax_gemv, time, yi, text, bg)

ax_gemv.set_ylim(-0.5, len(ALL_TILES) - 0.5)
ax_gemv.set_yticks(range(len(ALL_TILES)))
ax_gemv.set_yticklabels(ALL_TILES, fontsize=8)
ax_gemv.set_ylabel("Tile (row,col)", fontsize=10)
ax_gemv.set_xlabel("Cycle", fontsize=11)
ax_gemv.set_title(
    f"Simulator Log — gemv.json.log  (cycles {gemv_xmin}–{gemv_xmax})",
    fontsize=12, fontweight="bold", loc="left",
)
ax_gemv.grid(True, linestyle=":", linewidth=0.3, alpha=0.4)

# X-ticks: every 5 cycles (fine enough to read individual cycles)
ax_gemv.set_xticks(range(global_xmin, global_xmax + 1, 5))
ax_gemv.tick_params(axis="x", labelsize=TICK_FS, rotation=90)


# ── legend ────────────────────────────────────────────────────────────────────
legend_patches = [
    mpatches.Patch(color=OP_COLOR,    label="Active op"),
    mpatches.Patch(color=NAH_IDLE,    label="NAH (idle, no data)"),
    mpatches.Patch(color=NAH_ROUTING, label="NAH (routing data through xbar)"),
    mpatches.Patch(color=STALL_COLOR, label="STALL (upstream or downstream, RTL only)"),
]

fig.legend(
    handles=legend_patches,
    loc="upper right",
    bbox_to_anchor=(0.998, 0.99),
    fontsize=10,
    title="Legend",
    title_fontsize=11,
    framealpha=0.9,
)

out = "timeseries_comparison.png"
fig.savefig(out, dpi=120, bbox_inches="tight")
print(f"Saved → {out}")
