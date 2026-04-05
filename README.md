# CGRA-log-analyzer

分析 VectorCGRA RTL 仿真导出的 `jsonl` log。

## 用法

```bash
python analyze_cgra_stage_cycles.py trace_gemv_4x4_Mesh.jsonl trace_gemm_4x4_Mesh.jsonl
```

输出目录 `analysis_csv/`：

- `trace_*_stage_cycles.csv`：按 `ctrl_mem.times` 的细粒度分段
- `stage_cycle_summary.csv`：两种口径的 cycle summary（用于对比）

## 当前 summary 两种口径

`stage_cycle_summary.csv` 里每个 log 会有以下统计：

1. `effective_data_full_window`
   - 从第一条有效数据指令到最后一条有效数据指令（连续窗口）
2. `effective_data_active_cycles`
   - 只统计“有有效数据”的 cycle（窗口内去除空转/气泡）
3. `effective_data_truncated_window`
   - 按窗口截断口径统计（用于对齐 simulator）
   - 当前内置截断参数（按现有两份 trace 校准）：
     - gemv: `start_cycle >= 44` 且 `global_times <= 110`
     - gemm: `start_cycle >= 123` 且 `global_times <= 643`
4. `truncated_out_tail`
   - 截断口径下被排除的尾部周期

> 这样你可以同时比较：
> - 去空转口径（`effective_data_active_cycles`）
> - 窗口截断口径（`effective_data_truncated_window`）
