# -*- coding: utf-8 -*-
"""Final Offset policy extensions for new-cable joining and lane-change timing.

Rules enforced here:
1. A new Cable, including a true Return Cable, joins a common Cable Group on
   the physical side from which it originally enters. The entry side is sticky
   for the whole continuous segment unless a real engineering conflict forces
   a side change.
2. A normal Lane change happens at the route Corner Pole, not before it.
   Incoming geometry remains on the previous Lane up to the Corner Pole and
   outgoing geometry starts on the new Lane from that same Pole.

This module is a narrow extension of the single unified cable_offset_policy
seam. It does not create another Lane allocator or another geometry engine.
"""

from math import hypot

from qgis.core import QgsPointXY

from . import cable_offset_core as _core
from . import cable_offset_layout as _base
from . import cable_offset_policy as _policy
from . import cable_offset_direction_fix as _direction_fix

_INSTALLED = False
_ORIGINAL_PLAN = _core._plan
_ORIGINAL_CORNER = _core._build_corner_geometry


def _sign(value):
    return 1 if int(value) > 0 else -1


def _node_match(a, b, eps=1e-7):
    return hypot(float(a.x()) - float(b.x()), float(a.y()) - float(b.y())) <= eps


def _all_edges(segment):
    return [
        e for raw in segment.get("edge_sequence", []) or []
        if (e := _base._canonical_edge(raw))
    ]


def _entry_side_from_geometry(segment, edge, edge_crs, work):
    """Read the physical side of the original incoming cable geometry."""
    points = segment.get("points", []) or []
    if len(points) < 2:
        return 0
    try:
        a, b = _base._edge_points(edge)
        a = _core._tp(a, edge_crs, work)
        b = _core._tp(b, edge_crs, work)
        dx = b.x() - a.x()
        dy = b.y() - a.y()
        length = hypot(dx, dy)
        if length <= 1e-9:
            return 0
        for raw in points[1:]:
            if len(raw) < 2:
                continue
            probe = _core._tp(QgsPointXY(float(raw[0]), float(raw[1])), edge_crs, work)
            cross = dx * (probe.y() - a.y()) - dy * (probe.x() - a.x())
            if abs(cross) > max(1e-7, length * 1e-7):
                return 1 if cross > 0 else -1
    except Exception:
        return 0
    return 0


def _paired_return_side(designs, design_index, segment_index, edge_index, slots):
    """Return the Forward cable's physical side for a true Return."""
    design = designs[design_index]
    segment = design.get("segments", [])[segment_index]
    if not segment.get("_odn_return_chain"):
        return 0
    pair_index = segment.get("_odn_return_pair_segment")
    if pair_index is None:
        return 0
    return_edges = _all_edges(segment)
    if edge_index >= len(return_edges):
        return 0
    pair_key = (
        design_index,
        int(pair_index),
        len(return_edges) - 1 - edge_index,
    )
    pair_slot = int(slots.get(pair_key, 0))
    if pair_slot != 0:
        return _sign(pair_slot)

    # Forward Main Lane has no side sign. In that case use the original Return
    # geometry first; if it gives no evidence, the group-normalizer below will
    # place the Return on the existing group's outer side rather than crossing.
    return 0


def _entry_side_for_use(use, designs, edge_crs, work, slots):
    """Determine the immutable physical side at the point this Cable joins."""
    design = designs[use.design_index]
    segment = design.get("segments", [])[use.segment_index]

    side = _entry_side_from_geometry(segment, use.edge_key, edge_crs, work)
    if side in (1, -1):
        return side

    side = _paired_return_side(
        designs, use.design_index, use.segment_index, use.edge_index, slots
    )
    if side in (1, -1):
        return side

    old = int(slots.get((use.design_index, use.segment_index, use.edge_index), 0))
    if old != 0:
        return _sign(old)

    hint = int(use.side_hint or 0)
    if hint in (1, -1):
        return hint
    return 0


def _is_new_entry(use, by_edge):
    """A cable is new to the shared group on its first edge with another use."""
    for edge in sorted(
        by_edge,
        key=lambda e: min(
            (u.segment_index, u.edge_index)
            for u in by_edge[e]
            if u.design_index == use.design_index and u.segment_index == use.segment_index
        ) if any(u.design_index == use.design_index and u.segment_index == use.segment_index for u in by_edge[e]) else (10**9, 10**9),
    ):
        members = by_edge[edge]
        own = any(
            u.design_index == use.design_index
            and u.segment_index == use.segment_index
            for u in members
        )
        other = any(u.design_index != use.design_index for u in members)
        if own and other:
            return edge == use.edge_key
    return use.edge_index == 0


def _group_other_side_candidates(use, by_edge, slots):
    """Return the signs already occupied by other cables on this physical Edge."""
    signs = []
    for other in by_edge.get(use.edge_key, []):
        if other.design_index == use.design_index and other.segment_index == use.segment_index:
            continue
        value = int(slots.get((other.design_index, other.segment_index, other.edge_index), 0))
        if value != 0:
            signs.append(_sign(value))
    return signs


def _outer_side_for_new_join(entry_side, use, by_edge, slots):
    """Keep a new cable on its entry side and place it outside the group."""
    occupied_same = []
    for other in by_edge.get(use.edge_key, []):
        if other.design_index == use.design_index and other.segment_index == use.segment_index:
            continue
        value = int(slots.get((other.design_index, other.segment_index, other.edge_index), 0))
        if value != 0 and _sign(value) == entry_side:
            occupied_same.append(abs(value))
    # Main Lane is signless. A new cable entering the left/right side therefore
    # starts at the nearest free magnitude on that same physical side.
    magnitude = max(1, max(occupied_same, default=0) + 1)
    return entry_side * magnitude


def _patched_plan(designs, dc, edge_layer, spacing):
    result = _ORIGINAL_PLAN(designs, dc, edge_layer, spacing)
    if not result or len(result) < 5:
        return result

    edge_crs, work, ordered, routes, slots = result
    pending, _ = _core._collect_uses(designs, edge_crs, work)
    by_edge = {}
    for use in pending:
        by_edge.setdefault(use.edge_key, []).append(use)

    # Store the side of first entry per continuous segment. Once a cable has
    # joined the Group, subsequent free slots never authorize an opposite-side
    # jump.
    entry_side = {}
    join_adjusted = 0
    for use in pending:
        if not _is_new_entry(use, by_edge):
            continue
        side = _entry_side_for_use(use, designs, edge_crs, work, slots)
        if side not in (1, -1):
            # Return from a Main-Lane Forward has no intrinsic sign. In that
            # case keep any side already present for this Return segment;
            # otherwise prefer the side indicated by the physical entry hint.
            existing = [
                _sign(int(slots.get((u.design_index, u.segment_index, u.edge_index), 0)))
                for u in by_edge.get(use.edge_key, [])
                if u.design_index == use.design_index
                and u.segment_index == use.segment_index
                and int(slots.get((u.design_index, u.segment_index, u.edge_index), 0)) != 0
            ]
            side = existing[0] if existing else 1
        entry_side[(use.design_index, use.segment_index)] = side

    for use in pending:
        key = (use.design_index, use.segment_index, use.edge_index)
        current = int(slots.get(key, 0))
        if current == 0:
            # Main Lane is not a left/right side. Do not turn Main into a side
            # lane merely to satisfy the new-entry rule.
            continue

        sticky = entry_side.get((use.design_index, use.segment_index))
        if sticky not in (1, -1):
            continue
        if use.edge_index > 0:
            previous = int(slots.get((use.design_index, use.segment_index, use.edge_index - 1), 0))
            if previous != 0:
                # Once inside the group, preserve the established physical side.
                if _sign(current) != sticky:
                    magnitude = max(1, abs(current))
                    candidate = sticky * magnitude
                    occupied = {
                        int(v) for k, v in slots.items()
                        if k[0] == use.design_index
                        and k[1] == use.segment_index
                        and k != key
                    }
                    while candidate in occupied:
                        magnitude += 1
                        candidate = sticky * magnitude
                    slots[key] = candidate
                    join_adjusted += 1
            continue

        # On the actual group-entry Edge, never insert into the middle of the
        # existing group. Keep the physical entry side and append outside.
        desired = _outer_side_for_new_join(sticky, use, by_edge, slots)
        if _sign(current) != sticky or abs(current) != abs(desired):
            occupied = {
                int(v) for k, v in slots.items()
                if k != key
                and any(
                    o.design_index == k[0]
                    and o.segment_index == k[1]
                    and o.edge_index == k[2]
                    for o in pending
                    if o.edge_key == use.edge_key
                )
            }
            candidate = desired
            while candidate in occupied:
                candidate = sticky * (abs(candidate) + 1)
            slots[key] = candidate
            join_adjusted += 1

    if join_adjusted:
        _core._log(
            f"[JOIN-SIDE] adjusted={join_adjusted}; rule=ENTER_SIDE_STICKY; "
            "new-cable=OUTSIDE; return=NEW_CABLE"
        )

    validator = getattr(_policy, "_validate_final_pole_landings", None)
    if validator is not None:
        validator(designs, dc, edge_crs, work, slots)
    return edge_crs, work, ordered, routes, slots


def _oriented_edge(edge, node, incoming):
    edge_crs = getattr(_direction_fix, "_ORIENT_EDGE_CRS", None)
    work = getattr(_direction_fix, "_ORIENT_WORK_CRS", None)
    a, b = _base._edge_points(edge)
    if edge_crs is not None and work is not None:
        a = _core._tp(a, edge_crs, work)
        b = _core._tp(b, edge_crs, work)
    if incoming:
        if _node_match(b, node):
            return a, b
        if _node_match(a, node):
            return b, a
    else:
        if _node_match(a, node):
            return a, b
        if _node_match(b, node):
            return b, a
    return a, b


def _corner_at_pole(node, in_edge, out_edge, prev_slot, next_slot, spacing):
    in_a, in_b = _oriented_edge(in_edge, node, True)
    out_a, out_b = _oriented_edge(out_edge, node, False)
    in_dir = _base._unit(in_a, in_b)
    out_dir = _base._unit(out_a, out_b)
    incoming_anchor = _core._offset_lane_point(node, in_dir, prev_slot, spacing)
    outgoing_anchor = _core._offset_lane_point(node, out_dir, next_slot, spacing)
    return [QgsPointXY(incoming_anchor), QgsPointXY(outgoing_anchor)]


def _patched_corner(node, in_edge, out_edge, prev_slot, next_slot, spacing, return_context=False):
    prev_slot = int(prev_slot)
    next_slot = int(next_slot)
    if prev_slot == next_slot or return_context:
        return _ORIGINAL_CORNER(
            node, in_edge, out_edge, prev_slot, next_slot, spacing, return_context
        )

    points = _corner_at_pole(node, in_edge, out_edge, prev_slot, next_slot, spacing)
    decision = _core._corner_decision(prev_slot, next_slot, return_context=False)
    _core._log(
        f"[LANE-CHANGE] decision={decision}; prev={prev_slot}; next={next_slot}; "
        "timing=CORNER_POLE; early-change=FORBIDDEN; geometry=AT_POLE"
    )
    return decision, points


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _core._plan = _patched_plan
    _core._build_corner_geometry = _patched_corner
    _INSTALLED = True
    _core._log(
        "[POLICY-EXT] new-entry-side=STICKY; return=NEW_CABLE; "
        "group=OUTSIDE; lane-change=AT_CORNER_POLE; early-change=FORBIDDEN"
    )


install()
