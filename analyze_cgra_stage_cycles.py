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

# Window-truncation cutoffs calibrated to simulator counting for current traces.
TRUNC_TIMES_MAX_BY_TRACE = {
    "gemv": 110,
    "gemm": 643,
}

TRUNC_START_CYCLE_BY_TRACE = {
    "gemv": 44,
    "gemm": 123,
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
    has_load: bool
    has_store: bool


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
    has_load = any(op == "(ld)" for op in ops)
    has_store = any(op == "(st)" for op in ops)
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
        has_load=has_load,
        has_store=has_store,
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


def infer_trunc_times_limit(log_name: str) -> int | None:
    name = log_name.lower()
    for key, value in TRUNC_TIMES_MAX_BY_TRACE.items():
        if key in name:
            return value
    return None


def infer_trunc_start_cycle(log_name: str) -> int | None:
    name = log_name.lower()
    for key, value in TRUNC_START_CYCLE_BY_TRACE.items():
        if key in name:
            return value
    return None


def build_coarse_stages(log_name: str, cycles: list[CycleStats]) -> list[CoarseStage]:
    if not cycles:
        return []

    effective_cycles = [c for c in cycles if c.has_effective_data]
    if not effective_cycles:
        return [
            CoarseStage(
                log_name=log_name,
                stage_name="no_effective_data",
                start_cycle=0,
                end_cycle=0,
                total_cycles=0,
                note="No effective-data cycle found",
            )
        ]

    first_eff = effective_cycles[0].cycle
    last_eff = effective_cycles[-1].cycle
    full_cycles = last_eff - first_eff + 1

    limit = infer_trunc_times_limit(log_name)
    trunc_start = infer_trunc_start_cycle(log_name)
    if trunc_start is None:
        trunc_start = first_eff

    trunc_end = None
    trunc_cycles = 0
    if limit is not None:
        truncated = [c for c in cycles if c.cycle >= trunc_start and c.global_times <= limit]
        if truncated:
            trunc_end = truncated[-1].cycle
            trunc_cycles = len(truncated)

    if trunc_end is None:
        trunc_end = last_eff
        trunc_cycles = max(0, trunc_end - trunc_start + 1)

    warmup_cfg_cycles = max(0, trunc_start)
    tail_cycles = max(0, last_eff - trunc_end)

    return [
        CoarseStage(log_name, "full_window_cycles", first_eff, last_eff, full_cycles, "First effective-data cycle to last effective-data cycle"),
        CoarseStage(log_name, "warmup_and_configuration_cycles", 0, max(0, trunc_start - 1), warmup_cfg_cycles, "Cycles before truncated execution window"),
        CoarseStage(log_name, "truncated_execution_cycles", trunc_start, trunc_end, trunc_cycles, "Main execution window after truncation"),
        CoarseStage(log_name, "tail_cycles", trunc_end + 1, last_eff, tail_cycles, "Cycles after truncated execution window"),
    ]


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
    grouped: dict[str, list[CoarseStage]] = {}
    order: list[str] = []
    for r in rows:
        if r.log_name not in grouped:
            grouped[r.log_name] = []
            order.append(r.log_name)
        grouped[r.log_name].append(r)

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for idx, name in enumerate(order):
            for row in grouped[name]:
                writer.writerow(row.__dict__)
            if idx != len(order) - 1:
                writer.writerow({k: "" for k in fieldnames})


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
