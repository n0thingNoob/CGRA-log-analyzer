# CGRA-log-analyzer

分析 VectorCGRA RTL 仿真导出的 `jsonl` log。

## 1) 生成阶段统计（推荐先跑）

```bash
python analyze_cgra_stage_cycles.py trace_gemv_4x4_Mesh.jsonl trace_gemm_4x4_Mesh.jsonl
```

运行后会在 `analysis_csv/` 生成：

- `trace_gemv_4x4_Mesh_stage_cycles.csv`
- `trace_gemm_4x4_Mesh_stage_cycles.csv`
- `stage_cycle_summary.csv`

### `stage_cycle_summary.csv` 字段含义

每个 log 保留 4 类 cycle 数（两个 log 中间有一空行）：

1. `full_window_cycles`
   - 从第一条有效数据到最后一条有效数据的完整窗口
2. `warmup_and_configuration_cycles`
   - 截断执行窗口开始之前的 warm-up/configuration 周期
3. `truncated_execution_cycles`
   - 截断得到的主执行窗口周期
4. `execution_until_last_store_cycles`
   - 从截断起点延伸到最后一次有效 store 的执行窗口（保留终点写回）
5. `tail_cycles`
   - 截断执行窗口之后的收尾周期

当前内置截断参数（按现有两份 trace 标定）：
- gemv: `start_cycle >= 44` 且 `global_times <= 110`
- gemm: `start_cycle >= 123` 且 `global_times <= 643`

---

## 2) 提取主执行窗口（extract_main_window.py）

`extract_main_window.py` 用于从单个 trace 中提取“主执行窗口”并导出窗口内事件。

### 2.1 GEMV 示例

```bash
python extract_main_window.py \
  --trace trace_gemv_4x4_Mesh.jsonl \
  --kernel gemv \
  --out-dir out_gemv
```

### 2.2 GEMM 示例

```bash
python extract_main_window.py \
  --trace trace_gemm_4x4_Mesh.jsonl \
  --kernel gemm \
  --out-dir out_gemm
```

### 2.3 自动识别 kernel

```bash
python extract_main_window.py \
  --trace trace_gemm_4x4_Mesh.jsonl \
  --kernel auto \
  --out-dir out_auto
```

### 2.4 手动指定 II（覆盖默认）

```bash
python extract_main_window.py \
  --trace trace_gemm_4x4_Mesh.jsonl \
  --kernel gemm \
  --compiled-ii 25 \
  --out-dir out_gemm_ii25
```

### 2.5 新增计算指令（不改代码）

```bash
python extract_main_window.py \
  --trace your_new_trace.jsonl \
  --kernel auto \
  --extra-arith-ops "(min),(max),(xor)" \
  --out-dir out_new
```

### 2.6 手动覆盖窗口边界

```bash
python extract_main_window.py \
  --trace trace_gemv_4x4_Mesh.jsonl \
  --kernel gemv \
  --manual-start 129 \
  --manual-end 406 \
  --out-dir out_manual
```

### 输出文件说明（以 `out_gemv/` 为例）

- `all_exec_events.csv`：全 trace 的 exec 事件
- `all_math_events.csv`：全 trace 的 math 事件
- `window_exec_events.csv`：窗口内 exec 事件
- `window_math_events.csv`：窗口内 math 事件
- `window_trace.jsonl`：窗口截取后的原始 trace
- `summary.json`：窗口检测结果汇总（自动窗口、最终窗口、事件统计等）

> 补充：`extract_main_window.py` 的 exec 提取不会剔除 `(NAH)`，用于保留原始执行行为。
> 默认 II：gemv=11, gemm=25（可用 `--compiled-ii` 覆盖）。
