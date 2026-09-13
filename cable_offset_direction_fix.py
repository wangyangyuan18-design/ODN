# -*- coding: utf-8 -*-
"""Runtime fix for direction-sensitive Cable Offset geometry.

Canonical Pole Edge identity is intentionally directionless. This module
keeps that identity for lane allocation but restores the actual route
orientation whenever side hints or corner geometry are calculated.
"""

from math import hypot

from qgis.core import QgsPointXY

from . import cable_offset_core as _core
from . import cable_offset_layout as _base


_PATCH_TAG = "[route-direction-fix]"


def _same_point(a, b, eps=1e-6):
    return hypot(float(a.x()) - float(b.x()), float(a.y()) - float(b.y())) <= eps


def _oriented_edge(edge, node, incoming):
    """Orient a canonical edge relative to the route node.

    For an incoming edge the route is other_endpoint -> node.
    For an outgoing edge the route is node -> other_endpoint.
    """
    a, b = _base._edge_points(edge)
    a = _core._tp(a, _ORIENT_EDGE_CRS, _ORIENT_WORK_CRS)
    b = _core._tp(b, _ORIENT_EDGE_CRS, _ORIENT_WORK_CRS)
    if incoming:
        if _same_point(b, node):
            return a, b
        if _same_point(a, node):
            return b, a
    else:
        if _same_point(a, node):
            return a, b
        if _same_point(b, node):
            return b, a
    return (a, b)


_ORIENT_EDGE_CRS = None
_ORIENT_WORK_CRS = None


def _collect_uses(designs, edge_crs, work_crs):
    """Same planner input as the authoritative core, but side_hint is local
    to the actual directed route edge instead of canonical A->B orientation.
    """
    global _ORIENT_EDGE_CRS, _ORIENT_WORK_CRS
    _ORIENT_EDGE_CRS = edge_crs
    _ORIENT_WORK_CRS = work_crs

    pending = []
    routes = {}
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        routes[design_index] = _core._route_metrics(design_index, design, edge_crs, work_crs)
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
            points = segment.get("points", []) or []
            if not edges or len(points) < 2:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            start = _core._tp(QgsPointXY(float(points[0][0]), float(points[0][1])), edge_crs, work_crs)
            end = _core._tp(QgsPointXY(float(points[-1][0]), float(points[-1][1])), edge_crs, work_crs)
            for edge_index, edge in enumerate(edges):
                a = QgsPointXY(nodes[edge_index])
                b = QgsPointXY(nodes[edge_index + 1])
                use = _base._LayoutUse(
                    design_index,
                    segment_index,
                    edge_index,
                    edge,
                    start,
                    end,
                    _base._route_side_hint(start, end, a, b),
                )
                ca, cb = _base._edge_points(edge)
                ca = _core._tp(ca, edge_crs, work_crs)
                cb = _core._tp(cb, edge_crs, work_crs)
                same = int(_same_point(a, ca) and _same_point(b, cb))
                _core._log(
                    f"{_PATCH_TAG} link={design.get('link','')}; segment={segment_index}; "
                    f"edge={edge_index}; route={_core._corner_debug_point(a)}->{_core._corner_debug_point(b)}; "
                    f"canonical={_core._corner_debug_point(ca)}->{_core._corner_debug_point(cb)}; "
                    f"same={same}; reversed={1-same}"
                )
                use.prev_edge_key = edges[edge_index - 1] if edge_index else None
                pending.append(use)
    return pending, routes


def _build_corner_geometry(node, in_edge, out_edge, prev_slot, next_slot, spacing, return_context=False):
    """Build the existing D/E/F corner core with correctly oriented edges."""
    global _ORIENT_EDGE_CRS, _ORIENT_WORK_CRS
    if _ORIENT_EDGE_CRS is None or _ORIENT_WORK_CRS is None:
        in_a, in_b = _core._edge_points_work(in_edge)
        out_a, out_b = _core._edge_points_work(out_edge)
    else:
        in_a, in_b = _oriented_edge(in_edge, node, incoming=True)
        out_a, out_b = _oriented_edge(out_edge, node, incoming=False)

    decision = _core._corner_decision(prev_slot, next_slot, return_context=return_context)

    if decision == _core.CORNER_SAME_LANE_TURN:
        points = _core._same_lane_corner(node, in_a, in_b, out_a, out_b, prev_slot, spacing)
    elif decision == _core.CORNER_EARLY_TURN:
        points = _core._early_turn_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing)
    elif decision == _core.CORNER_CROSS_MAIN_TURN:
        points = _core._cross_main_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing)
    elif decision == _core.CORNER_MAIN_REACH_TURN:
        points = _core._main_reach_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing)
    else:
        points = _core._same_lane_corner(node, in_a, in_b, out_a, out_b, prev_slot, spacing)

    if decision != _core.CORNER_SAME_LANE_TURN or int(prev_slot) != 0 or int(next_slot) != 0:
        filtered = [p for p in points if hypot(p.x() - node.x(), p.y() - node.y()) > 1e-7]
        if filtered:
            points = filtered

    _core._log(
        f"[corner-debug] BUILT; decision={decision}; prev_slot={int(prev_slot)}; "
        f"next_slot={int(next_slot)}; node={_core._corner_debug_point(node)}; "
        f"points={_core._corner_debug_points(points)}; direction=ORIENTED"
    )
    return decision, points


# Install only these two seams. All D/E/F primitives remain in the existing
# authoritative core; this patch only changes the direction supplied to them.
_core._collect_uses = _collect_uses
_core._build_corner_geometry = _build_corner_geometry
