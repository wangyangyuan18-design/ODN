# -*- coding: utf-8 -*-
"""Final Offset policy extensions for new-cable joining and lane-change timing.

Rules:
1. A new Cable, including a true Return Cable, joins a common Cable Group on
   the physical side from which it originally enters. Once that entry side is
   established, it is sticky for the segment unless an explicit engineering
   conflict requires a change.
2. A normal Lane change happens at the route Corner Pole, not before it.
   Incoming geometry stays on the old Lane up to the Pole; outgoing geometry
   starts on the new Lane from the same Pole. No ordinary 0.30 m takeoff is
   introduced by this rule.

This module is a narrow extension of the single unified cable_offset_policy
seam. It does not create another Lane allocator or a second geometry engine.
"""

from math import hypot

from qgis.core import QgsPointXY

from . import cable_offset_core as _core
from . import cable_offset_layout as _base
from . import cable_offset_policy as _policy

_INSTALLED = False
_ORIGINAL_PLAN = _core._plan
_ORIGINAL_CORNER = _core._build_corner_geometry


def _sign(value):
    return 1 if int(value) > 0 else -1


def _node_match(a, b, eps=1e-7):
    return hypot(float(a.x()) - float(b.x()), float(a.y()) - float(b.y())) <= eps


def _entry_side_from_original_geometry(segment, edge, edge_crs, work):
    """Infer the side where a new cable physically enters the shared route.

    Prefer the original segment geometry because it represents the cable's
    actual incoming side before the Offset Core rewrites its final lanes.
    """
    points = segment.get("points", []) or []
    if len(points) < 2:
        return 0
    try:
        a, b = _base._edge_points(edge)
        a = _core._tp(a, edge_crs, work)
        b = _core._tp(b, edge_crs, work)
        tangent_x = b.x() - a.x()
        tangent_y = b.y() - a.y()
        length = hypot(tangent_x, tangent_y)
        if length <= 1e-9:
            return 0
        for raw in points[1:]:
            if len(raw) < 2:
                continue
            probe = _core._tp(QgsPointXY(float(raw[0]), float(raw[1])), edge_crs, work)
            cross = tangent_x * (probe.y() - a.y()) - tangent_y * (probe.x() - a.x())
            eps = max(1e-7, length * 1e-7)
            if abs(cross) > eps:
                return 1 if cross > 0 else -1
    except Exception:
        return 0
    return 0


def _paired_return_side(design, segment_index, edge_index, slots):
    """Recover the Return's original group side from its paired Forward route."""
    segment = design.get("segments", [])[segment_index]
    if not segment.get("_odn_return_chain"):
        return 0
    pair_index = segment.get("_odn_return_pair_segment")
    if pair_index is None:
        return 0
    return_edges = [
        e for raw in segment.get("edge_sequence", []) or []
        if (e := _base._canonical_edge(raw))
    ]
    if edge_index >= len(return_edges):
        return 0
    pair_key = (
        next((i for i, d in enumerate(_DESIGNS) if d is design), -1),
        int(pair_index),
        len(return_edges) - 1 - edge_index,
    )
    previous = slots.get(pair_key)
    if previous not in (None, 0):
        return _sign(previous)
    return 0


_DESIGNS = []


def _patched_plan(designs, dc, edge_layer, spacing):
    global _DESIGNS
    result = _ORIGINAL_PLAN(designs, dc, edge_layer, spacing)
    if not result or len(result) < 5:
        return result

    _DESIGNS = designs
    edge_crs, work, ordered, routes, slots = result
    pending, _ = _core._collect_uses(designs, edge_crs, work)

    # Stable entry-side memory is intentionally segment-scoped. A new cable is
    # allowed to join a group on the side from which it physically enters, but
    # it must not switch side later merely because another side has a free slot.
    entry_side = {}
    for use in pending:
        if use.edge_index != 0:
            continue
        design = designs[use.design_index]
        segment = design.get("segments", [])[use.segment_index]
        seg_key = (use.design_index, use.segment_index)
        side = _entry_side_from_original_geometry(
            segment, use.edge_key, edge_crs, work
        )
        if side == 0:
            side = _paired_return_side(design, use.segment_index, use.edge_index, slots)
        if side == 0:
            old = int(slots.get((use.design_index, use.segment_index, use.edge_index), 0))
            if old:
                side = _sign(old)
        if side == 0:
            side = 1 if int(use.side_hint or 0) >= 0 else -1
        entry_side[seg_key] = side

    # Enforce the new-cable / Return entry-side rule without disturbing
    # existing same-side order. Repack only the affected segment where a
    # first-edge assignment would otherwise cross to the other side.
    changed = 0
    for use in pending:
        key = (use.design_index, use.segment_index, use.edge_index)
        if key not in slots:
            continue
        seg_key = (use.design_index, use.segment_index)
        remembered = entry_side.get(seg_key)
        if remembered not in (1, -1):
            continue
        value = int(slots[key])
        if use.edge_index == 0 and value != 0 and _sign(value) != remembered:
            # Preserve magnitude when possible; move only to the remembered
            # physical side. Main Lane remains slot 0 when it is valid.
            magnitude = max(1, abs(value))
            candidate = remembered * magnitude
            occupied = {int(v) for k, v in slots.items() if k[1:] == key[1:] and k != key}
            while candidate in occupied:
                magnitude += 1
                candidate = remembered * magnitude
            slots[key] = candidate
            changed += 1

        if use.edge_index > 0:
            previous = int(slots.get((use.design_index, use.segment_index, use.edge_index - 1), 0))
            current = int(slots[key])
            if previous != 0 and current != 0 and _sign(previous) != remembered:
                magnitude = max(1, abs(current))
                candidate = remembered * magnitude
                occupied = {int(v) for k, v in slots.items() if k[0] == use.design_index and k[1] == use.segment_index and k != key}
                while candidate in occupied:
                    magnitude += 1
                    candidate = remembered * magnitude
                slots[key] = candidate
                changed += 1

    if changed:
        _core._log(
            f"[JOIN-SIDE] new-entry-side-adjusted={changed}; "
            "rule=ENTER_SIDE_STICKY; return=NEW_CABLE"
        )

    # Re-run the unified final landing validator after any slot correction.
    validator = getattr(_policy, "_validate_final_pole_landings", None)
    if validator is not None:
        validator(designs, dc, edge_crs, work, slots)

    return edge_crs, work, ordered, routes, slots


def _oriented_edge(edge, node, incoming, edge_crs, work):
    a, b = _base._edge_points(edge)
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
    # Direction-aware work CRS endpoints.
    edge_crs = getattr(_core, "_ORIENT_EDGE_CRS", None)
    work = getattr(_core, "_ORIENT_WORK_CRS", None)
    if edge_crs is None or work is None:
        # Direction seam keeps these globals on its own module; discover the
        # source by using the current work edge coordinates when available.
        try:
            in_a, in_b = _core._edge_points_work(in_edge)
            out_a, out_b = _core._edge_points_work(out_edge)
        except Exception:
            return None
    else:
        in_a, in_b = _oriented_edge(in_edge, node, True, edge_crs, work)
        out_a, out_b = _oriented_edge(out_edge, node, False, edge_crs, work)

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
    if points is None:
        return _ORIGINAL_CORNER(
            node, in_edge, out_edge, prev_slot, next_slot, spacing, return_context
        )

    decision = _core._corner_decision(prev_slot, next_slot, return_context=False)
    _core._log(
        f"[LANE-CHANGE] decision={decision}; prev={prev_slot}; next={next_slot}; "
        f"timing=CORNER_POLE; early-change=FORBIDDEN"
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
        "lane-change=AT_CORNER_POLE; early-change=FORBIDDEN"
    )


install()
