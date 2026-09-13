# ODN Offset Rule Audit — 2026-09-13

## 目的

本记录把正式设计规则、已发生问题和当前运行时结构交叉检查，确认哪些规则曾经没有真正进入唯一执行链，以及哪些补丁之间存在冲突。

正式规则来源：
- `ODN_DESIGN_RULES.md`
- `ODN_DESIGN_SPEC.md`
- `ODN_OFFSET_CORE_RULES.md`
- `ODN_DESIGN_AND_DEVELOPMENT_RULES.md`
- `ODN_OFFSET_PENDING_ISSUES.md`

## 已发现的遗漏 / 冲突

### 1. Main Lane 规则曾经是三套逻辑

过去同时存在：
- `cable_offset_core._plan()` 的局部上一 Edge 判断；
- `cable_offset_main_lane_guard.py` 的“当前最接近 0 的 Cable 抢 Main”；
- `cable_offset_policy.py` 的 Return / Pole Landing 二次调整。

这与“Offset Core 唯一执行入口、完整 Route 决定 Lane”的长期规则冲突。

现在：
- `__init__.py` 不再加载 `cable_offset_main_lane_guard.py`；
- `cable_offset_policy.py` 成为最终统一 Policy seam；
- Core 负责 Route / Corner / Geometry，Policy 负责全局 Lane state、Return、Pole Landing 最终校验。

### 2. Main Owner 不能按当前 Edge 重新选

正式规则要求连续共同 Route 保持稳定主位，不允许因为局部加入/删除 Cable 就发生 0→非0→0 往返。

统一 Policy 现在保存：
- `main_owner`：segment 内稳定 Main Owner；
- Cable 离开某个物理 Edge 时，原 Owner 不会因为另一 Cable 出现而永久丢失；
- Owner 因真实 Pole Landing 冲突暂时不能使用 slot 0 时，不改变历史 Owner；冲突消失后可恢复 Main。

### 3. 相对侧和相对顺序必须有状态

正式规则要求：
- 同一 Cable 左/右侧不要随局部 Edge 改变；
- Group 内原有 Cable 相对顺序保持；
- 新 Cable 从 Group 外侧加入；
- 不能出现无工程原因的 `+1→-1→+1`。

统一 Policy 现在保存：
- `side_memory`：segment 级稳定侧；
- `order_memory`：同侧 Cable 的历史相对顺序；
- 新成员按 Group Outside 规则排在已有成员外侧。

### 4. Lane 压缩只能发生在同一物理侧

正式规则要求：`0,2,3 → 0,1,2`，但不能因为压缩而把 `+1` 变成 `-1`。

统一 Policy 现在只在同一 side 内将 Lane magnitude 压缩为紧凑序列；既有 DC 保留的 reserved slot 不会被覆盖。

### 5. Pole Route Node 与 Cable Landing 必须分离

此前 Core 在 Lane 规划前直接根据 segment endpoint 引用执行 Pole exclusivity `raise`，把“Route endpoint”过早当成“最终 Cable landing”。

这与正式规则冲突：
- Main Lane 应先优先使用；
- 只有真正发生 Pole Landing 冲突时，受影响 Cable 才离开 Main；
- 内部 Pole 只是 Route Node，不自动等于 Landing。

现在改为：
1. Lane 规划前不再因 endpoint 共点直接失败；
2. 根据最终 slot 判断实际 endpoint landing；
3. 最终再执行 Pole landing exclusivity。

### 6. 0.30 m 与普通 Corner 必须彻底隔离

普通 Corner 继续使用已有统一 `_unified_lane_corner()`；不增加 run-in/run-out 控制距离。

0.30 m 仍只由特殊同点出线 / Return 等显式 endpoint context 触发。

### 7. Return 必须由拓扑确认

`FATRETURN` 只是业务标记，不再独立决定 Return。

最终 Return 条件仍为：
- 同一 Link；
- Pole Edge Chain 完全相同；
- 顺序严格反向；
- 从 FAT Pole 出发；
- 到另一端最末 Pole 结束。

### 8. Return FAT 起点与普通 Pole 不能混为一谈

Return 的 FAT 起点是明确允许的同点特殊例外；Return 的远端 Pole 仍是普通独占 Pole，不能因为“Return”标签而自动变成共享节点。

### 9. FAT / Cable ownership 必须保持解耦

Lane Policy 不使用附近 FAT 位置来决定别的 Link 的 Lane；FAT landing 继续由 owning Link 的最终 Cable geometry 决定。

### 10. 诊断日志要回答“为什么换 Lane”

统一 Policy 的最小核心摘要：

`[LANE-SUMMARY] edges=...; main_switches=...; side_crosses=...; gaps_compacted=...; blocked_main=...; preserved=...`

其中：
- `main_switches` 应接近 0；
- `side_crosses` 正常情况下应为 0；
- `blocked_main` 只有真实 Landing / reserved Main 情况才应出现；
- `gaps_compacted` 表示真实发生同侧压缩。

## 当前唯一规则执行结构

```text
Route / direction
      ↓
cable_offset_core
      ↓
Corner geometry / final cable geometry
      ↓
unified cable_offset_policy
      ├─ Main Owner state
      ├─ Side memory
      ├─ Group order / Outside
      ├─ Same-side compacting
      ├─ Return topology
      └─ Final Pole Landing validation
      ↓
FAT → Owning Link final geometry
```

不得重新加入另一套 Main Lane allocator 或新的 v2/v3/v4/v5 offset engine。

## 下一轮测试判据

重点观察同一测试网络是否满足：

1. 主线空闲时不会长期空着；
2. Main Owner 不因局部 Cable 加入/删除而来回切换；
3. Cable 不发生无工程原因的左右穿梭；
4. 同一 Pole Edge 上同组 Cable 的相对顺序不交换；
5. Lane 只在真实冲突/分叉/目的方向变化时改变；
6. 普通 Corner 无人工 dog-leg；
7. Return 在 FAT 起点物理落杆，并与 Forward 保持 0.50m；
8. 普通 Pole 最终只出现一个独立 Cable Landing；
9. FAT 只跟随 Owning Link 最终 geometry；
10. 日志能够直接说明 Main 被阻塞还是发生了正常压缩。
