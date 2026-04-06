# Behavioral 与 RTL 周期对齐说明

## 背景

两套simulations：

- **Zeonica**：事件驱动，按照编译器给出的理论 II（Initiation Interval）模拟每个操作的触发时刻，时间单位为"behavioral time"。GEMV 理论 II = 11，GEMM 理论 II = 17。
- **VectorCGRA**：时序精确，包含真实的 FU 流水线深度与路由延迟，实际 II 更大。GEMV 实测 II = 14，GEMM 实测 II = 25。

两套仿真器的周期数不能直接比较，需要先对齐。`align_cycles.py` 实现了这一对齐过程，并输出可用于论文的对比表格。

---

## 对齐方法

### 1. 提取执行事件

**Behavioral 端**：从 `.json.log` 中筛选 `msg == "Inst"` 的记录，按 `(X, Y, ID)` 分组保存每个 tile 的触发时刻列表。支持 `MUL`、`STORE`、`ADD` 三类操作。

关于 predicate 过滤策略：
- `MUL`、`ADD`：只保留 predicate=True 的记录（`Result` 字段以 `(true)` 结尾），排除无效执行
- `STORE`、`LOAD`：**保留所有记录**，不过滤 predicate。这是因为内存操作 tile 每个 II 都会执行一次，predicate 只控制该次是否真正写入内存，但 tile 的触发节奏与 RTL 的每迭代触发完全对应

**RTL 端**：从 `.jsonl` trace 中遍历每个 cycle 的所有 tile，检测 valid handshake（`val=1 AND rdy=1`）。FU 操作检查输出端口，内存操作检查 `mem_access`。按 `(tile_id, row, col)` 分组保存每个 tile 的触发周期列表。

### 2. 截取执行窗口（Exec Window Trim）

以 MUL 操作作为定位锚点，裁掉两端的非稳态部分：

| 端 | 窗口定义 |
|----|---------|
| Behavioral | `[first_MUL_time, last_MUL_time + last_interval]`（保留最后一次 MUL 后的 trailing slack） |
| RTL | `[first_MUL_cycle, last_MUL_cycle]`（严格到最后一次 MUL） |

这一步去除了：
- 配置阶段（configuration phase）产生的初始事件
- drain/cooldown 阶段的额外事件

### 3. 联合 Tile 选择（Joint Tile Selection）

对每个操作类型，在窗口内遍历所有 `(behavioral_tile, RTL_tile)` 组合，选择事件数差值最小的配对：

```
score = (|behav_count - rtl_count|, -(behav_count + rtl_count))
```

这一步解决了同一操作符被多个 tile 使用的问题。例如 ADD 指令在 CGRA 中同时用于累加、地址计算、循环计数器等不同目的，若混合所有 tile 会导致事件数不匹配。通过 tile 级精确配对，可以筛选出真正对应的一对。

### 4. 线性拟合与稳态修正

对每个操作做 1:1 顺序匹配，拟合：

```
rtl_cycle = slope × behavioral_time + intercept
```

由于 pipeline 在最初几次迭代尚未稳定，实际上分两次拟合：
- **全量拟合（linear_fit）**：包含所有对齐事件
- **稳态拟合（steady_state_linear_fit）**：跳过 RTL 间隔异常偏大的前几次（判断标准：间隔 > 1.5 × 中位数间隔），只用稳定阶段估计斜率和截距

稳态拟合的参数用于生成输出表格中的 `rtl_corrected` 列：

```
rtl_corrected = (rtl_cycle - ss_intercept) / ss_slope
```

`rtl_corrected` 表示"若去除 RTL 的固定开销，该 RTL 周期对应的 behavioral time 是多少"，可与 `behavioral_time` 直接对比。

---

## 周期差距分析

RTL 总周期数远大于 behavioral 的根本原因可以分解为两个分量：

| 分量 | 含义 | GEMV | GEMM |
|------|------|------|------|
| **warmup_offset**（一次性） | 流水线建立阶段的固定开销，体现为截距 | ≈ 182 cycles | ≈ 376 cycles |
| **per_iter_overhead**（每迭代） | RTL II 超出理论 II 的部分，体现为斜率 | 14/11 ≈ 1.273×（即每 11 behavioral cycles 对应 14 RTL cycles） | 25/17 ≈ 1.471×（即每 17 behavioral cycles 对应 25 RTL cycles） |

去除这两项开销后，稳态阶段的 `rtl_corrected` 与 `behavioral_time` 偏差在 sub-cycle 量级（< 2 cycles），验证了 Zeonica 对稳态行为的正确性。

---

## 对齐结果

### GEMV（4×4，理论 II=11，实测 II=14）

| 操作 | Behavioral 事件数 | RTL 事件数 | 对齐数 | 稳态间隔比 | R²（稳态） | 状态 |
|------|:---:|:---:|:---:|:---:|:---:|------|
| MUL   | 16 | 16 | 16 | 1.2727（= 14/11） | 0.9999 | ✓ 正常对齐 |
| STORE | 16 | 15 | 15 | 1.2727（= 14/11） | 1.0000 | ✓ 正常对齐 |
| ADD   | 15 | 15 | 15 | 1.2727（= 14/11） | 1.0000 | ✓ 正常对齐 |

所有三个操作的稳态间隔比均为 **14/11 ≈ 1.2727**，与 RTL_II / behavioral_II 完全一致。

**Tile 选择**：
- ADD：`behav tile(X=1,Y=1,ID=41) ↔ RTL tile(id=5,row=1,col=1)`，各 15 个事件

**MUL 稳态线性拟合参数**（用于输出 `rtl_corrected`）：
- slope = 1.2659，intercept = 181.87，R² = 0.9999
- 含义：RTL cycle ≈ 1.266 × behavioral_time + 182

### GEMM（4×4，理论 II=17，实测 II=25）

| 操作 | Behavioral 事件数 | RTL 事件数 | 对齐数 | 稳态间隔比 | R²（稳态） | 状态 |
|------|:---:|:---:|:---:|:---:|:---:|------|
| MUL   | 64 | 65 | 64 | 1.4706（= 25/17） | 1.0000 | ✓ 正常对齐 |
| STORE | 64 | 64 | 64 | 1.4706（= 25/17） | 1.0000 | ✓ 正常对齐 |
| ADD   | 64 | 64 | 64 | 1.4706（= 25/17） | 1.0000 | ✓ 正常对齐 |

所有三个操作 R² = 1.0000，对齐精度达到 sub-cycle 级别。

**Tile 选择**：
- ADD：`behav tile(X=2,Y=0,ID=181) ↔ RTL tile(id=2,row=0,col=2)`，各 64 个事件（GEMM 的 ADD 指令被多个 tile 共用，联合 tile 选择筛选出了累加用的那一对）

**GEMM 稳态线性拟合参数**（三个操作几乎完全一致）：
- slope ≈ 1.4706，intercept ≈ 375–376，R² = 1.0000

---

## 输出文件

| 文件 | 内容 |
|------|------|
| `alignment_anchors_{kernel}.csv` | 每个对齐事件的详细表格，含 `behavioral_time`、`rtl_cycle`、`rtl_corrected`、`correction_error`、`interval_ratio` |
| `alignment_summary_{kernel}.json` | 汇总信息：事件数、线性拟合参数、稳态参数 |

### CSV 字段说明

| 字段 | 说明 |
|------|------|
| `behavioral_time` | Zeonica 中该事件的触发时刻（单位：behavioral cycle） |
| `rtl_cycle` | VectorCGRA RTL 仿真器中对应事件的触发周期 |
| `rtl_corrected` | 用 MUL 稳态拟合将 `rtl_cycle` 反推回 behavioral 时间轴，便于与 `behavioral_time` 直接对比 |
| `correction_error` | `rtl_corrected - behavioral_time`，理想情况下接近 0 |
| `ratio` | `rtl_cycle / behavioral_time`，反映累积膨胀比 |
| `interval_ratio` | 相邻两次事件间隔之比 `Δrtl / Δbehavioral`，稳态应等于 RTL_II / behavioral_II |

---

## 使用方式

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

可选参数：
- `--ops MUL,ADD`：只对齐指定操作
- `--no-trim`：关闭执行窗口裁剪，包含配置阶段与 warmup 事件
