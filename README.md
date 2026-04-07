# CGRA-log-analyzer

分析 VectorCGRA RTL 仿真与 Zeonica behavioral 仿真导出的 log，进行周期对齐与执行窗口分析。

---

## 1) Behavioral 与 RTL 周期对齐（align_cycles.py）

将 Zeonica behavioral 仿真（`.json.log`）与 VectorCGRA RTL 仿真（`.jsonl`）的周期数对齐，输出可直接用于论文的对比表格。详细方法说明见 [align_cycles_notes.md](align_cycles_notes.md)。

### 用法

```bash
# GEMV
python align_cycles.py \
    --behavioral gemv.json.log \
    --trace trace_gemv_4x4_Mesh.jsonl \
    --kernel gemv \
    --output-dir analysis_csv

# GEMM
python align_cycles.py \
    --behavioral gemm.json.log \
    --trace trace_gemm_4x4_Mesh.jsonl \
    --kernel gemm \
    --output-dir analysis_csv
```

### 主要参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--behavioral` | （必填） | behavioral `.json.log` 路径 |
| `--trace` | （必填） | RTL `.jsonl` trace 路径 |
| `--kernel` | `auto` | `gemv` / `gemm` / `auto`（从文件名推断） |
| `--output-dir` | `analysis_csv` | 输出目录 |
| `--ops` | `MUL,STORE,ADD` | 对齐的操作类型（逗号分隔） |
| `--no-trim` | 关闭 | 禁用执行窗口裁剪，保留配置阶段事件 |

### 输出文件

| 文件 | 内容 |
|------|------|
| `alignment_anchors_{kernel}.csv` | 每个对齐事件的详细表格 |
| `alignment_summary_{kernel}.json` | 汇总：事件数、线性拟合参数、稳态参数 |

#### `alignment_anchors_{kernel}.csv` 字段说明

| 字段 | 说明 |
|------|------|
| `behavioral_time` | Zeonica 触发时刻（behavioral cycle） |
| `rtl_cycle` | VectorCGRA RTL 触发周期 |
| `rtl_corrected` | 去除 RTL 固定开销后反推的 behavioral time，可与 `behavioral_time` 直接对比 |
| `correction_error` | `rtl_corrected − behavioral_time`，稳态下 < 2 cycles |
| `ratio` | `rtl_cycle / behavioral_time`，累积膨胀比 |
| `interval_ratio` | `Δrtl / Δbehavioral`，稳态应等于 `RTL_II / behavioral_II` |

### 对齐结果

| Kernel | Op | 稳态间隔比 | R² |
|--------|-----|:---:|:---:|
| GEMV（II: 11→14） | MUL | 14/11 = 1.273 | 0.9999 |
| | STORE | 14/11 = 1.273 | 1.0000 |
| | ADD | 14/11 = 1.273 | 1.0000 |
| GEMM（II: 17→25） | MUL | 25/17 = 1.471 | 1.0000 |
| | STORE | 25/17 = 1.471 | 1.0000 |
| | ADD | 25/17 = 1.471 | 1.0000 |

---

## 2) 提取主执行窗口（extract_main_window.py）

从单个 RTL trace 中提取主执行窗口并导出窗口内事件。

```bash
python extract_main_window.py \
  --trace trace_gemv_4x4_Mesh.jsonl \
  --kernel gemv \
  --out-dir out_gemv
```

常用参数：`--compiled-ii`（覆盖默认 II）、`--manual-start` / `--manual-end`（手动指定窗口）、`--extra-arith-ops "(min),(max)"`（新增计算指令）。

输出：`window_exec_events.csv`、`window_math_events.csv`、`window_trace.jsonl`、`summary.json`。

---

## 3) 阶段统计（analyze_cgra_stage_cycles.py）

统计 RTL trace 中各阶段（warmup / execution / tail）的周期数。

```bash
python analyze_cgra_stage_cycles.py trace_gemv_4x4_Mesh.jsonl trace_gemm_4x4_Mesh.jsonl
```

输出至 `analysis_csv/`：`stage_cycle_summary.csv`、各 trace 的 `_stage_cycles.csv`。
