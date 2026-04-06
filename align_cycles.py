#!/usr/bin/env python3
"""Align cycle counts between behavioral simulator (.json.log) and RTL trace (.jsonl).

Usage:
    python align_cycles.py --behavioral gemv.json.log --trace trace_gemv_4x4_Mesh.jsonl \
        --kernel auto --output-dir analysis_csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Map behavioral OpCode -> RTL operation symbol
BEHAVIORAL_TO_RTL: Dict[str, str] = {
    "MUL": "(*)",
    "STORE": "(st)",
    "ADD": "(+)",
    "LOAD": "(ld)",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Align behavioral and RTL cycle counts.")
    p.add_argument("--behavioral", required=True, help="Path to behavioral .json.log file")
    p.add_argument("--trace", required=True, help="Path to RTL .jsonl trace file")
    p.add_argument("--kernel", choices=["auto", "gemv", "gemm"], default="auto")
    p.add_argument("--output-dir", default="analysis_csv")
    p.add_argument(
        "--ops",
        default="MUL,STORE,ADD",
        help="Comma-separated behavioral OpCodes to align (default: MUL,STORE,ADD)",
    )
    return p.parse_args()


def infer_kernel(behavioral: str, kernel_flag: str) -> str:
    if kernel_flag != "auto":
        return kernel_flag
    name = Path(behavioral).name.lower()
    if "gemm" in name:
        return "gemm"
    return "gemv"


# ---------------------------------------------------------------------------
# Behavioral log parsing
# ---------------------------------------------------------------------------

def _behavioral_pred_true(rec: dict) -> bool:
    """Return True if the behavioral Inst record represents a predicated-true execution.

    Two formats exist:
    - Direct ``Pred`` boolean field (e.g. STORE, CTRL_MOV).
    - Result-encoded predicate (e.g. MUL, ADD): ``Result`` value ends with ``(true)``.
    """
    if "Pred" in rec:
        return bool(rec["Pred"])
    result_str = rec.get("Result", "")
    if isinstance(result_str, str):
        return result_str.endswith("(true)")
    return False


def extract_behavioral_events(log_path: str, target_ops: List[str]) -> Dict[str, List[int]]:
    """Return sorted Time lists for Inst events with given OpCodes and Pred=True."""
    result: Dict[str, List[int]] = {op: [] for op in target_ops}
    with open(log_path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("msg") != "Inst":
                continue
            if not _behavioral_pred_true(rec):
                continue
            opcode = rec.get("OpCode", "")
            if opcode in result:
                result[opcode].append(round(rec["Time"]))
    for op in result:
        result[op].sort()
    return result


# ---------------------------------------------------------------------------
# RTL trace parsing (mirrors extract_main_window.py valid_event_from_tile)
# ---------------------------------------------------------------------------

def _valid_event_from_tile(tile: dict) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    """Return (source, payload, predicate) if the tile has a valid handshake, else None."""
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

    for out in tile.get("fu", {}).get("outputs", []):
        if out.get("val", 0) == 1 and out.get("rdy", 0) == 1:
            return "fu", out.get("payload"), out.get("predicate")
    return None


def extract_rtl_events(trace_path: str, target_ops: List[str]) -> Dict[str, List[int]]:
    """Return sorted cycle lists for valid RTL events with given operation symbols."""
    result: Dict[str, List[int]] = {op: [] for op in target_ops}
    with open(trace_path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            cycle = rec["cycle"]
            for tile in rec["tiles"]:
                op = tile["fu"]["operation_symbol"]
                if op not in result:
                    continue
                if _valid_event_from_tile(tile) is None:
                    continue
                result[op].append(cycle)
    for op in result:
        result[op].sort()
    return result


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def _median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return (s[mid - 1] + s[mid]) / 2 if n % 2 == 0 else s[mid]


def _linear_fit(xs: List[float], ys: List[float]) -> Tuple[float, float, float]:
    """Least-squares fit y = slope*x + intercept; returns (slope, intercept, r_squared)."""
    n = len(xs)
    if n < 2:
        return (0.0, float(ys[0]) if ys else 0.0, 0.0)

    sx = sum(xs)
    sy = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sxx = sum(x * x for x in xs)

    denom = n * sxx - sx * sx
    if denom == 0:
        return (0.0, sy / n, 0.0)

    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n

    y_mean = sy / n
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot != 0 else 1.0

    return slope, intercept, r_squared


def _steady_interval_ratio(
    behav_times: List[int], rtl_cycles: List[int], n: int
) -> Optional[float]:
    """Median interval ratio across all consecutive aligned pairs."""
    ratios: List[float] = []
    for i in range(1, n):
        db = behav_times[i] - behav_times[i - 1]
        dr = rtl_cycles[i] - rtl_cycles[i - 1]
        if db > 0:
            ratios.append(dr / db)
    if not ratios:
        return None
    return _median(ratios)


# ---------------------------------------------------------------------------
# Per-op alignment
# ---------------------------------------------------------------------------

def align_op(
    behav_times: List[int],
    rtl_cycles: List[int],
    behavioral_op: str,
    rtl_op: str,
) -> Tuple[List[dict], dict]:
    """1:1 sequential matching for one operation type; return (anchors, summary)."""
    n = min(len(behav_times), len(rtl_cycles))

    anchors: List[dict] = []
    for idx in range(n):
        bt = behav_times[idx]
        rc = rtl_cycles[idx]
        ratio = rc / bt if bt != 0 else None
        if idx > 0:
            db = behav_times[idx] - behav_times[idx - 1]
            dr = rtl_cycles[idx] - rtl_cycles[idx - 1]
            interval_ratio: Optional[float] = dr / db if db != 0 else None
        else:
            interval_ratio = None
        anchors.append(
            {
                "op": behavioral_op,
                "idx": idx,
                "behavioral_time": bt,
                "rtl_cycle": rc,
                "ratio": round(ratio, 6) if ratio is not None else None,
                "interval_ratio": round(interval_ratio, 6) if interval_ratio is not None else None,
            }
        )

    valid_ratios = [a["ratio"] for a in anchors if a["ratio"] is not None]
    overall_ratio = sum(valid_ratios) / len(valid_ratios) if valid_ratios else None

    steady_ratio = _steady_interval_ratio(behav_times[:n], rtl_cycles[:n], n)

    slope, intercept, r_squared = _linear_fit(
        [float(t) for t in behav_times[:n]],
        [float(c) for c in rtl_cycles[:n]],
    )

    summary = {
        "behavioral_op": behavioral_op,
        "rtl_op": rtl_op,
        "behavioral_count": len(behav_times),
        "rtl_count": len(rtl_cycles),
        "aligned_count": n,
        "overall_ratio": round(overall_ratio, 6) if overall_ratio is not None else None,
        "steady_state_interval_ratio": round(steady_ratio, 6) if steady_ratio is not None else None,
        "linear_fit": {
            "slope": round(slope, 6),
            "intercept": round(intercept, 6),
            "r_squared": round(r_squared, 6),
        },
    }
    return anchors, summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    kernel = infer_kernel(args.behavioral, args.kernel)
    os.makedirs(args.output_dir, exist_ok=True)

    target_behav_ops = [op.strip() for op in args.ops.split(",") if op.strip()]
    target_rtl_ops = [BEHAVIORAL_TO_RTL[op] for op in target_behav_ops if op in BEHAVIORAL_TO_RTL]

    print(f"Kernel       : {kernel}")
    print(f"Behavioral   : {args.behavioral}")
    print(f"RTL trace    : {args.trace}")
    print(f"Aligning ops : {target_behav_ops}")

    print("\nExtracting behavioral events …")
    behav_events = extract_behavioral_events(args.behavioral, target_behav_ops)

    print("Extracting RTL events …")
    rtl_events = extract_rtl_events(args.trace, target_rtl_ops)

    print("\nEvent counts:")
    for bop in target_behav_ops:
        rop = BEHAVIORAL_TO_RTL.get(bop, "?")
        print(f"  {bop:6s} ({rop:4s})  behavioral={len(behav_events.get(bop, []))}  RTL={len(rtl_events.get(rop, []))}")

    all_anchors: List[dict] = []
    op_summaries: Dict[str, dict] = {}

    for behavioral_op in target_behav_ops:
        rtl_op = BEHAVIORAL_TO_RTL.get(behavioral_op)
        if rtl_op is None:
            continue
        bt_list = behav_events.get(behavioral_op, [])
        rc_list = rtl_events.get(rtl_op, [])

        if not bt_list or not rc_list:
            print(f"\n  {behavioral_op}: no events – skipped")
            continue

        anchors, summary = align_op(bt_list, rc_list, behavioral_op, rtl_op)
        all_anchors.extend(anchors)
        op_summaries[behavioral_op] = summary

        lf = summary["linear_fit"]
        print(
            f"\n  {behavioral_op}: aligned={summary['aligned_count']}  "
            f"overall_ratio={summary['overall_ratio']}  "
            f"steady_interval_ratio={summary['steady_state_interval_ratio']}  "
            f"R²={lf['r_squared']}  slope={lf['slope']}  intercept={lf['intercept']}"
        )

    # Write anchors CSV
    anchors_path = os.path.join(args.output_dir, f"alignment_anchors_{kernel}.csv")
    if all_anchors:
        fieldnames = ["op", "idx", "behavioral_time", "rtl_cycle", "ratio", "interval_ratio"]
        with open(anchors_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_anchors)
        print(f"\nWrote: {anchors_path}  ({len(all_anchors)} rows)")

    # Build summary JSON
    # Top-level convenience fields taken from primary op (MUL preferred, else first)
    primary_op = "MUL" if "MUL" in op_summaries else (next(iter(op_summaries), None))
    summary_data: dict = {
        "kernel": kernel,
        "behavioral_log": args.behavioral,
        "rtl_trace": args.trace,
        "ops": op_summaries,
    }
    if primary_op and primary_op in op_summaries:
        ps = op_summaries[primary_op]
        summary_data["overall_ratio"] = ps["overall_ratio"]
        summary_data["steady_state_interval_ratio"] = ps["steady_state_interval_ratio"]
        summary_data["linear_fit"] = ps["linear_fit"]

    summary_path = os.path.join(args.output_dir, f"alignment_summary_{kernel}.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2)
    print(f"Wrote: {summary_path}")


if __name__ == "__main__":
    main()
