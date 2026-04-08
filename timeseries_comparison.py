"""
timeseries_comparison.py

Visualize CGRA simulation timelines from two log formats:
  Top subplot:    RTL trace (trace_gemv_4x4_Mesh.jsonl), with stall highlighting in red
  Bottom subplot: Simulator log (gemv.json.log), Inst records only

Each operation is drawn as a uniform-size cell with the op name inside.
Stalled cells are red and show the stall direction + blocking/missing tile.
"""

import json
import sys
import argparse
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

GRID_ROWS = 4
GRID_COLS = 4


def to_user_xy(row, col):
    """Display coordinates in PE(x,y) order: (x,y) = (col,row)."""
    return col, row


def tile_label_xy(row, col):
    x, y = to_user_xy(row, col)
    return f"({x},{y})"

# ── colour scheme ─────────────────────────────────────────────────────────────
OP_COLOR      = "#5B9BD5"   # blue: all active FU ops
PARTIAL_COLOR = "#2EAD4B"   # green: partial progress (some ports accept while others stall)
BUBBLE_COLOR  = "#F2C94C"   # yellow: op shown but no handshake progress this cycle
NAH_IDLE      = "#d0d0d0"   # light grey: truly idle (no data moving)
NAH_ROUTING   = "#FFA040"   # orange: NAH but actively routing data through xbar
STALL_COLOR   = "#ff0000"   # red: stalled (overrides all others)

_DARK_BG = {OP_COLOR, PARTIAL_COLOR, STALL_COLOR, NAH_ROUTING}
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
        nr, nc = row + dr, col + dc
        return f"{arrow}{tile_label_xy(nr, nc)}", nr, nc
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


PORT_DIR = {
    0: "South",
    1: "North",
    2: "West",
    3: "East",
}


def nah_port_status(tile):
    """Return (accepted_labels, stalled_labels) for NAH routing activity."""
    accepted = []
    stalled = []
    rxbar = tile["routing_xbar"]

    # Incoming neighbour ports (recv[0..3])
    for i in range(4):
        p = rxbar["recv"][i]
        if p["val"] == 1 and p["rdy"] == 1:
            accepted.append(f"in-{PORT_DIR[i]} accept")
        elif p["val"] == 1 and p["rdy"] == 0:
            stalled.append(f"in-{PORT_DIR[i]} stall")

    # Outgoing neighbour ports (send[0..3])
    for i in range(4):
        p = rxbar["send"][i]
        if p["val"] == 1 and p["rdy"] == 1:
            accepted.append(f"out-{PORT_DIR[i]} accept")
        elif p["val"] == 1 and p["rdy"] == 0:
            stalled.append(f"out-{PORT_DIR[i]} stall")

    # Routes into FU inputs (send[4..7])
    for i in range(4, 8):
        p = rxbar["send"][i]
        fu_idx = i - 4
        if p["val"] == 1 and p["rdy"] == 1:
            accepted.append(f"to-FU{fu_idx} accept")
        elif p["val"] == 1 and p["rdy"] == 0:
            stalled.append(f"to-FU{fu_idx} stall")

    return accepted, stalled


def tile_port_status(tile):
    """Return (accepted_labels, stalled_labels) for key dataflow ports."""
    accepted, stalled = nah_port_status(tile)

    # Include FU crossbar sends so non-NAH ops can also be classified as partial.
    fxbar = tile["fu_xbar"]
    for i, p in enumerate(fxbar["send"]):
        if p["val"] == 1 and p["rdy"] == 1:
            accepted.append(f"fu-out{i} accept")
        elif p["val"] == 1 and p["rdy"] == 0:
            stalled.append(f"fu-out{i} stall")

    return accepted, stalled


# ── parse RTL trace ───────────────────────────────────────────────────────────
# (cycle, row, col) -> (op_symbol, bg_color, label_str)
trace_data = {}
trace_ops  = set()

def parse_args():
    parser = argparse.ArgumentParser(
        description="Compare RTL trace and simulator log timelines."
    )
    parser.add_argument(
        "trace_file",
        nargs="?",
        default="trace_gemv_4x4_Mesh.jsonl",
        help="Path to RTL trace jsonl file.",
    )
    parser.add_argument(
        "log_file",
        nargs="?",
        default="gemv.json.log",
        help="Path to simulator json.log file.",
    )
    parser.add_argument(
        "--align-steady-center",
        action="store_true",
        help="Shift simulator cycles so steady-state center aligns with RTL steady-state center.",
    )
    parser.add_argument(
        "--steady-min-active",
        type=int,
        default=1,
        help="Minimum active tiles per cycle used to detect steady-state window (default: 1).",
    )
    parser.add_argument(
        "--max-fig-width",
        type=float,
        default=60.0,
        help="Maximum figure width in inches to keep rendering tractable (default: 60).",
    )
    parser.add_argument(
        "--max-fig-height",
        type=float,
        default=40.0,
        help="Maximum figure height in inches (default: 40).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=0,
        help="If >0, save segmented figures with this many cycles per image.",
    )
    parser.add_argument(
        "--cycle-start",
        type=int,
        default=None,
        help="Optional first cycle (inclusive) to display/save.",
    )
    parser.add_argument(
        "--cycle-end",
        type=int,
        default=None,
        help="Optional last cycle (inclusive) to display/save.",
    )
    return parser.parse_args()


def longest_window(active_counts, min_active):
    """Return (start, end) of longest contiguous window with active_count >= min_active."""
    if not active_counts:
        return None

    min_cycle = min(active_counts)
    max_cycle = max(active_counts)
    best = None
    cur_start = None
    prev_cycle = min_cycle - 1

    for cycle in range(min_cycle, max_cycle + 1):
        count = active_counts.get(cycle, 0)
        if count >= min_active:
            if cur_start is None:
                cur_start = cycle
            elif prev_cycle is not None and cycle != prev_cycle + 1:
                cur_end = prev_cycle
                if best is None or (cur_end - cur_start) > (best[1] - best[0]):
                    best = (cur_start, cur_end)
                cur_start = cycle
        else:
            if cur_start is not None and prev_cycle is not None:
                cur_end = prev_cycle
                if best is None or (cur_end - cur_start) > (best[1] - best[0]):
                    best = (cur_start, cur_end)
            cur_start = None
        prev_cycle = cycle

    if cur_start is not None and prev_cycle is not None:
        cur_end = prev_cycle
        if best is None or (cur_end - cur_start) > (best[1] - best[0]):
            best = (cur_start, cur_end)

    return best


def estimate_shift_by_activity(trace_active, sim_active, lag_min=-200, lag_max=200):
    """Find sim shift (in cycles) that maximizes trace-vs-sim activity correlation.

    A positive return value means simulator cycles should be shifted later.
    """
    if not trace_active or not sim_active:
        return 0

    best_lag = 0
    best_score = None
    best_overlap = 0

    for lag in range(lag_min, lag_max + 1):
        score = 0
        overlap = 0
        for cycle, t_val in trace_active.items():
            s_val = sim_active.get(cycle - lag, 0)
            if s_val > 0:
                overlap += 1
                score += t_val * s_val

        if overlap < 8:
            continue

        candidate = (score, overlap)
        if best_score is None or candidate > (best_score, best_overlap):
            best_score = score
            best_overlap = overlap
            best_lag = lag

    return best_lag


args = parse_args()
trace_file = args.trace_file
log_file = args.log_file

with open(trace_file) as f:
    for line in f:
        record = json.loads(line)
        cycle  = record["cycle"]
        for tile in record["tiles"]:
            row, col  = tile["row"], tile["col"]
            op        = tile["fu"]["operation_symbol"]
            stalled, causes, fu_executing = stall_info(tile, row, col)
            routing   = is_routing_nah(tile)

            if op == "(NAH)":
                accepted_ports, stalled_ports = tile_port_status(tile)
                if stalled_ports and accepted_ports:
                    # Partial progress: at least one port advances data this cycle.
                    bg = PARTIAL_COLOR
                    label = op + "\n[acc:" + " ".join(accepted_ports) + "]\n[stall:" + " ".join(stalled_ports) + "]"
                elif stalled_ports:
                    # Fully blocked: no port accepted data this cycle.
                    bg = STALL_COLOR
                    label = op + "\n[stall:" + " ".join(stalled_ports) + "]"
                elif routing:
                    bg = NAH_ROUTING
                    label = op + "\n[routing]"
                else:
                    bg = NAH_IDLE
                    label = op
            elif stalled and not fu_executing:
                accepted_ports, stalled_ports = tile_port_status(tile)
                if accepted_ports and stalled_ports:
                    # Same as NAH partial case: some lanes are progressing.
                    bg = PARTIAL_COLOR
                    label = op + "\n[acc:" + " ".join(accepted_ports) + "]\n[stall:" + " ".join(stalled_ports) + "]"
                else:
                    # Fully blocked: no observable acceptance this cycle.
                    bg    = STALL_COLOR
                    label = op + "\n" + "\n".join(causes)
            elif stalled and fu_executing:
                # FU completed output this cycle despite secondary routing stalls
                # (e.g. an unused FU input port backed up by its source).
                # Show as executing but annotate the secondary blockage.
                bg    = OP_COLOR
                label = op + "\n[blk:" + " ".join(causes) + "]"
            elif op not in ("(NAH)", "(start)") and not fu_executing:
                accepted_ports, stalled_ports = tile_port_status(tile)
                # STORE-like ops may commit side effects without driving FU outputs.
                store_like_commit = (
                    op in ("(st)", "(store)")
                    and any(inp["val"] == 1 and inp["rdy"] == 1 for inp in tile["fu"]["inputs"])
                )
                if store_like_commit:
                    bg = OP_COLOR
                    label = op + "\n[store-commit]"
                elif not accepted_ports and not stalled_ports:
                    # Issue/pipe bubble: operation symbol is visible, but there is
                    # no data handshake progress in this cycle.
                    bg = BUBBLE_COLOR
                    label = op + "\n[bubble]"
                else:
                    bg = OP_COLOR
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

with open(log_file) as f:
    for line in f:
        record = json.loads(line)
        msg = record.get("msg")
        if msg == "Inst":
            time = round(record["Time"])
            row  = record["Y"]
            col  = record["X"]
            op   = record["OpCode"]
            key  = (time, row, col)
            if op not in gemv_data[key]:
                gemv_data[key].append(op)
            gemv_ops.add(op)
        elif msg == "Memory" and record.get("Behavior") == "LoadDirect":
            # Keep direct-load visible in simulator subplot.
            if "Time" not in record:
                continue
            time = round(record["Time"])
            row  = record["Y"]
            col  = record["X"]
            op   = "LOAD_DIRECT"
            key  = (time, row, col)
            if op not in gemv_data[key]:
                gemv_data[key].append(op)
            gemv_ops.add(op)

gemv_times = [k[0] for k in gemv_data]
gemv_xmin, gemv_xmax = min(gemv_times), max(gemv_times)

# Optional: align simulator timeline to RTL steady-state center.
sim_shift = 0
trace_steady = None
sim_steady = None
if args.align_steady_center:
    trace_active = defaultdict(int)
    for (cycle, _row, _col), (op, _bg, _label) in trace_data.items():
        if op not in ("(start)", "(NAH)"):
            trace_active[cycle] += 1

    sim_active = defaultdict(int)
    for (time, _row, _col), ops in gemv_data.items():
        if ops:
            sim_active[time] += 1

    sim_shift = estimate_shift_by_activity(trace_active, sim_active)
    if sim_shift != 0:
        shifted = defaultdict(list)
        for (time, row, col), ops in gemv_data.items():
            shifted[(time + sim_shift, row, col)] = ops
        gemv_data = shifted
        gemv_times = [k[0] for k in gemv_data]
        gemv_xmin, gemv_xmax = min(gemv_times), max(gemv_times)

    # Recompute steady windows after possible shift for reporting.
    sim_active_shifted = defaultdict(int)
    for (time, _row, _col), ops in gemv_data.items():
        if ops:
            sim_active_shifted[time] += 1

    trace_steady = longest_window(trace_active, args.steady_min_active)
    sim_steady = longest_window(sim_active_shifted, args.steady_min_active)


# ── shared axis range ─────────────────────────────────────────────────────────
global_xmin = min(trace_xmin, gemv_xmin)
global_xmax = max(trace_xmax, gemv_xmax)
n_cycles    = global_xmax - global_xmin + 1
cycles_for_sizing = args.chunk_size if args.chunk_size and args.chunk_size > 0 else n_cycles

ALL_TILES = [f"({r},{c})" for r in range(GRID_ROWS) for c in range(GRID_COLS)]
tile_y    = {t: i for i, t in enumerate(ALL_TILES)}

# ── figure sizing: boxes scale with text content ──────────────────────────────
# Tune this single knob — everything else derives from it.
LABEL_FS  = 8.0           # font size in points
TICK_FS   = 7

# Measure the widest / tallest label across all cells.
all_labels = (
    [f"{tile_label_xy(row,col)}\n{lbl}" for (cycle, row, col), (_op, _bg, lbl) in trace_data.items()]
    + [f"{tile_label_xy(row,col)}\n" + ("\n".join(ops) if len(ops) > 1 else ops[0])
       for (time, row, col), ops in gemv_data.items()]
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

fig_width  = cycles_for_sizing * CELL_W_IN + 4.0
fig_height = len(ALL_TILES) * TILE_H_IN * 2 + 4.0   # 2 subplots
fig_width = min(fig_width, args.max_fig_width)
fig_height = min(fig_height, args.max_fig_height)

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
    draw_cell(ax_trace, cycle, yi, f"{tile_label_xy(row,col)}\n{label}", bg, alpha)

ax_trace.set_ylim(-0.5, len(ALL_TILES) - 0.5)
ax_trace.set_yticks(range(len(ALL_TILES)))
ax_trace.set_yticklabels([tile_label_xy(r, c) for r in range(GRID_ROWS) for c in range(GRID_COLS)], fontsize=9)
ax_trace.set_ylabel("Tile (x,y)", fontsize=10)
ax_trace.set_title(
    f"RTL Trace — {trace_file}  (cycles {trace_xmin}–{trace_xmax})",
    fontsize=12, fontweight="bold", loc="left",
)
ax_trace.grid(True, linestyle=":", linewidth=0.3, alpha=0.4)


# ── simulator log subplot ─────────────────────────────────────────────────────
for (time, row, col), ops in gemv_data.items():
    label = f"({row},{col})"
    if label not in tile_y:
        continue
    yi   = tile_y[label]
    text = f"{tile_label_xy(row,col)}\n" + ("\n".join(ops) if len(ops) > 1 else ops[0])
    draw_cell(ax_gemv, time, yi, text, OP_COLOR)

ax_gemv.set_ylim(-0.5, len(ALL_TILES) - 0.5)
ax_gemv.set_yticks(range(len(ALL_TILES)))
ax_gemv.set_yticklabels([tile_label_xy(r, c) for r in range(GRID_ROWS) for c in range(GRID_COLS)], fontsize=9)
ax_gemv.set_ylabel("Tile (x,y)", fontsize=10)
ax_gemv.set_xlabel("Cycle", fontsize=11)
ax_gemv.set_title(
    f"Simulator Log — {log_file}  (cycles {gemv_xmin}–{gemv_xmax}, shift {sim_shift:+d})",
    fontsize=12, fontweight="bold", loc="left",
)
ax_gemv.grid(True, linestyle=":", linewidth=0.3, alpha=0.4)

ax_gemv.tick_params(axis="x", labelsize=TICK_FS, rotation=90)


# ── legend ────────────────────────────────────────────────────────────────────
legend_patches = [
    mpatches.Patch(color=OP_COLOR,    label="Active op"),
    mpatches.Patch(color=PARTIAL_COLOR,
                   label="Partial progress: some ports accept, some stall"),
    mpatches.Patch(color=BUBBLE_COLOR,
                   label="Bubble: op shown, no handshake progress"),
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

trace_base = trace_file.replace(".jsonl", "")
log_base   = log_file.replace(".json.log", "").replace(".log", "")
suffix = "_aligned" if args.align_steady_center else ""

def apply_x_window(x0, x1):
    ax_trace.set_xlim(x0 - 0.6, x1 + 0.6)
    ax_gemv.set_xlim(x0 - 0.6, x1 + 0.6)
    tick_start = x0 - (x0 % 5)
    if tick_start < x0:
        tick_start += 5
    ticks = list(range(tick_start, x1 + 1, 5))
    if not ticks:
        ticks = [x0, x1] if x0 != x1 else [x0]
    ax_gemv.set_xticks(ticks)


if args.chunk_size and args.chunk_size > 0:
    chunk = args.chunk_size
    starts = list(range(global_xmin, global_xmax + 1, chunk))
    for x0 in starts:
        x1 = min(x0 + chunk - 1, global_xmax)
        apply_x_window(x0, x1)
        out = f"timeseries_comparison_{trace_base}_vs_{log_base}{suffix}_cycles_{x0}_{x1}.png"
        fig.savefig(out, dpi=120, bbox_inches="tight")
        print(f"Saved → {out}")
else:
    if args.cycle_start is not None or args.cycle_end is not None:
        x0 = global_xmin if args.cycle_start is None else max(global_xmin, args.cycle_start)
        x1 = global_xmax if args.cycle_end is None else min(global_xmax, args.cycle_end)
        if x0 > x1:
            raise ValueError(f"Invalid cycle window: start={x0}, end={x1}")
        apply_x_window(x0, x1)
        out = f"timeseries_comparison_{trace_base}_vs_{log_base}{suffix}_cycles_{x0}_{x1}.png"
    else:
        apply_x_window(global_xmin, global_xmax)
        out = f"timeseries_comparison_{trace_base}_vs_{log_base}{suffix}.png"
    fig.savefig(out, dpi=120, bbox_inches="tight")
    print(f"Saved → {out}")
if args.align_steady_center:
    print(f"Alignment shift applied to simulator: {sim_shift:+d} cycles")
    if trace_steady and sim_steady:
        print(
            "Steady windows (trace/sim before shift): "
            f"{trace_steady[0]}-{trace_steady[1]} / {sim_steady[0]}-{sim_steady[1]}"
        )
