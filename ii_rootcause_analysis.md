# GEMV CGRA II Root-Cause Analysis
# RTL II=14 vs Simulator II=11：根因分析

---

## Overview / 概述

| | RTL (VectorCGRA) | Simulator (Zeonica) |
|---|---|---|
| Steady-state II | **14 cycles** | **11 cycles** |
| Steady-state range | cycles 209–417 (iterations 3–16) | times 2–275 (25 iterations) |
| Δ II | **+3 cycles** | — |

The 3 extra RTL cycles are entirely attributable to **inter-tile routing pipeline registers** — one register per mesh hop — that the behavioral simulator does not model.

RTL 多出的 3 个周期完全来源于片间路由流水线寄存器（mesh 每跳 1 个寄存器），行为级仿真器没有对此建模。

---

## Steady-State Tile Timeline (one representative iteration)
## 稳态单次迭代时序表（以第 4 次迭代为例）

**Iteration anchor tile: tile(0,0)**
**Example iteration: c=221 → c=235 (II=14)**

| RTL cycle | Offset in II | Op at tile(0,0) | Status | Notes |
|-----------|-------------|-----------------|--------|-------|
| 221 | 0  | `grant_once'` | **EXEC** | Control token fires; FU output val=1,rdy=1 |
| 222 | 1  | `grant_once'` | **STALL-A** | routing_xbar.send[5]→FU_IN[1]: val=1,rdy=0; data from tile(0,1) via EAST routing register arrived, FU transitioning |
| 223 | 2  | `ph*`         | EXEC | FU_IN[0]+FU_IN[1] both valid |
| 224 | 3  | `<<'`         | EXEC | |
| 225 | 4  | `+'`          | EXEC | (first add in iteration) |
| 226–230 | 5–9 | `(NAH)` | idle | 5 idle slots |
| 231 | 10 | `(NAH)`       | **STALL-B** | routing_xbar.send[4]→FU_IN[0]: val=1,rdy=0; data from tile(1,0) via NORTH routing register arrived, FU at NAH slot |
| 232 | 11 | `(NAH)`       | idle | cascade idle (data buffered, FU slot not yet +'  ) |
| 233 | 12 | `+'`          | EXEC | (second add) — delayed 1 cycle by STALL-B + cascade |
| 234 | 13 | `(NAH)`       | idle | |
| 235 | 0  | `grant_once'` | **EXEC** | Next iteration begins |

**Simulator (II=11) for same tile:**
Positions 0=`grant_once'`, 1=`ph*`, 2=`<<'`, 3=`+'`, 4-8=`NAH×5`, 9=`+'`, 10=`NAH`
→ No stall slots; schedule assumes 0-latency inter-tile routing.

**Simulator（II=11）同一 tile 排期：**
第 0 槽 `grant_once'`，第 1 槽 `ph*`，第 2 槽 `<<'`，第 3 槽 `+'`，4-8 槽 `NAH×5`，第 9 槽 `+'`，第 10 槽 `NAH`。
→ 无 stall 槽；排期假设 tile 间路由为零延迟。

---

## Observed Stall Points in RTL
## RTL 中观测到的 Stall 点

The same pattern repeats in **every** steady-state iteration (period confirmed: II=14, cycles 209–417).

以下模式在每一次稳态迭代中都**完全重复**（周期确认：II=14，范围 cycles 209–417）。

---

### Stall A — routing_xbar.send[5] → FU_IN[1], from EAST = tile(0,1)

**Occurs every iteration:** c=208, 222, 236, 250, 264, 278, 292, 306, 320, 334, 348, 362, 376, 390, 404

**发生时刻（每次迭代各一次）：** c=208, 222, 236, …（每 14 周期重复）

```
Signal observed at tile(0,0):
  routing_xbar.send[5]  val=1, rdy=0   ← data FROM tile(0,1) queued for FU_IN[1]
  fu.inputs[1]          val=1, rdy=0   ← FU not accepting yet (transitioning from grant_once')
  fu.outputs[*]         val=0          ← grant_once' has already fired; FU transitioning
  routing_xbar.config[5] = 4           ← FU_IN[1] sourced from EAST = tile(0,1)
```

**Upstream chain / 上游链路：**

```
tile(1,1) [grant_pred EXEC, c=218]
    │ sends NORTH output (fxbar.send[1])
    │ 1-cycle routing register on (1,1)→(0,1) link
    ▼
tile(0,1) [grant_pred EXEC, c=221]
    │ routing_xbar.send[3] EAST val=1, rdy=1  ← sends EAST to tile(0,0)
    │ 1-cycle routing register on (0,1)→(0,0) link
    ▼
tile(0,0) [routing_xbar RECV, c=222]
    │ data arrives at FU_IN[1] input of routing_xbar
    │ but FU just completed grant_once' and has rdy=0 during transition
    ▼
STALL for 1 cycle (c=222)
    ▼
tile(0,0) [ph* EXEC, c=223] — both FU_IN[0] and FU_IN[1] valid
```

**Effect / 影响：** +1 extra cycle. Every subsequent op in the iteration (ph*, <<', +') is shifted 1 cycle forward relative to the simulator schedule.

**影响：** 多出 1 个周期。本次迭代内后续所有操作（ph*、<<'、+'）相对仿真器排期整体推迟 1 个周期。

**Root cause / 根因：** The 1-cycle pipeline register on the EAST mesh link tile(0,1)→tile(0,0). The simulator models this as zero-latency.

**根因：** tile(0,1)→tile(0,0) 东向 mesh 链路上的 1 个周期流水线寄存器。仿真器将该路由视为零延迟。

---

### Stall B — routing_xbar.send[4] → FU_IN[0], from SOUTH = tile(1,0)

**Occurs every iteration:** c=217, 231, 245, 259, 273, 287, 301, 315, 329, 343, 357, 371, 385, 399, 413

**发生时刻（每次迭代各一次）：** c=217, 231, 245, …（每 14 周期重复）

```
Signal observed at tile(0,0):
  routing_xbar.send[4]  val=1, rdy=0   ← data FROM tile(1,0) queued for FU_IN[0]
  routing_xbar.recv[0]  val=1, rdy=1   ← data ARRIVED from SOUTH = tile(1,0) this cycle
  fu.inputs[0]          val=1, rdy=0   ← FU is at NAH slot, not accepting
  routing_xbar.config[4] = 1           ← FU_IN[0] sourced from SOUTH = tile(1,0)
  op = (NAH)                           ← FU scheduled NAH at this slot
```

**Upstream chain / 上游链路：**

```
tile(1,0) [grant_pred EXEC, c=230]
    │ fxbar.send[1] NORTH val=1, rdy=1  ← sends NORTH to tile(0,0)
    │ 1-cycle routing register on (1,0)→(0,0) link
    ▼
tile(0,0) routing_xbar.recv[0] receives data at c=231
    │ routing_xbar config[4]=1 → routes to FU_IN[0] (send[4] val=1)
    │ but FU is at a NAH instruction slot, rdy=0
    ▼
STALL for 1 cycle (c=231)    ← direct stall: data arrived early
    ▼
c=232: NAH idle               ← cascade: ctrl_mem advances to next NAH slot,
    │                            data held in routing_xbar buffer
    ▼
c=233: +' EXEC  (consumes FU_IN[0])  ← fires 1 cycle late due to stall + cascade
```

**Why does this happen? / 为什么会发生？**

Stall-A already shifted tile(0,0)'s execution schedule by +1 cycle. The upstream supplier (tile(1,0)) fires based on the **original** unshifted schedule. Its data therefore arrives 1 cycle before tile(0,0)'s now-shifted `+'` slot:

Stall-A 已将 tile(0,0) 的执行排期整体推迟 1 个周期。而上游供应者 tile(1,0) 依照**原始**排期工作，因此数据比 tile(0,0) 已推迟的 `+'` 槽早到 1 个周期：

```
Without Stall-A (ideal):   +' slot at T+9 relative to iteration start
                            tile(1,0) fires at T+8 → data arrives T+9 ✓  (no stall)

With Stall-A (+1 shift):   +' slot now at T+10
                            tile(1,0) still fires at T+8 → data arrives T+9
                            data arrives at T+9, +' slot at T+10 → 1 cycle early
                            → Stall-B at T+9 (1 stall) + cascade idle at T+10 (1 cycle)
                            → +' executes at T+11 instead of T+10
```

**Effect / 影响：** +2 extra cycles (1 direct stall + 1 cascade idle). Combined with Stall-A (+1), total overhead = **3 cycles per iteration**.

**影响：** 多出 2 个周期（1 个直接 stall + 1 个级联空闲）。与 Stall-A（+1）合计，每次迭代额外开销共 **3 个周期**。

**Root cause / 根因：** The 1-cycle pipeline register on the NORTH mesh link tile(1,0)→tile(0,0), combined with the 1-cycle schedule shift already induced by Stall-A.

**根因：** tile(1,0)→tile(0,0) 北向 mesh 链路上的 1 个周期流水线寄存器，叠加 Stall-A 已造成的 1 周期排期偏移。

---

## Accounting: How 3 Cycles Are Lost Per Iteration
## 逐周期开销分解：每次迭代多出的 3 个周期来自哪里

| Source | Cycles lost | Signal evidence |
|--------|:-----------:|-----------------|
| Stall-A: routing register on (0,1)→(0,0) EAST link | **+1** | `routing_xbar.send[5]` val=1,rdy=0 at grant_once'/ph* boundary |
| Stall-B direct: routing register on (1,0)→(0,0) NORTH link (data arrives early due to Stall-A shift) | **+1** | `routing_xbar.send[4]` val=1,rdy=0 at NAH slot |
| Stall-B cascade: schedule slip induced by Stall-B | **+1** | extra idle NAH slot before second `+'` fires |
| **Total** | **+3** | RTL II=14 − Simulator II=11 = 3 ✓ |

| 来源 | 损失周期数 | 信号证据 |
|------|:---------:|---------|
| Stall-A：(0,1)→(0,0) 东向路由寄存器 | **+1** | `routing_xbar.send[5]` val=1,rdy=0，位于 grant_once'/ph* 边界 |
| Stall-B 直接：(1,0)→(0,0) 北向路由寄存器（因 Stall-A 排期偏移导致数据提前到达） | **+1** | `routing_xbar.send[4]` val=1,rdy=0，位于 NAH 槽 |
| Stall-B 级联：Stall-B 引发的排期滑移 | **+1** | 第二个 `+'` 执行前多出一个空闲 NAH 槽 |
| **合计** | **+3** | RTL II=14 − Simulator II=11 = 3 ✓ |

---

## Critical Path Diagram
## 关键路径示意图

```
                 tile(1,1)
                    │
                    │  fxbar NORTH ↑ (c=218)
                    │  1-cycle mesh register
                    ▼
                 tile(0,1) ──── grant_pred EXEC (c=221)
                    │
                    │  routing_xbar EAST → (c=221)
                    │  1-cycle mesh register            ← Stall-A source
                    ▼
                 tile(0,0) ──── FU_IN[1] arrives (c=222)
                    │             grant_once' stalls 1 cycle
                    │             ph* finally executes (c=223)
                    │
                 tile(1,0) ──── grant_pred fxbar NORTH ↑ (c=230)
                    │
                    │  fxbar NORTH → tile(0,0) recv[0] (c=230)
                    │  1-cycle mesh register            ← Stall-B source
                    ▼
                 tile(0,0) ──── FU_IN[0] arrives (c=231)
                                NAH slot stalls 1 cycle
                                cascade idle (c=232)
                                +' finally executes (c=233)  [+2 total]
```

---

## Root Cause Classification
## 根因分类

**Category: ROUTING PIPELINE REGISTER LATENCY**
**类别：路由流水线寄存器延迟**

- **Mechanism / 机制：** Each tile-to-tile mesh link has a 1-cycle pipeline register in the routing crossbar's receive path. Data sent in cycle T arrives at the destination tile's routing_xbar in cycle T+1.

  每条 tile 间 mesh 链路在路由交叉开关的接收路径上有 1 个流水线寄存器。在第 T 周期发送的数据，在第 T+1 周期到达目的 tile 的 routing_xbar。

- **Simulator modeling gap / 仿真器建模缺口：** Zeonica (behavioral) models all inter-tile data transfers as zero-latency. It schedules instructions assuming data arrives in the same cycle it is produced by an upstream tile.

  Zeonica（行为级仿真器）将所有 tile 间数据传输建模为零延迟。它在排期时假设数据与上游 tile 产生的同一周期即可到达下游。

- **Affected links / 受影响链路：**
  - `tile(0,1) → tile(0,0)` via EAST routing_xbar → 1-cycle latency → causes Stall-A
  - `tile(1,0) → tile(0,0)` via NORTH fxbar+routing_xbar → 1-cycle latency → causes Stall-B (interacts with Stall-A)

- **Not a cause / 非根因：**
  - FU operator latency (all FU ops are single-cycle in this design)
  - Control predicate mismatch (predicates are functionally correct, timing only)
  - Memory access latency (not on the critical path for II)

---

## Implications and Recommendations
## 影响与建议

### For the compiler / 对编译器

The CGRA compiler's modulo scheduling must account for mesh routing latency. A 1-hop routing path should add **1 cycle** to the data dependence edge weight in the MRRG (Modulo Routing Resource Graph). For a 2-hop path (e.g., tile(1,1)→tile(1,0)→tile(0,0)), the edge weight should be +2.

CGRA 编译器的模调度必须将 mesh 路由延迟计入依赖边权重。MRRG（模路由资源图）中 1 跳路由路径应在数据依赖边权重上增加 **1 个周期**，2 跳路径增加 **2 个周期**，以此类推。

**Current situation / 现状：** The compiler produced II=11 assuming 0-hop latency. The RTL shows II=14 because the actual 1-hop latency adds 3 cycles via the two stall chains described above.

**现状：** 编译器以 0 跳延迟假设生成了 II=11。RTL 实际运行为 II=14，因为真实的 1 跳延迟通过上述两条 stall 链合计多出 3 个周期。

**Expected optimal II with correct routing model / 加入正确路由模型后的预期最优 II：**
If the compiler accounts for 1-cycle routing latency per hop, the re-scheduled II should be at most 14 (matching RTL). Routing-aware scheduling may find a more optimal schedule (possibly II=12 or II=13) by reordering instructions to hide routing latency.

若编译器将每跳 1 周期的路由延迟纳入考量，重新排期后的 II 上限为 14（与 RTL 吻合）。通过重新排列指令顺序以隐藏路由延迟，路由感知排期可能找到更优解（如 II=12 或 II=13）。

### For the simulator / 对仿真器

To close the II gap, Zeonica should model inter-tile routing as:
- **+1 cycle latency** per mesh hop for data tokens
- **+0 cycle** for control tokens **only if** the control token is on the same tile; otherwise also +1 per hop

为缩小 II 差异，Zeonica 应将 tile 间路由建模为：
- 数据令牌每跳 mesh 路由 **+1 个周期延迟**
- 控制令牌仅在同一 tile 内为 **+0 周期**；跨 tile 则同样每跳 +1 周期

### For the RTL / 对 RTL

If routing pipeline registers can be **removed** (at the cost of longer critical path / lower Fmax), the II may reduce toward 11. The tradeoff is timing closure. Alternatively, the routing registers are necessary for high-frequency operation and the compiler should be fixed instead.

若可以**移除**路由流水线寄存器（代价是关键路径变长、最大时钟频率降低），II 可能降至接近 11。折中方案是修正编译器以充分利用这些寄存器，而非将其视为纯开销。

---

## Verification Evidence
## 验证证据

All observations are from `trace_gemv_4x4_Mesh.jsonl` (444 cycles, 16 tiles per cycle).

所有观测均来自 `trace_gemv_4x4_Mesh.jsonl`（444 cycles，每周期 16 个 tile）。

```python
# Verified stall cycle lists (steady-state, complete):
stall_A_cycles = [208, 222, 236, 250, 264, 278, 292, 306, 320,
                  334, 348, 362, 376, 390, 404]  # 15 iterations × 1 = 15 stalls

stall_B_cycles = [217, 231, 245, 259, 273, 287, 301, 315, 329,
                  343, 357, 371, 385, 399, 413]  # 15 iterations × 1 = 15 stalls

grant_once_exec = [221, 235, 249, 263, 277, 291, 305, 319, 333,
                   347, 361, 375, 389, 403, 417]  # II = 14 cycles confirmed

# RTL II per iteration (all 14):
# [14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14]

# Upstream confirmed:
# Stall-A: tile(0,1) routing_xbar.send[3] (EAST) val=1,rdy=1 at c=221
#          → 1-cycle register → arrives tile(0,0) recv at c=222
# Stall-B: tile(1,0) fxbar.send[1] (NORTH) val=1,rdy=1 at c=230
#          → 1-cycle register → arrives tile(0,0) recv[0] val=1,rdy=1 at c=231
```

---

## Summary in One Sentence / 一句话总结

**EN:** Every RTL iteration of GEMV wastes 3 cycles (II=14 vs ideal 11) because the CGRA mesh has 1-cycle pipeline registers per hop that the behavioral scheduler ignores: one hop on `tile(0,1)→tile(0,0)` causes a 1-cycle stall, which cascades into a 2-cycle stall on the `tile(1,0)→tile(0,0)` hop due to schedule misalignment.

**ZH：** GEMV 的每次 RTL 迭代浪费 3 个周期（II=14 vs 理论最优 11），原因是 CGRA mesh 每跳有 1 个流水线寄存器而行为级排期器对此视而不见：`tile(0,1)→tile(0,0)` 这一跳造成 1 周期 stall，由于排期错位进而在 `tile(1,0)→tile(0,0)` 这一跳级联出 2 周期的额外开销。
