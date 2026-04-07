"""
timeseries_comparison.py

Visualize CGRA simulation timelines from two log formats:
  Top subplot:    RTL trace (trace_gemv_4x4_Mesh.jsonl), with stall highlighting in red
  Bottom subplot: Simulator log (gemv.json.log), Inst records only

Each operation is drawn as a uniform-size cell with the op name inside.
Stalled cells are red and show the stall direction + blocking/missing tile.
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

_DARK_BG = {OP_COLOR, STALL_COLOR, NAH_ROUTING}
def text_color(bg): return "white" if bg in _DARK_BG else "black"


# ── xbar port direction mappings ─────────────────────────────────────────────
# routing_xbar.send[0..3] and fu_xbar.send[0..3]: empirically verified from
# boundary tile rdy=0 patterns at cycle 1 (row-3 tiles have send[0].rdy=0,
# row-0 tiles have send[1].rdy=0, col-0 tiles have send[2].rdy=0, etc.)
XBAR_SEND_DIR = {
    0: ("↓S", +1,  0),   # SOUTH output → (row+1, col)
    1: ("↑N", -1,  0),   # NORTH output → (row-1, col)
    2: ("←W",  0, -1),   # WEST  output → (row,   col-1)
    3: ("→E",  0, +1),   # EAST  output → (row,   col+1)
}

# routing_xbar.config value → upstream source direction
# From spec (cgra/test/CgraRTL_test.py, CrossbarRTL.py):
#   0=not-connected, 1=N(row-1), 2=S(row+1), 3=W(col-1), 4=E(col+1), 5-8=regfile
CONFIG_SRC = {
    1: ("↑N", -1,  0),   # from NORTH neighbour (row-1, col)
    2: ("↓S", +1,  0),   # from SOUTH neighbour (row+1, col)
    3: ("←W",  0, -1),   # from WEST  neighbour (row,   col-1)
    4: ("→E",  0, +1),   # from EAST  neighbour (row,   col+1)
}

def _cfg_label(cfg, row, col):
    """Return (label_str, neighbor_row, neighbor_col) for a config value."""
    if cfg == 0:
        return "∅", None, None
    if cfg in CONFIG_SRC:
        arrow, dr, dc = CONFIG_SRC[cfg]
        return f"{arrow}({row+dr},{col+dc})", row + dr, col + dc
    return f"reg{cfg-5}", None, None   # local regfile bank


# ── stall detection + cause labelling ────────────────────────────────────────
def stall_info(tile, row, col):
    """
    Returns (is_stalled: bool, causes: list[str]).

    Uses xbar config to precisely trace:
      • Downstream: which neighbour is not accepting routed/FU data
      • Upstream:   which neighbour hasn't supplied expected FU operand

    routing_xbar.send layout (per spec + empirical verification):
      [0]=↓S  [1]=↑N  [2]=←W  [3]=→E   (to neighbour tiles)
      [4]=FU_IN[0]  [5]=FU_IN[1]  [6]=FU_IN[2]  [7]=FU_IN[3]

    routing_xbar.config[i] encodes which input feeds output i:
      0=disconnected, 1=↑N, 2=↓S, 3=←W, 4=→E, 5-8=regfile[0-3]

    fu_xbar.send layout: same as routing_xbar.send (0..3=neighbours, 4..7=FU loopback)
    fu_xbar.config[i]: 0=disconnected, 1=FU_OUT[0], 2=FU_OUT[1]
    """
    op    = tile["fu"]["operation_symbol"]
    rxbar = tile["routing_xbar"]
    fxbar = tile["fu_xbar"]
    causes = []

    # ── A. routing_xbar outputs stalled ──────────────────────────────────────
    for i, s in enumerate(rxbar["send"]):
        if s["val"] != 1 or s["rdy"] != 0:
            continue
        cfg = rxbar["config"][i]
        src_lbl, _, _ = _cfg_label(cfg, row, col)

        if i <= 3:
            # Routed data trying to leave to a neighbour tile
            dir_arrow, dr, dc = XBAR_SEND_DIR[i]
            nr, nc = row + dr, col + dc
            causes.append(f"route {src_lbl}{dir_arrow}({nr},{nc})blk")
        else:
            # Data trying to enter FU input (FU not accepting)
            fu_in = i - 4
            causes.append(f"FU[{fu_in}]←{src_lbl} FUblk")

    # ── B. fu_xbar outputs stalled ────────────────────────────────────────────
    for i, s in enumerate(fxbar["send"]):
        if s["val"] != 1 or s["rdy"] != 0:
            continue
        cfg = fxbar["config"][i]
        fu_out_lbl = f"FUout{cfg}"   # which FU output (1=FU_OUT[0], 2=FU_OUT[1])

        if i <= 3:
            dir_arrow, dr, dc = XBAR_SEND_DIR[i]
            nr, nc = row + dr, col + dc
            causes.append(f"{fu_out_lbl}{dir_arrow}({nr},{nc})blk")
        else:
            causes.append(f"{fu_out_lbl}→loop[{i-4}]blk")

    # ── C. upstream: active FU waiting for expected operand ──────────────────
    # Only meaningful when the tile is executing a real operation.
    if op not in ("(NAH)", "(start)"):
        for j, inp in enumerate(tile["fu"]["inputs"]):
            if inp["rdy"] == 1 and inp["val"] == 0:
                cfg = rxbar["config"][4 + j]   # FU_IN[j] ← config[4+j]
                src_lbl, nr, nc = _cfg_label(cfg, row, col)
                if nr is not None:
                    causes.append(f"FU[{j}]wait {src_lbl}")
                elif cfg == 0:
                    pass   # disconnected port, not a real stall
                else:
                    causes.append(f"FU[{j}]wait {src_lbl}")

    # A tile whose FU is actively completing a computation this cycle is
    # "executing", even if secondary routing ports are simultaneously stalled
    # (e.g. an unused FU input whose rdy=0 causes backpressure on its source).
    # Return that flag so the caller can colour executing cycles blue.
    fu_executing = any(o["val"] == 1 and o["rdy"] == 1
                       for o in tile["fu"]["outputs"])

    return bool(causes), causes, fu_executing


# ── routing-NAH detection ─────────────────────────────────────────────────────
def is_routing_nah(tile):
    xbar = tile["routing_xbar"]
    return (
        any(p["val"] == 1 for p in xbar["send"]) or
        any(p["val"] == 1 for p in xbar["recv"])
    )


# ── parse RTL trace ───────────────────────────────────────────────────────────
# (cycle, row, col) -> (op_symbol, bg_color, label_str)
trace_data = {}
trace_ops  = set()

with open("trace_gemv_4x4_Mesh.jsonl") as f:
    for line in f:
        record = json.loads(line)
        cycle  = record["cycle"]
        for tile in record["tiles"]:
            row, col  = tile["row"], tile["col"]
            op        = tile["fu"]["operation_symbol"]
            stalled, causes, fu_executing = stall_info(tile, row, col)
            routing   = is_routing_nah(tile)

            if stalled and not fu_executing:
                # Genuinely blocked: stall signal present AND FU produced nothing
                bg    = STALL_COLOR
                label = op + "\n" + "\n".join(causes)
            elif stalled and fu_executing:
                # FU completed output this cycle despite secondary routing stalls
                # (e.g. an unused FU input port backed up by its source).
                # Show as executing but annotate the secondary blockage.
                bg    = OP_COLOR
                label = op + "\n[blk:" + " ".join(causes) + "]"
            elif op == "(NAH)":
                bg    = NAH_ROUTING if routing else NAH_IDLE
                label = op
            else:
                bg    = OP_COLOR
                label = op

            trace_data[(cycle, row, col)] = (op, bg, label)
            trace_ops.add(op)

trace_cycles = [k[0] for k in trace_data]
trace_xmin, trace_xmax = min(trace_cycles), max(trace_cycles)


# ── parse simulator log ───────────────────────────────────────────────────────
gemv_data = defaultdict(list)   # (time, row, col) -> [opcode, ...]
gemv_ops  = set()

with open("gemv.json.log") as f:
    for line in f:
        record = json.loads(line)
        if record.get("msg") != "Inst":
            continue
        time = round(record["Time"])
        row  = record["Y"]
        col  = record["X"]
        op   = record["OpCode"]
        key  = (time, row, col)
        if op not in gemv_data[key]:
            gemv_data[key].append(op)
        gemv_ops.add(op)

gemv_times = [k[0] for k in gemv_data]
gemv_xmin, gemv_xmax = min(gemv_times), max(gemv_times)


# ── shared axis range ─────────────────────────────────────────────────────────
global_xmin = min(trace_xmin, gemv_xmin)
global_xmax = max(trace_xmax, gemv_xmax)
n_cycles    = global_xmax - global_xmin + 1

ALL_TILES = [f"({r},{c})" for r in range(4) for c in range(4)]
tile_y    = {t: i for i, t in enumerate(ALL_TILES)}

# ── figure sizing: boxes scale with text content ──────────────────────────────
# Tune this single knob — everything else derives from it.
LABEL_FS  = 8.0           # font size in points
TICK_FS   = 7

# Measure the widest / tallest label across all cells.
all_labels = (
    [lbl for _, _, lbl in trace_data.values()]
    + ["\n".join(ops) for ops in gemv_data.values()]
)
max_chars = max((max(len(ln) for ln in lbl.split("\n")) for lbl in all_labels), default=6)
max_lines = max((lbl.count("\n") + 1                    for lbl in all_labels), default=1)

# Approximate character / line dimensions at LABEL_FS points.
# Empirical constants for the default matplotlib font:
#   character width  ≈ LABEL_FS × 0.55 pt  →  inches = × (1/72)
#   line height      ≈ LABEL_FS × 1.35 pt  (includes inter-line spacing)
CHAR_W_IN = LABEL_FS * 0.55 / 72
LINE_H_IN = LABEL_FS * 1.35 / 72

H_PAD = 0.12   # extra inches of padding around text, horizontal
V_PAD = 0.10   # vertical

CELL_W_IN = max_chars * CHAR_W_IN + H_PAD   # inches per cycle column
TILE_H_IN = max_lines * LINE_H_IN + V_PAD   # inches per tile row
CELL_H    = 0.90                            # cell height in data units (gap of 0.05)

fig_width  = n_cycles * CELL_W_IN + 4.0
fig_height = len(ALL_TILES) * TILE_H_IN * 2 + 4.0   # 2 subplots

fig, (ax_trace, ax_gemv) = plt.subplots(
    2, 1, figsize=(fig_width, fig_height), sharex=True,
    gridspec_kw={"hspace": 0.10},
)
fig.subplots_adjust(left=0.03, right=0.93, top=0.97, bottom=0.03)


# ── helper: draw one cell ─────────────────────────────────────────────────────
def draw_cell(ax, x, yi, label, bg, alpha=0.88):
    rect = plt.Rectangle(
        (x - 0.45, yi - CELL_H / 2), 0.9, CELL_H,
        facecolor=bg, edgecolor="#777777", linewidth=0.3, alpha=alpha,
    )
    ax.add_patch(rect)
    ax.text(
        x, yi, label,
        ha="center", va="center",
        fontsize=LABEL_FS, color=text_color(bg),
        linespacing=1.2,
        clip_on=True,
    )


# ── RTL trace subplot ─────────────────────────────────────────────────────────
for (cycle, row, col), (op, bg, label) in trace_data.items():
    if op == "(start)":
        continue
    yi    = tile_y[f"({row},{col})"]
    alpha = 0.18 if bg == NAH_IDLE else 0.88
    draw_cell(ax_trace, cycle, yi, label, bg, alpha)

ax_trace.set_xlim(global_xmin - 0.6, global_xmax + 0.6)
ax_trace.set_ylim(-0.5, len(ALL_TILES) - 0.5)
ax_trace.set_yticks(range(len(ALL_TILES)))
ax_trace.set_yticklabels(ALL_TILES, fontsize=9)
ax_trace.set_ylabel("Tile (row,col)", fontsize=10)
ax_trace.set_title(
    f"RTL Trace — trace_gemv_4x4_Mesh.jsonl  (cycles {trace_xmin}–{trace_xmax})",
    fontsize=12, fontweight="bold", loc="left",
)
ax_trace.grid(True, linestyle=":", linewidth=0.3, alpha=0.4)


# ── simulator log subplot ─────────────────────────────────────────────────────
for (time, row, col), ops in gemv_data.items():
    label = f"({row},{col})"
    if label not in tile_y:
        continue
    yi   = tile_y[label]
    text = "\n".join(ops) if len(ops) > 1 else ops[0]
    draw_cell(ax_gemv, time, yi, text, OP_COLOR)

ax_gemv.set_ylim(-0.5, len(ALL_TILES) - 0.5)
ax_gemv.set_yticks(range(len(ALL_TILES)))
ax_gemv.set_yticklabels(ALL_TILES, fontsize=9)
ax_gemv.set_ylabel("Tile (row,col)", fontsize=10)
ax_gemv.set_xlabel("Cycle", fontsize=11)
ax_gemv.set_title(
    f"Simulator Log — gemv.json.log  (cycles {gemv_xmin}–{gemv_xmax})",
    fontsize=12, fontweight="bold", loc="left",
)
ax_gemv.grid(True, linestyle=":", linewidth=0.3, alpha=0.4)

ax_gemv.set_xticks(range(global_xmin, global_xmax + 1, 5))
ax_gemv.tick_params(axis="x", labelsize=TICK_FS, rotation=90)


# ── legend ────────────────────────────────────────────────────────────────────
legend_patches = [
    mpatches.Patch(color=OP_COLOR,    label="Active op"),
    mpatches.Patch(color=NAH_IDLE,    label="NAH – idle (no data)"),
    mpatches.Patch(color=NAH_ROUTING, label="NAH – routing data through xbar"),
    mpatches.Patch(color=STALL_COLOR,
                   label="STALL (FU not producing + blocked port)\n"
                         "  blue+[blk:] = executing but secondary port stalled\n"
                         "  ↓S/↑N/←W/→E = blocked direction, (r,c) = neighbour"),
]
fig.legend(
    handles=legend_patches,
    loc="upper right",
    bbox_to_anchor=(0.998, 0.99),
    fontsize=9,
    title="Legend",
    title_fontsize=10,
    framealpha=0.9,
    handlelength=1.4,
)

out = "timeseries_comparison.png"
fig.savefig(out, dpi=120, bbox_inches="tight")
print(f"Saved → {out}")
