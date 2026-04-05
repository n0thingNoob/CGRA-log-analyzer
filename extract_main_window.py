#!/usr/bin/env python3
"""Extract main execution window from VectorCGRA JSONL traces (GEMV/GEMM)."""
from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

IDLE_OPS = {"(start)", "(NAH)", "(grant_pred)", "(grant_once)", "(grant_once')", "(ret_void)"}
ARITH_OPS = {"(*)", "(+)", "(&)", "(+')"}

DEFAULT_II_BY_KERNEL = {
    "gemv": 11,
    "gemm": 25,
}


@dataclass
class Event:
    cycle: int
    tile_id: int
    row: int
    col: int
    op: str
    kind: str
    source: str
    payload: Optional[int]
    predicate: Optional[int]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--trace", required=True, help="Path to PyRTL JSONL trace")
    p.add_argument("--kernel", choices=["auto", "gemv", "gemm"], default="auto")
    p.add_argument("--compiled-ii", type=int, default=None)
    p.add_argument("--out-dir", default="extract_main_window_out")
    p.add_argument("--mode", choices=["exec", "math"], default="exec")
    p.add_argument("--ii-gap-slack", type=int, default=4)
    p.add_argument("--steady-run", type=int, default=3)
    p.add_argument("--lookback-ii-multiple", type=int, default=8)
    p.add_argument("--manual-start", type=int, default=None)
    p.add_argument("--manual-end", type=int, default=None)
    return p.parse_args()


def infer_kernel(trace: str, kernel_flag: str) -> str:
    if kernel_flag != "auto":
        return kernel_flag
    name = Path(trace).name.lower()
    if "gemm" in name:
        return "gemm"
    return "gemv"


def valid_fu_output(tile: Dict[str, Any]) -> Tuple[bool, Optional[int], Optional[int]]:
    for out in tile.get("fu", {}).get("outputs", []):
        if out.get("val", 0) == 1 and out.get("rdy", 0) == 1:
            return True, out.get("payload"), out.get("predicate")
    return False, None, None


def valid_event_from_tile(tile: Dict[str, Any]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    op = tile["fu"]["operation_symbol"]

    if op == "(ld)":
        rd = tile.get("mem_access", {}).get("rdata", {})
        if rd.get("val", 0) == 1 and rd.get("rdy", 0) == 1:
            return "mem_r", rd.get("payload"), rd.get("predicate")
        return None

    if op == "(st)":
        wd = tile.get("mem_access", {}).get("wdata", {})
        if wd.get("val", 0) == 1 and wd.get("rdy", 0) == 1:
            return "mem_w", wd.get("payload"), wd.get("predicate")
        return None

    ok, payload, pred = valid_fu_output(tile)
    if ok:
        return "fu", payload, pred
    return None


def classify_kind(op: str) -> str:
    if op in {"(ld)", "(st)"} or op in ARITH_OPS:
        return "math"
    return "ctrl"


def allow_event(op: str, mode: str) -> bool:
    if op in IDLE_OPS:
        return False
    if mode == "math":
        return op in {"(ld)", "(st)"} or op in ARITH_OPS
    return True


def extract_events(trace_path: str, mode: str) -> List[Event]:
    events: List[Event] = []
    with open(trace_path, "r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            cycle = rec["cycle"]
            for tile in rec["tiles"]:
                op = tile["fu"]["operation_symbol"]
                if not allow_event(op, mode):
                    continue
                valid = valid_event_from_tile(tile)
                if valid is None:
                    continue
                source, payload, predicate = valid
                events.append(
                    Event(
                        cycle=cycle,
                        tile_id=int(tile["id"]),
                        row=int(tile["row"]),
                        col=int(tile["col"]),
                        op=op,
                        kind=classify_kind(op),
                        source=source,
                        payload=payload,
                        predicate=predicate,
                    )
                )
    return events


def get_cycles(events: Sequence[Event], op: str) -> List[int]:
    return [e.cycle for e in events if e.op == op]


def find_steady_anchor_cycles(events: Sequence[Event], compiled_ii: int, gap_slack: int, steady_run: int) -> int:
    # pick densest arithmetic op as cadence anchor
    best = []
    for op in ["(*)", "(&)", "(+)", "(+')"]:
        cyc = sorted(get_cycles(events, op))
        if len(cyc) >= steady_run + 1:
            best.append((len(cyc), cyc))
    if not best:
        return -1

    cycles = sorted(best, reverse=True)[0][1]
    lo, hi = compiled_ii, compiled_ii + gap_slack
    for i in range(len(cycles) - steady_run):
        gaps = [cycles[i + j + 1] - cycles[i + j] for j in range(steady_run)]
        if all(lo <= g <= hi for g in gaps):
            return cycles[i]
    return cycles[0]


def find_exec_start(events: Sequence[Event], steady_anchor: int, compiled_ii: int, lookback_multiple: int) -> int:
    ld_cycles = [e.cycle for e in events if e.op == "(ld)"]
    if steady_anchor < 0:
        return min(ld_cycles) if ld_cycles else min(e.cycle for e in events)

    begin = steady_anchor - lookback_multiple * compiled_ii
    local_ld = [c for c in ld_cycles if begin <= c <= steady_anchor]
    if local_ld:
        return min(local_ld)
    fallback = [e.cycle for e in events if begin <= e.cycle <= steady_anchor]
    return min(fallback) if fallback else (min(ld_cycles) if ld_cycles else min(e.cycle for e in events))


def find_exec_end(events: Sequence[Event]) -> int:
    st_cycles = [e.cycle for e in events if e.op == "(st)"]
    return max(st_cycles) if st_cycles else max(e.cycle for e in events)


def filter_window(events: Sequence[Event], start: int, end: int) -> List[Event]:
    return [e for e in events if start <= e.cycle <= end]


def dump_events_csv(events: Sequence[Event], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["cycle", "tile_id", "row", "col", "op", "kind", "source", "payload", "predicate"])
        writer.writeheader()
        for e in events:
            writer.writerow(asdict(e))


def dump_windowed_trace(trace_path: str, out_path: str, start: int, end: int) -> None:
    with open(trace_path, "r", encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            rec = json.loads(line)
            if start <= rec["cycle"] <= end:
                fout.write(json.dumps(rec) + "\n")


def summarize(events: Sequence[Event]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for e in events:
        k = f"tile{e.tile_id}:{e.op}"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def main() -> None:
    args = parse_args()
    kernel = infer_kernel(args.trace, args.kernel)
    compiled_ii = args.compiled_ii if args.compiled_ii is not None else DEFAULT_II_BY_KERNEL[kernel]

    os.makedirs(args.out_dir, exist_ok=True)
    exec_events = extract_events(args.trace, mode="exec")
    if not exec_events:
        raise RuntimeError("No valid execution events extracted.")
    math_events = extract_events(args.trace, mode="math")

    steady_anchor = find_steady_anchor_cycles(exec_events, compiled_ii, args.ii_gap_slack, args.steady_run)
    auto_start = find_exec_start(exec_events, steady_anchor, compiled_ii, args.lookback_ii_multiple)
    auto_end = find_exec_end(exec_events)

    start = args.manual_start if args.manual_start is not None else auto_start
    end = args.manual_end if args.manual_end is not None else auto_end
    if start > end:
        raise ValueError(f"Invalid window: start={start} > end={end}")

    wexec = filter_window(exec_events, start, end)
    wmath = filter_window(math_events, start, end)

    dump_events_csv(exec_events, os.path.join(args.out_dir, "all_exec_events.csv"))
    dump_events_csv(math_events, os.path.join(args.out_dir, "all_math_events.csv"))
    dump_events_csv(wexec, os.path.join(args.out_dir, "window_exec_events.csv"))
    dump_events_csv(wmath, os.path.join(args.out_dir, "window_math_events.csv"))
    dump_windowed_trace(args.trace, os.path.join(args.out_dir, "window_trace.jsonl"), start, end)

    summary = {
        "trace": os.path.abspath(args.trace),
        "kernel": kernel,
        "compiled_ii": compiled_ii,
        "steady_anchor": steady_anchor,
        "auto_window": {"start": auto_start, "end": auto_end, "span_exclusive": auto_end - auto_start, "span_inclusive": auto_end - auto_start + 1},
        "final_window": {"start": start, "end": end, "span_exclusive": end - start, "span_inclusive": end - start + 1},
        "event_count_in_window": {"exec": len(wexec), "math": len(wmath)},
        "window_exec_event_histogram": summarize(wexec),
        "window_math_event_histogram": summarize(wmath),
    }

    with open(os.path.join(args.out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
