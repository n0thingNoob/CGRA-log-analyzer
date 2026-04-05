# CGRA-log-analyzer

分析 VectorCGRA RTL 仿真导出的 `jsonl` log。

## 用法

```bash
python analyze_cgra_stage_cycles.py trace_gemv_4x4_Mesh.jsonl trace_gemm_4x4_Mesh.jsonl
```

输出目录 `analysis_csv/`：

- `trace_*_stage_cycles.csv`：按 `ctrl_mem.times` 的细粒度分段
- `stage_cycle_summary.csv`：简化后的阶段统计（两份 log 中间有一空行）

## `stage_cycle_summary.csv` 当前结构

每个 log 只保留 4 类 cycle 数：

1. `full_window_cycles`
   - 从第一条有效数据到最后一条有效数据的完整窗口
2. `warmup_and_configuration_cycles`
   - 截断执行窗口开始之前的 warm-up/configuration 周期
3. `truncated_execution_cycles`
   - 截断得到的主执行窗口周期
4. `tail_cycles`
   - 截断执行窗口之后的收尾周期

当前内置截断参数（按现有两份 trace 标定）：
- gemv: `start_cycle >= 44` 且 `global_times <= 110`
- gemm: `start_cycle >= 123` 且 `global_times <= 643`

另外提供 `extract_main_window.py` 作为独立窗口提取脚本。脚本支持 `--kernel gemv|gemm|auto`，并为不同 kernel 使用不同默认 II（gemv=11, gemm=25）。
