# CGRA-log-analyzer

分析 VectorCGRA RTL 仿真导出的 `jsonl` log，并导出两类结果：

1. **细粒度结果**：按 `ctrl_mem.times` 拆分详细阶段（用于 debug）
2. **语义结果**：按配置/执行/空转气泡等阶段汇总总 cycle（用于和 simulator 对账）

## 用法

```bash
python analyze_cgra_stage_cycles.py trace_gemv_4x4_Mesh.jsonl trace_gemm_4x4_Mesh.jsonl
```

输出目录 `analysis_csv/`：

- `trace_*_stage_cycles.csv`：细粒度 stage
- `stage_cycle_summary.csv`：语义阶段总周期统计（建议优先看这个）

## `stage_cycle_summary.csv` 字段与阶段定义

- `configuration_or_setup`：首次 kernel 指令前
- `gemm_execution_with_data` / `gemv_execution_with_data`：
  - 检测到 kernel 指令（非 control-like op），且
  - 同 cycle 至少有一个 tile 的 FU/memory 数据 `val=1`
  - 这部分更接近“真正有数据在跑”的执行周期
- `gemm_execution_no_data` / `gemv_execution_no_data`：
  - 检测到 kernel 指令，但 FU/memory 数据 `val` 没有被拉高
  - 可视作气泡/等待/无效执行周期
- `*_span_window`：从首个到最后一个 kernel 指令的连续窗口（便于与旧统计方式对照）
- `finalize_or_other`：kernel 指令窗口之后的尾部周期（如果有）

> 注意：`with_data` / `no_data` 是按“离散 cycle 计数”统计，不要求连续。
