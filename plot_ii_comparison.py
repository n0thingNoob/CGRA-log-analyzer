"""
Compare simulator (gemv.json.log) vs RTL (trace_gemv_4x4_Mesh.jsonl)
operation timelines for the first 3 IIs, cycle-by-cycle.
"""

import json
import math
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ── colour palette ───────────────────────────────────────────────────────────
OP_COLORS = {
    # simulator opcode  : colour
    "GRANT_ONCE":       "#e6194b",
    "GRANT_PREDICATE":  "#f58231",
    "PHI_START":        "#ffe119",
    "DATA_MOV":         "#3cb44b",
    "CTRL_MOV":         "#42d4f4",
    "SHL":              "#4363d8",
    "ADD":              "#911eb4",
    "MUL":              "#f032e6",
    "GEP":              "#a9a9a9",
    "ICMP_EQ":          "#800000",
    "STORE":            "#9a6324",
    "LOAD":             "#469990",
    "NOT":              "#000075",
    "RETURN_VOID":      "#808000",
    # RTL operation_symbol
    "(grant_once')":    "#e6194b",
    "(grant_pred)":     "#f58231",
    "(ph*)":            "#ffe119",
    "NAH":              "#cccccc",   # catch-all NAH key
    "(NAH)":            "#cccccc",
    "(<<')":            "#4363d8",
    "(+')":             "#911eb4",
    "(+)":              "#3cb44b",
    "(*)":              "#f032e6",
    "(*)" :             "#f032e6",
    "(==')":            "#800000",
    "(ld)":             "#469990",
    "(st)":             "#9a6324",
    "(!)":              "#000075",
    "(ret_void)":       "#808000",
    "(start)":          "#ffffff",
}
DEFAULT_COLOR = "#dddddd"


def op_color(op):
    return OP_COLORS.get(op, DEFAULT_COLOR)


# ── parse simulator log ───────────────────────────────────────────────────────
sim_insts = []
with open("gemv.json.log") as f:
    for line in f:
        d = json.loads(line)
        if d.get("msg") == "Inst":
            sim_insts.append({
                "time": round(d["Time"]),
                "op":   d["OpCode"],
                "x":    d["X"],
                "y":    d["Y"],
            })

# Group by (time, tile)
sim_by_time = defaultdict(list)   # time -> list of (tile_label, op)
for inst in sim_insts:
    label = f"({inst['y']},{inst['x']})"
    sim_by_time[inst["time"]].append((label, inst["op"]))

# Detect II = period of repeating pattern
# Find the first time T≥1 where sorted ops at T == sorted ops at T+P for all P candidate
ref = 0
for ref_t in sorted(sim_by_time):
    # find next same-pattern time
    pat = sorted([op for _, op in sim_by_time[ref_t]])
    for t2 in sorted(sim_by_time):
        if t2 > ref_t:
            if sorted([op for _, op in sim_by_time[t2]]) == pat:
                SIM_II = t2 - ref_t
                SIM_START = ref_t          # first cycle of steady-state
                break
    else:
        continue
    break

print(f"Detected SIM II = {SIM_II}, starting at t={SIM_START}")

# Build sim IIs: II-1 = [SIM_START .. SIM_START+SIM_II-1], etc.
sim_ii_starts = [SIM_START + i * SIM_II for i in range(3)]

# ── parse RTL trace ───────────────────────────────────────────────────────────
rtl_records = []
with open("trace_gemv_4x4_Mesh.jsonl") as f:
    for line in f:
        rtl_records.append(json.loads(line))

# Find RTL II boundaries: cycles where ctrl_mem.times resets to a new multiple
# (addr goes back to 0 with new times value)
rtl_ii_start_cycles = []
prev_times = -1
for r in rtl_records:
    cm = r["tiles"][0]["ctrl_mem"]
    times = cm["times"]
    addr  = cm["addr"]
    if times != prev_times and addr == 0:
        rtl_ii_start_cycles.append(r["cycle"])
    prev_times = times

print(f"RTL II boundary cycles: {rtl_ii_start_cycles[:6]}")

# Map: rtl cycle -> dict[tile_label] = op_symbol
def rtl_ops_at_cycle(record):
    ops = {}
    for tile in record["tiles"]:
        label = f"({tile['row']},{tile['col']})"
        sym   = tile["fu"]["operation_symbol"]
        ops[label] = sym
    return ops

# Build lookup: cycle -> ops
rtl_by_cycle = {}
for r in rtl_records:
    rtl_by_cycle[r["cycle"]] = rtl_ops_at_cycle(r)

# All tile labels in row-major order (4×4)
ALL_TILES = [f"({r},{c})" for r in range(4) for c in range(4)]

# ── legend entries ────────────────────────────────────────────────────────────
UNIQUE_OPS_SIM = sorted({op for _, ops in sim_by_time.items() for _, op in ops})
UNIQUE_OPS_RTL = sorted({op for r in rtl_records
                          for t in r["tiles"]
                          for op in [t["fu"]["operation_symbol"]]})

all_ops = sorted(set(UNIQUE_OPS_SIM) | set(UNIQUE_OPS_RTL))
legend_patches = [
    mpatches.Patch(color=op_color(op), label=op)
    for op in all_ops if op not in ("(start)",)
]

# ── plotting ──────────────────────────────────────────────────────────────────
N_II = 3
fig, axes = plt.subplots(
    nrows=N_II * 2,   # SIM row + RTL row per II
    ncols=1,
    figsize=(32, N_II * 12),
    constrained_layout=False,
)
fig.subplots_adjust(hspace=0.65, left=0.08, right=0.86, top=0.97, bottom=0.02)

tile_y = {t: i for i, t in enumerate(ALL_TILES)}   # tile -> y index
Y_LABELS = ALL_TILES

for ii_idx in range(N_II):
    ax_sim = axes[ii_idx * 2]
    ax_rtl = axes[ii_idx * 2 + 1]

    # ── SIM subplot ──────────────────────────────────────────────────────────
    sim_t_start = sim_ii_starts[ii_idx]
    sim_steps   = list(range(sim_t_start, sim_t_start + SIM_II))

    ax_sim.set_xlim(-0.5, SIM_II - 0.5)
    ax_sim.set_ylim(-0.5, len(ALL_TILES) - 0.5)
    ax_sim.set_title(
        f"SIM  — II #{ii_idx+1}  (t={sim_t_start}..{sim_t_start+SIM_II-1},  {SIM_II} steps)",
        fontsize=11, fontweight="bold", loc="left",
    )
    ax_sim.set_xlabel("Simulator time step (within II)", fontsize=9)
    ax_sim.set_ylabel("Tile (row,col)", fontsize=9)
    ax_sim.set_xticks(range(SIM_II))
    ax_sim.set_xticklabels([str(s) for s in sim_steps], fontsize=7, rotation=45)
    ax_sim.set_yticks(range(len(ALL_TILES)))
    ax_sim.set_yticklabels(Y_LABELS, fontsize=7)
    ax_sim.grid(True, which="both", linestyle=":", linewidth=0.4, alpha=0.5)

    # Draw SIM ops as coloured squares
    for step_idx, t in enumerate(sim_steps):
        for tile_label, op in sim_by_time.get(t, []):
            if tile_label not in tile_y:
                continue
            yi = tile_y[tile_label]
            color = op_color(op)
            rect = plt.Rectangle(
                (step_idx - 0.45, yi - 0.45), 0.9, 0.9,
                color=color, alpha=0.85, linewidth=0,
            )
            ax_sim.add_patch(rect)
            ax_sim.text(step_idx, yi, op[:6], ha="center", va="center",
                        fontsize=5, color="black")

    # ── RTL subplot ──────────────────────────────────────────────────────────
    rtl_c_start = rtl_ii_start_cycles[ii_idx]
    rtl_c_end   = rtl_ii_start_cycles[ii_idx + 1] if ii_idx + 1 < len(rtl_ii_start_cycles) else rtl_ii_start_cycles[ii_idx] + 1
    rtl_cycles  = list(range(rtl_c_start, rtl_c_end))
    n_rtl       = len(rtl_cycles)

    ax_rtl.set_xlim(-0.5, n_rtl - 0.5)
    ax_rtl.set_ylim(-0.5, len(ALL_TILES) - 0.5)
    ax_rtl.set_title(
        f"RTL  — II #{ii_idx+1}  (cycle {rtl_c_start}..{rtl_c_end-1},  {n_rtl} cycles  "
        f"[overhead vs SIM: +{n_rtl - SIM_II} cycles])",
        fontsize=11, fontweight="bold", loc="left",
    )
    ax_rtl.set_xlabel("RTL cycle (absolute)", fontsize=9)
    ax_rtl.set_ylabel("Tile (row,col)", fontsize=9)

    # X-ticks: every 5 cycles or every cycle if short
    tick_step = max(1, n_rtl // 30)
    ax_rtl.set_xticks(range(0, n_rtl, tick_step))
    ax_rtl.set_xticklabels(
        [str(rtl_c_start + i) for i in range(0, n_rtl, tick_step)],
        fontsize=6, rotation=45,
    )
    ax_rtl.set_yticks(range(len(ALL_TILES)))
    ax_rtl.set_yticklabels(Y_LABELS, fontsize=7)
    ax_rtl.grid(True, which="both", linestyle=":", linewidth=0.4, alpha=0.5)

    # Draw RTL ops
    for local_idx, c in enumerate(rtl_cycles):
        ops_here = rtl_by_cycle.get(c, {})
        for tile_label, op in ops_here.items():
            if op == "(start)":
                continue
            if tile_label not in tile_y:
                continue
            yi = tile_y[tile_label]
            color = op_color(op)
            alpha = 0.3 if op == "(NAH)" else 0.85
            rect = plt.Rectangle(
                (local_idx - 0.45, yi - 0.45), 0.9, 0.9,
                color=color, alpha=alpha, linewidth=0,
            )
            ax_rtl.add_patch(rect)
            if op != "(NAH)":
                ax_rtl.text(local_idx, yi, op[:6], ha="center", va="center",
                            fontsize=5, color="black")

    # Overlay ctrl_mem addr (program counter) as a line on the RTL plot
    # Build a fast cycle→addr lookup (only for cycles in this II)
    rtl_addr_by_cycle = {
        rec["cycle"]: rec["tiles"][0]["ctrl_mem"]["addr"]
        for rec in rtl_records
        if rtl_c_start <= rec["cycle"] < rtl_c_end
    }

    addrs_local = []
    for local_idx, c in enumerate(rtl_cycles):
        if c in rtl_addr_by_cycle:
            addrs_local.append((local_idx, rtl_addr_by_cycle[c]))

    if addrs_local:
        xs, ys = zip(*addrs_local)
        # Normalise addr (0..10) → tile y-axis scale for overlay
        # Draw on a twin axis
        ax2 = ax_rtl.twinx()
        ax2.plot(xs, ys, color="navy", linewidth=1.5, linestyle="-", alpha=0.7,
                 label="ctrl PC (addr)")
        ax2.set_ylabel("ctrl_mem addr (PC)", fontsize=7, color="navy")
        ax2.tick_params(axis="y", labelcolor="navy", labelsize=6)
        ax2.set_ylim(-0.5, 11.5)
        ax2.legend(loc="upper right", fontsize=7)

    # Mark stall regions: consecutive cycles with same ctrl_mem addr, annotate duration
    local_addr = {li: rtl_addr_by_cycle[c] for li, c in enumerate(rtl_cycles) if c in rtl_addr_by_cycle}

    prev_addr_val = None
    stall_start_local = None
    stall_addr_val = None
    for local_idx in range(len(rtl_cycles)):
        cur_addr = local_addr.get(local_idx)
        if cur_addr == prev_addr_val:
            if stall_start_local is None:
                stall_start_local = local_idx - 1
                stall_addr_val = cur_addr
        else:
            if stall_start_local is not None and local_idx - stall_start_local > 1:
                stall_len = local_idx - stall_start_local
                ax_rtl.axvspan(
                    stall_start_local - 0.5, local_idx - 0.5,
                    color="red", alpha=0.10, zorder=0,
                )
                mid = (stall_start_local + local_idx) / 2
                ax_rtl.text(
                    mid, len(ALL_TILES) - 0.1,
                    f"stall addr={stall_addr_val}\n{stall_len}cy",
                    ha="center", va="top", fontsize=6, color="darkred",
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="red", alpha=0.7),
                )
            stall_start_local = None
        prev_addr_val = cur_addr
    # Close any open stall at end
    if stall_start_local is not None:
        stall_len = len(rtl_cycles) - stall_start_local
        ax_rtl.axvspan(
            stall_start_local - 0.5, len(rtl_cycles) - 0.5,
            color="red", alpha=0.10, zorder=0,
        )
        mid = (stall_start_local + len(rtl_cycles)) / 2
        ax_rtl.text(
            mid, len(ALL_TILES) - 0.1,
            f"stall addr={stall_addr_val}\n{stall_len}cy",
            ha="center", va="top", fontsize=6, color="darkred",
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="red", alpha=0.7),
        )

# ── shared legend ─────────────────────────────────────────────────────────────
fig.legend(
    handles=legend_patches,
    loc="center right",
    bbox_to_anchor=(0.99, 0.5),
    fontsize=8,
    title="Operation",
    title_fontsize=9,
    framealpha=0.9,
    ncol=1,
)

out = "ii_comparison.png"
fig.savefig(out, dpi=150, bbox_inches="tight")
print(f"Saved → {out}")
