#!/usr/bin/env python3
"""Analyze VectorCGRA JSONL traces and export stage-cycle CSV files."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

IDLE_OPS = {"(start)", "(NAH)"}
CONTROL_LIKE_OPS = {
    "(start)",
    "(NAH)",
    "(grant_pred)",
    "(grant_once)",
    "(grant_once')",
    "(ret_void)",
}


@dataclass
class CycleStats:
    cycle: int
    global_times: int
    global_addr_max: int
    started_tiles: int
    complete_tiles: int
    active_tiles: int
    dominant_op: str
    op_breakdown: str
    has_kernel_op: bool
    has_effective_data: bool


@dataclass
class Segment:
    stage_id: int
    times_value: int
    start_cycle: int
    end_cycle: int
    duration_cycles: int
    started_tiles_min: int
    started_tiles_max: int
    complete_tiles_min: int
    complete_tiles_max: int
    active_tiles_avg: float
    active_tiles_max: int
    dominant_op: str
    top_ops: str
    addr_max_min: int
    addr_max_max: int


@dataclass
class CoarseStage:
    log_name: str
    stage_name: str
    start_cycle: int
    end_cycle: int
    total_cycles: int
    note: str


def tile_has_effective_data(tile: dict) -> bool:
    """Data-valid heuristic: FU/memory payload val is asserted, not just control ops running."""

    vals: list[int] = []
    vals += [x.get("val", 0) for x in tile.get("fu", {}).get("inputs", [])]
    vals += [x.get("val", 0) for x in tile.get("fu", {}).get("outputs", [])]

    const_obj = tile.get("fu", {}).get("const", {})
    if isinstance(const_obj, dict):
        vals.append(const_obj.get("val", 0))

    mem = tile.get("mem_access", {})
    for k in ("rdata", "wdata"):
        obj = mem.get(k)
        if isinstance(obj, dict):
            vals.append(obj.get("val", 0))

    return any(v == 1 for v in vals)


def summarize_cycle(record: dict) -> CycleStats:
    tiles = record["tiles"]
    started = [t["ctrl_mem"]["started"] for t in tiles]
    complete = [t["ctrl_mem"]["complete"] for t in tiles]
    times = [t["ctrl_mem"]["times"] for t in tiles]
    addrs = [t["ctrl_mem"]["addr"] for t in tiles]

    ops = [t["fu"]["operation_symbol"] for t in tiles]
    active_ops = [op for op in ops if op not in IDLE_OPS]
    op_counter = Counter(active_ops)

    has_kernel_op = any(op not in CONTROL_LIKE_OPS for op in ops)

    has_effective_data = False
    for tile in tiles:
        op = tile["fu"]["operation_symbol"]
        if op in CONTROL_LIKE_OPS:
            continue
        if tile_has_effective_data(tile):
            has_effective_data = True
            break

    if op_counter:
        dominant_op, _ = op_counter.most_common(1)[0]
        op_breakdown = "|".join(f"{k}:{v}" for k, v in op_counter.most_common(6))
    else:
        dominant_op = "idle"
        op_breakdown = ""

    return CycleStats(
        cycle=record["cycle"],
        global_times=max(times),
        global_addr_max=max(addrs),
        started_tiles=sum(started),
        complete_tiles=sum(complete),
        active_tiles=len(active_ops),
        dominant_op=dominant_op,
        op_breakdown=op_breakdown,
        has_kernel_op=has_kernel_op,
        has_effective_data=has_effective_data,
    )


def load_cycle_stats(path: Path) -> list[CycleStats]:
    stats: list[CycleStats] = []
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            stats.append(summarize_cycle(json.loads(line)))
    return stats


def segment_by_times(cycles: Iterable[CycleStats]) -> list[Segment]:
    cycles = list(cycles)
    if not cycles:
        return []

    segments: list[Segment] = []
    buf: list[CycleStats] = [cycles[0]]

    def flush(stage_id: int, chunk: list[CycleStats]) -> Segment:
        op_counter = Counter(c.dominant_op for c in chunk if c.dominant_op != "idle")
        dominant_op = op_counter.most_common(1)[0][0] if op_counter else "idle"

        op_merged = Counter()
        for c in chunk:
            if not c.op_breakdown:
                continue
            for kv in c.op_breakdown.split("|"):
                op, n = kv.rsplit(":", 1)
                op_merged[op] += int(n)

        top_ops = "|".join(f"{k}:{v}" for k, v in op_merged.most_common(6))

        return Segment(
            stage_id=stage_id,
            times_value=chunk[0].global_times,
            start_cycle=chunk[0].cycle,
            end_cycle=chunk[-1].cycle,
            duration_cycles=chunk[-1].cycle - chunk[0].cycle + 1,
            started_tiles_min=min(c.started_tiles for c in chunk),
            started_tiles_max=max(c.started_tiles for c in chunk),
            complete_tiles_min=min(c.complete_tiles for c in chunk),
            complete_tiles_max=max(c.complete_tiles for c in chunk),
            active_tiles_avg=round(sum(c.active_tiles for c in chunk) / len(chunk), 3),
            active_tiles_max=max(c.active_tiles for c in chunk),
            dominant_op=dominant_op,
            top_ops=top_ops,
            addr_max_min=min(c.global_addr_max for c in chunk),
            addr_max_max=max(c.global_addr_max for c in chunk),
        )

    for c in cycles[1:]:
        if c.global_times == buf[-1].global_times:
            buf.append(c)
        else:
            segments.append(flush(len(segments), buf))
            buf = [c]

    segments.append(flush(len(segments), buf))
    return segments


def infer_kernel_label(log_stem: str) -> str:
    stem = log_stem.lower()
    if "gemm" in stem:
        return "gemm_execution"
    if "gemv" in stem:
        return "gemv_execution"
    return "kernel_execution"


def _append_stage(rows: list[CoarseStage], log_name: str, stage_name: str, cycles: list[int], note: str) -> None:
    if not cycles:
        return
    rows.append(
        CoarseStage(
            log_name=log_name,
            stage_name=stage_name,
            start_cycle=min(cycles),
            end_cycle=max(cycles),
            total_cycles=len(cycles),
            note=note,
        )
    )


def build_coarse_stages(log_name: str, cycles: list[CycleStats]) -> list[CoarseStage]:
    if not cycles:
        return []

    rows: list[CoarseStage] = []
    all_cycles = [c.cycle for c in cycles]
    kernel_cycles = [c.cycle for c in cycles if c.has_kernel_op]

    if not kernel_cycles:
        _append_stage(rows, log_name, "other", all_cycles, "No kernel FU op found")
        return rows

    kernel_start = min(kernel_cycles)
    kernel_end = max(kernel_cycles)

    setup_cycles = [c.cycle for c in cycles if c.cycle < kernel_start]
    _append_stage(
        rows,
        log_name,
        "configuration_or_setup",
        setup_cycles,
        "Before first non-control kernel instruction",
    )

    label = infer_kernel_label(log_name)
    effective = [c.cycle for c in cycles if c.has_kernel_op and c.has_effective_data]
    bubble = [c.cycle for c in cycles if c.has_kernel_op and not c.has_effective_data]

    _append_stage(
        rows,
        log_name,
        f"{label}_with_data",
        effective,
        "Kernel instruction + FU/memory data val asserted (effective execution cycles)",
    )
    _append_stage(
        rows,
        log_name,
        f"{label}_no_data",
        bubble,
        "Kernel instruction seen but no FU/memory data val (bubbles/overhead)",
    )

    tail_cycles = [c.cycle for c in cycles if c.cycle > kernel_end]
    complete_cycles = [c.cycle for c in cycles if c.complete_tiles > 0]
    tail_note = "After kernel instruction span"
    if complete_cycles:
        tail_note += f"; completion flag first at {min(complete_cycles)}"
    _append_stage(rows, log_name, "finalize_or_other", tail_cycles, tail_note)

    # Add one compact span row so simulator owners can compare with original window-based accounting.
    rows.append(
        CoarseStage(
            log_name=log_name,
            stage_name=f"{label}_span_window",
            start_cycle=kernel_start,
            end_cycle=kernel_end,
            total_cycles=kernel_end - kernel_start + 1,
            note="Continuous window from first to last kernel instruction",
        )
    )

    return rows


def write_csv(path: Path, segments: list[Segment]) -> None:
    fieldnames = [
        "stage_id",
        "times_value",
        "start_cycle",
        "end_cycle",
        "duration_cycles",
        "started_tiles_min",
        "started_tiles_max",
        "complete_tiles_min",
        "complete_tiles_max",
        "active_tiles_avg",
        "active_tiles_max",
        "dominant_op",
        "top_ops",
        "addr_max_min",
        "addr_max_max",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for seg in segments:
            writer.writerow(seg.__dict__)


def write_coarse_summary(path: Path, rows: list[CoarseStage]) -> None:
    fieldnames = ["log_name", "stage_name", "start_cycle", "end_cycle", "total_cycles", "note"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze stage cycles from VectorCGRA trace JSONL files.")
    parser.add_argument("inputs", nargs="+", type=Path, help="Input trace jsonl files")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("analysis_csv"),
        help="Directory for output CSV files (default: analysis_csv)",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    coarse_rows: list[CoarseStage] = []

    for input_path in args.inputs:
        cycles = load_cycle_stats(input_path)
        segments = segment_by_times(cycles)
        coarse_rows.extend(build_coarse_stages(input_path.stem, cycles))

        detail_out = args.output_dir / f"{input_path.stem}_stage_cycles.csv"
        write_csv(detail_out, segments)
        print(f"{input_path} -> {detail_out} ({len(segments)} detailed stages)")

    summary_out = args.output_dir / "stage_cycle_summary.csv"
    write_coarse_summary(summary_out, coarse_rows)
    print(f"coarse summary -> {summary_out} ({len(coarse_rows)} rows)")


if __name__ == "__main__":
    main()
