# ODN Offset Rule Audit — 2026-09-13

## 目的

本记录把正式设计规则、已发生问题和当前运行时结构交叉检查，确认 Lane 分配必须同时满足：连续性、相对位置、Group Outside、真实进入侧、Corner Pole 换道、Pole Landing 独占，以及尽量减少线缆交叉。

正式规则来源：
- `ODN_DESIGN_RULES.md`
- `ODN_DESIGN_SPEC.md`
- `ODN_OFFSET_CORE_RULES.md`
- `ODN_DESIGN_AND_DEVELOPMENT_RULES.md`
- `ODN_OFFSET_PENDING_ISSUES.md`
- `ODN_OFFSET_JOIN_AND_CORNER_RULES.md`

## 全局 Lane 原则

1. **Main Lane 优先但不能来回抢位**：物理 Main Lane 未被 reserved/真实 Landing 冲突占用时，应有且仅有一个 Cable 使用 slot 0。
2. **Main Owner 连续**：同一共同 Pole Edge / 连续 Route 上，优先保持原 Main Owner；Cable 暂时离开不等于永久失去 Main。
3. **Route continuity 优先于局部空槽**：不能因为当前 Edge 的某个 slot 空闲，就让 Cable 从 `0→+1→0` 或 `+1→-1→+1` 往返。
4. **物理侧稳定**：Cable 一旦确定 `+` / `-` side，后续优先保持同侧；无真实工程原因不得跨侧。
5. **同侧相对顺序稳定**：同一 Pole Edge 上 A 在 B 内侧，则下一 Edge 不得无原因交换成 B 在 A 内侧。
6. **Group Outside**：新加入 Cable 不插入既有 Cable 中间，而从自己的进入侧外侧加入。
7. **新 Cable / Return 同规则**：Return 是新加入 Cable；第一次进入共同 Group 时按实际物理进入侧加入。
8. **进入侧来源**：优先原始 Cable geometry 的实际进入侧；Return 优先原始 Return takeoff / paired Forward 已知非 Main side；最后才使用 side hint。
9. **同侧紧凑**：只在同一物理侧压缩空槽，例如 `+1,+3→+1,+2`；不得通过跨侧实现所谓“紧凑”。
10. **直线连续 Edge 不换 Lane**：同一 Route 在两个 Pole 之间只是直线延续时，Lane 必须保持不变；真实冲突应进入冲突处理，而不是偷偷提前换道。
11. **Corner Pole 才能换 Lane**：只有真实转角 Pole 才允许 `prev_slot→next_slot`；Incoming Edge 到 Pole 前保持旧 Lane，Outgoing Edge 从 Pole 开始使用新 Lane。
12. **不允许提前换道**：禁止上游先偏移到新 Lane、到 Pole 再回折，或在 Corner 前后产生无工程意义的 dog-leg。
13. **0.30m 隔离**：普通 Lane Change 不使用 0.30m；0.30m 只属于显式 special takeoff / Return 等特殊上下文。
14. **Pole Landing 与 Route Node 分离**：内部 Route Node 不等于 Cable Landing；先 Lane，再根据最终 geometry 判断真实落杆冲突。
15. **最小交叉目标**：Lane 分配不是单纯“slot 越小越好”，而是以 `Main continuity + side stability + relative order + new outside + corner-only change` 为硬约束，再最小化潜在交叉。

## 已发生问题与当前处理

### Main Lane 空着
旧 Core 只看 immediate previous Edge，旧 Main Guard 又按当前最接近 0 的 Cable 抢 Main，造成 Main Owner 漂移。

当前由 `cable_offset_policy` + `cable_offset_lane_optimizer_final.py` 统一处理：连续性优先、历史 Owner 优先、reserved Main 不覆盖。

### 线缆来回穿梭
旧逻辑在每条 Edge 独立重新按空槽排序，导致 `+1→+2→+1`、`+1→-1→+1` 等视觉穿梭。

当前保存 segment 级 side/order memory，并将上一 Edge Lane 作为强连续性信号。

### 新 Cable 插入 Group 中间
旧逻辑只追求紧凑，可能把新 Cable 塞入既有 Cable 之间。

当前新 Cable / Return 在真实 Group entry 侧采用 Group Outside，已有成员优先，新成员追加到外侧。

### Lane 提前变更
旧 Corner 使用 offset-line mathematical intersection，交点可能位于 Corner Pole 上游/下游。

当前 `cable_offset_join_corner_rules.py` 只负责 Corner geometry：Lane transition 必须以 Corner Pole 两侧 Anchor 为控制点，不再承担 Lane allocator。

### 直线 Pole Edge 被错误重新分配
如果一条 Route 只是经过多个共线 Pole，不能把每个 Pole 当作重新排序机会。当前最终 allocator 对共线 transition 保持上一 Edge slot。

### Return 规则冲突
Return 必须由拓扑确认，并按新 Cable 处理其进入侧；FAT 起点是特殊允许的同点 landing，远端 Pole 仍保持普通独占。

## 当前运行时结构

```text
Route / direction
      ↓
cable_offset_core
      ↓
cable_offset_policy
      ├─ Return topology
      ├─ Pole Landing final validation
      └─ unified policy seam
      ↓
cable_offset_join_corner_rules
      └─ Corner-Pole timing / geometry only
      ↓
cable_offset_lane_optimizer_final
      ├─ Main Owner continuity
      ├─ physical side stability
      ├─ same-side relative order
      ├─ Group Outside
      ├─ same-side compacting
      ├─ straight-edge no-change
      └─ minimum-crossing priority
      ↓
FAT → Owning Link final geometry
```

`cable_offset_main_lane_guard.py` 不再加载；旧重复 allocator 不再作为运行时入口。不得重新加入另一套 Main Lane allocator 或 v2/v3/v4/v5 Offset Engine。

## 验收指标

正常小测试网络应满足：

```text
main_switches       ≈ 0
side_crosses        = 0
straight changes    = 0（除真实冲突）
relative order swap = 0
new-cable insertion = OUTSIDE
lane change timing  = CORNER_POLE
```

日志重点：

```text
[LANE-OPT] strategy=MIN_CROSSING
[LANE-CHANGE] ... timing=CORNER_POLE; early-change=FORBIDDEN
[RETURN] ... rule=EXACT_REVERSE_CHAIN_FAT_START
```

任何 Main switch / side crossing 都应能追溯到真实 Pole Landing、reserved Lane、Route topology 或明确的工程冲突，而不能仅仅因为“当前 slot 空了”。
