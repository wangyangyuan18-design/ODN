# ODN Offset — New Cable Join & Corner Lane Change Rules

These rules are now part of the formal Offset policy and supersede any older local behavior that conflicts with them.

## 1. New Cable is side-sticky at entry

A newly joining Cable is treated as a new member of the existing Cable Group. This includes a true Return Cable.

When it first enters an already-used common Pole Edge route, its physical side is determined from where it originally enters:

1. Prefer the side visible in the original incoming Cable geometry.
2. For a true Return, use its original Return takeoff side when available; otherwise use the paired Forward lane side when that side is already non-zero.
3. If the geometry carries no side information, use the existing route side hint only as the final fallback.

After the entry side is established:

- the Cable remains on that physical side through the continuous common route;
- a free slot on the opposite side is not a reason to cross;
- lane compression is allowed only within the same physical side;
- a new Cable is placed outside the existing Cable Group on its entry side, never inserted into the middle of the Group.

`Return = New Cable` for Lane/Group purposes. Return topology, FAT shared landing and 0.30 m special takeoff remain separate rules.

## 2. Lane change happens at the Corner Pole

For an ordinary route transition where `prev_slot != next_slot`:

- the incoming Edge remains on `prev_slot` until the Corner Pole;
- the outgoing Edge starts on `next_slot` from that same Corner Pole;
- the lane-change geometry is created at the Corner Pole using the two lane anchors;
- the Cable must not begin changing lane upstream of the Corner Pole merely because the next Edge needs another lane;
- the Cable must not be moved to the new lane early and then return to the Corner Pole.

This rule does **not** introduce a 0.30 m control distance. 0.30 m remains limited to explicit special same-point output / Return takeoff contexts.

## 3. Interaction with existing rules

These two rules do not override:

- Main Lane priority;
- stable Main Owner;
- 0.50 m ordinary lane spacing;
- Pole Landing exclusivity;
- Route/Group continuity;
- Return exact reverse-chain topology;
- FAT → Owning Link final geometry.

When rules conflict, the engineering order is:

`Route topology → real landing constraint → established Group/entry side → lane allocation → Corner-Pole transition → final geometry`.

## 4. Required diagnostics

The production diagnostic summary must expose:

- `JOIN-SIDE` when a new Cable/Return entry side is established or corrected;
- `LANE-CHANGE` with `timing=CORNER_POLE` for every ordinary slot transition;
- no ordinary lane change may be logged as an upstream/early transition.

## 5. Test acceptance

A valid test must demonstrate:

- a new Cable entering from the left remains on the left side;
- a new Cable entering from the right remains on the right side;
- a Return is treated as a new Cable for Group placement but keeps its physical entry side;
- a slot transition from `+1 → +2`, `+1 → 0`, `-1 → -2`, etc. changes at the Corner Pole;
- no ordinary transition uses the 0.30 m takeoff control distance;
- no unnecessary `+ → - → +` or `0 → non-zero → 0` shuttle is introduced.
