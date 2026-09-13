# ODN Offset Rule Audit — 2026-09-13

## 目的

本记录把正式设计规则、已发生问题和当前运行时结构交叉检查，确认哪些规则曾经没有真正进入唯一执行链，以及哪些补丁之间存在冲突。

正式规则来源：
- `ODN_DESIGN_RULES.md`
- `ODN_DESIGN_SPEC.md`
- `ODN_OFFSET_CORE_RULES.md`
- `ODN_DESIGN_AND_DEVELOPMENT_RULES.md`
- `ODN_OFFSET_PENDING_ISSUES.md`
- `ODN_OFFSET_JOIN_AND_CORNER_RULES.md`

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
- Core 负责 Route / Corner / Geometry，Policy 负责全局 Lane state、Return、Pole Landing 最终校验；
- `cable_offset_join_corner_rules.py` 只作为该统一 Policy seam 的最后规则扩展，不建立第二套 allocator。

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

统一 Policy 保存：
- `side_memory`：segment 级稳定侧；
- `order_memory`：同侧 Cable 的历史相对顺序；
- 新成员按 Group Outside 规则排在已有成员外侧。

### 4. 新 Cable / Return 的加入侧必须由实际进入侧决定【新增正式规则】

此前只有“Group Outside / sticky side”原则，没有明确规定“新 Cable 第一次进入共同路由时，究竟以哪一侧作为物理加入侧”。这会让第一次加入在 `side_hint=0` 时退回默认侧。

现在明确：
- 新 Cable 是 Cable Group 的新成员；
- **Return 也按新 Cable 处理；**
- 首次进入已有共同 Pole Edge 时，优先读取原始 Cable geometry 的实际进入侧；
- 真正 Return 优先读取原始 Return takeoff 侧，其次读取 paired Forward 的非 0 side；
- 之后保持该 side，不允许因为另一个方向当前有空 slot 就换边；
- 新 Cable 只能从该进入侧的 Group 外侧加入，不能插入已有 Cable 中间。

实现：`cable_offset_join_corner_rules.py` 的 `entry-side` 规则。

### 5. Lane 压缩只能发生在同一物理侧

正式规则要求：`0,2,3 → 0,1,2`，但不能因为压缩而把 `+1` 变成 `-1`。

统一 Policy 只在同一 side 内将 Lane magnitude 压缩为紧凑序列；既有 DC 保留的 reserved slot 不会被覆盖。

### 6. Pole Route Node 与 Cable Landing 必须分离

此前 Core 在 Lane 规划前直接根据 segment endpoint 引用执行 Pole exclusivity `raise`，把“Route endpoint”过早当成“最终 Cable landing”。

这与正式规则冲突：
- Main Lane 应先优先使用；
- 只有真正发生 Pole Landing 冲突时，受影响 Cable 才离开 Main；
- 内部 Pole 只是 Route Node，不自动等于 Landing。

现在改为：
1. Lane 规划前不再因 endpoint 共点直接失败；
2. 根据最终 slot 判断实际 endpoint landing；
3. 最终再执行 Pole landing exclusivity。

### 7. Lane Change 必须在拐角 Pole 才发生【新增正式规则】

此前的 D/E/F Corner Core 使用 offset-line intersection。对于 `prev_slot != next_slot`，两条 offset line 的数学交点有可能位于实际 Corner Pole 上游或下游，因此存在“Cable 在到达拐角杆之前已经开始变 Lane”的可能。

现在明确：

- 普通 Lane Change 不提前；
- Incoming Edge 一直到 Corner Pole 都保持 `prev_slot`；
- 到达 Corner Pole 后，才从 `prev_slot` 切到 `next_slot`；
- Outgoing Edge 从 Corner Pole 开始使用 `next_slot`；
- 不允许“提前切到新 Lane → 到杆前回折 → 再转角”；
- 这一规则不引入 0.30m。

实现：`cable_offset_join_corner_rules.py` 的 `_patched_corner()` / `_corner_at_pole()`，以 Corner Pole 的两侧 Lane Anchor 作为唯一 Lane-change geometry。

### 8. 0.30 m 与普通 Corner 必须彻底隔离

普通 Corner 继续使用已有统一 `_unified_lane_corner()`；不增加 run-in/run-out 控制距离。

0.30 m 仍只由特殊同点出线 / Return 等显式 endpoint context 触发。

本次 Corner-Pole Lane Change 规则明确不调用 0.30m takeoff。

### 9. Return 必须由拓扑确认

`FATRETURN` 只是业务标记，不再独立决定 Return。

最终 Return 条件仍为：
- 同一 Link；
- Pole Edge Chain 完全相同；
- 顺序严格反向；
- 从 FAT Pole 出发；
- 到另一端最末 Pole 结束。

### 10. Return FAT 起点与普通 Pole 不能混为一谈

Return 的 FAT 起点是明确允许的同点特殊例外；Return 的远端 Pole 仍是普通独占 Pole，不能因为“Return”标签而自动变成共享节点。

### 11. FAT / Cable ownership 必须保持解耦

Lane Policy 不使用附近 FAT 位置来决定别的 Link 的 Lane；FAT landing 继续由 owning Link 的最终 Cable geometry 决定。

### 12. 诊断日志要回答“为什么换 Lane”

统一 Policy 的最小核心摘要：

`[LANE-SUMMARY] edges=...; main_switches=...; side_crosses=...; gaps_compacted=...; blocked_main=...; preserved=...`

新增：

`[JOIN-SIDE] ... rule=ENTER_SIDE_STICKY`

`[LANE-CHANGE] ... timing=CORNER_POLE; early-change=FORBIDDEN`

其中：
- `main_switches` 应接近 0；
- `side_crosses` 正常情况下应为 0；
- `blocked_main` 只有真实 Landing / reserved Main 情况才应出现；
- `gaps_compacted` 表示真实发生同侧压缩；
- `JOIN-SIDE` 只能在真正的新 Cable / Return 加入共同 Group 时出现；
- 普通 slot transition 必须出现 `timing=CORNER_POLE`，不允许记录为 upstream/early change。

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
Policy extension
      ├─ New Cable / Return entry-side sticky
      └─ Lane Change at Corner Pole
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
5. **新 Cable / Return 在第一次进入共同 Group 时保持原进入侧；**
6. **Lane Change 只发生在 Corner Pole，不在上游提前发生；**
7. Lane 只在真实冲突/分叉/目的方向变化时改变；
8. 普通 Corner 无人工 dog-leg；
9. Return 在 FAT 起点物理落杆，并与 Forward 保持 0.50m；
10. 普通 Pole 最终只出现一个独立 Cable Landing；
11. FAT 只跟随 Owning Link 最终 geometry；
12. 日志能够直接说明 Main 被阻塞、Group 新成员加入侧或正常压缩。
