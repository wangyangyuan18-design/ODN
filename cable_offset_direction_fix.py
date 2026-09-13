# -*- coding: utf-8 -*-
"""Runtime fix for direction-sensitive Cable Offset geometry.

Canonical Pole Edge identity is intentionally directionless. This module
keeps that identity for lane allocation but restores the actual route
orientation whenever side hints or corner geometry are calculated.

The module also installs a compact diagnostic logger. Diagnostic identity is
preserved, while verbose coordinates and repeated geometry arrays are reduced
to stable IDs/counts. Lane planning is audited after the authoritative planner
returns, so the diagnostic layer does not change allocation behaviour.
"""

from hashlib import sha1
from math import hypot
import re

from qgis.core import QgsGeometry, QgsPointXY, Qgis

from . import cable_offset_core as _core
from . import cable_offset_layout as _base


_PATCH_TAG = "[route-direction-fix]"
_COORD_RE = re.compile(r"\((-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\)")
_AUDIT_PENDING = None
_AUDIT_ROUTES = None
_AUDIT_EDGE_CRS = None
_AUDIT_WORK_CRS = None


def _same_point(a, b, eps=1e-6):
    return hypot(float(a.x()) - float(b.x()), float(a.y()) - float(b.y())) <= eps


def _point_id_text(point_text):
    """Return a short stable ID for a coordinate pair already formatted as text."""
    match = _COORD_RE.fullmatch(point_text.strip())
    if not match:
        return "#----"
    x = round(float(match.group(1)), 3)
    y = round(float(match.group(2)), 3)
    token = f"{x:.3f},{y:.3f}".encode("utf-8")
    return "#" + sha1(token).hexdigest()[:4].upper()


def _edge_id(edge):
    """Stable short physical-edge ID; never print endpoint coordinates."""
    try:
        a, b = _base._edge_points(edge)
        pa = (round(float(a.x()), 3), round(float(a.y()), 3))
        pb = (round(float(b.x()), 3), round(float(b.y()), 3))
        token = repr(tuple(sorted((pa, pb)))).encode("utf-8")
        return "#" + sha1(token).hexdigest()[:4].upper()
    except Exception:
        return "#----"


def _compact_log(message, level=Qgis.Info):
    """Compress high-volume diagnostics without changing behaviour."""
    text = str(message)

    if text.startswith(_PATCH_TAG):
        same = re.search(r"same=(\d)", text)
        direction = "SAME" if same and same.group(1) == "1" else "REVERSED"
        link = re.search(r"link=([^;]+)", text)
        segment = re.search(r"segment=([^;]+)", text)
        edge = re.search(r"edge=([^;]+)", text)
        text = (
            f"{_PATCH_TAG} link={link.group(1) if link else '?'}; "
            f"segment={segment.group(1) if segment else '?'}; "
            f"edge={edge.group(1) if edge else '?'}; direction={direction}"
        )

    if text.startswith("[corner-debug]"):
        node_match = re.search(r"node=(\([^)]*\))", text)
        point_list = re.search(r"points=\[(.*?)\]; direction=", text)
        node_id = _point_id_text(node_match.group(1)) if node_match else "#----"
        point_count = 0
        if point_list:
            payload = point_list.group(1).strip()
            if payload:
                point_count = len(_COORD_RE.findall(payload))
        decision = re.search(r"decision=([^;]+)", text)
        prev_slot = re.search(r"prev_slot=([^;]+)", text)
        next_slot = re.search(r"next_slot=([^;]+)", text)
        text = (
            f"[corner-debug] decision={decision.group(1) if decision else '?'}; "
            f"prev={prev_slot.group(1) if prev_slot else '?'}; "
            f"next={next_slot.group(1) if next_slot else '?'}; "
            f"node={node_id}; points={point_count}; direction=ORIENTED"
        )

    if text.startswith("[pole-landing-policy]"):
        node_match = re.search(r"blocked-node=(\([^)]*\))", text)
        if node_match:
            text = text.replace(
                f"blocked-node={node_match.group(1)}",
                f"node={_point_id_text(node_match.group(1))}",
            )
        text = text.replace("blocked-node=", "node=")

    _ORIGINAL_LOG(text, level)


_ORIGINAL_LOG = _core._log
_core._log = _compact_log


def _oriented_edge(edge, node, incoming):
    """Orient a canonical edge relative to the route node."""
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
    return a, b


_ORIENT_EDGE_CRS = None
_ORIENT_WORK_CRS = None


def _collect_uses(designs, edge_crs, work_crs):
    """Use actual directed route edges for side hints while keeping physical
    Edge identity canonical for allocation."""
    global _ORIENT_EDGE_CRS, _ORIENT_WORK_CRS
    global _AUDIT_PENDING, _AUDIT_ROUTES, _AUDIT_EDGE_CRS, _AUDIT_WORK_CRS
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
    _AUDIT_PENDING = pending
    _AUDIT_ROUTES = routes
    _AUDIT_EDGE_CRS = edge_crs
    _AUDIT_WORK_CRS = work_crs
    return pending, routes


def _audit_plan_result(designs, dc, edge_layer, spacing, slots):
    """Emit one compact evidence chain for lane jumps, gaps and pole landing.

    This is post-plan diagnostics only. It does not modify the returned slot map.
    """
    pending = _AUDIT_PENDING or []
    edge_crs = _AUDIT_EDGE_CRS or edge_layer.crs()
    work = _AUDIT_WORK_CRS
    if work is None or not pending:
        return

    occupancy = _core._occupancy(dc, designs, work)
    spatial_index, geometries = _base._build_existing_index(occupancy, work)
    reserved_cache = {}

    def reserved(edge):
        if edge not in reserved_cache:
            a, b = _base._edge_points(edge)
            a = _core._tp(a, edge_crs, work)
            b = _core._tp(b, edge_crs, work)
            reserved_cache[edge] = _base._existing_slot_occupancy(
                QgsGeometry.fromPolylineXY([a, b]), spacing, spatial_index, geometries
            )
        return set(reserved_cache[edge])

    by_edge = {}
    for use in pending:
        by_edge.setdefault(use.edge_key, []).append(use)

    # One line per physical edge. This is deliberately the main diagnostic
    # record; individual route-direction and corner records remain separate.
    for edge, uses in by_edge.items():
        rsv = reserved(edge)
        assigned = []
        for use in uses:
            key = (use.design_index, use.segment_index, use.edge_index)
            chosen = int(slots.get(key, getattr(use, "slot", 0)))
            prior = None
            if use.edge_index > 0:
                prior = slots.get((use.design_index, use.segment_index, use.edge_index - 1))
            if chosen == 0:
                source = "MAIN"
            elif prior == chosen:
                source = "KEEP"
            elif prior not in (None, 0) and (1 if prior > 0 else -1) == (1 if chosen > 0 else -1):
                source = "REPACK"
            elif prior not in (None, 0):
                source = "CROSS_SIDE"
            else:
                source = "SIDE_HINT"
            delta = "NA" if prior is None else f"{chosen-prior:+d}"
            assigned.append(
                f"{use.design_index}/{use.segment_index}/{use.edge_index}:{chosen}/{delta}/{source}"
            )

        values = sorted(set(int(slots.get((u.design_index, u.segment_index, u.edge_index), getattr(u, "slot", 0))) for u in uses))
        positive = sorted(v for v in values if v > 0)
        negative = sorted(abs(v) for v in values if v < 0)
        gaps = []
        if positive:
            missing = [str(i) for i in range(1, max(positive) + 1) if i not in positive]
            if missing:
                gaps.append("+" + ",".join(missing))
        if negative:
            missing = [str(i) for i in range(1, max(negative) + 1) if i not in negative]
            if missing:
                gaps.append("-" + ",".join(missing))

        links = ",".join(str(designs[u.design_index].get("link", "")) for u in uses)
        _core._log(
            f"[edge-audit] edge={_edge_id(edge)}; links={links}; "
            f"reserved={','.join(str(x) for x in sorted(rsv)) or 'NONE'}; "
            f"slots={'|'.join(assigned)}; gaps={','.join(gaps) or 'NONE'}"
        )

    # Endpoint/corner landing audit: enough to tell whether a segment ends at
    # a physical Pole or only reaches its offset lane. No coordinates are kept.
    for design_index, design in enumerate(designs or []):
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
            if not edges:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            first_slot = int(slots.get((design_index, segment_index, 0), 0))
            last_slot = int(slots.get((design_index, segment_index, len(edges)-1), 0))
            start_physical = bool(segment.get("_odn_special_start")) or first_slot == 0
            end_physical = bool(segment.get("_odn_special_end")) or last_slot == 0
            _core._log(
                f"[pole-audit] link={design.get('link','')}; segment={segment_index}; "
                f"start={'PHYSICAL' if start_physical else 'OFFSET'}; "
                f"end={'PHYSICAL' if end_physical else 'OFFSET'}; "
                f"start_slot={first_slot}; end_slot={last_slot}"
            )

    _core._log(
        f"[audit-summary] edges={len(by_edge)}; "
        f"note=EDGE(slot/delta/source/reserved/gap)+POLE(start/end)"
    )


_ORIGINAL_PLAN = _core._plan


def _plan_with_audit(designs, dc, edge_layer, spacing):
    result = _ORIGINAL_PLAN(designs, dc, edge_layer, spacing)
    try:
        _audit_plan_result(designs, dc, edge_layer, spacing, result[-1])
    except Exception as exc:
        # Diagnostics must never break production design.
        _ORIGINAL_LOG(f"[audit-warning] {type(exc).__name__}: {exc}", Qgis.Warning)
    return result


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


# Install the direction and diagnostic seams. The authoritative allocation and
# geometry algorithms remain unchanged.
_core._collect_uses = _collect_uses
_core._build_corner_geometry = _build_corner_geometry
_core._plan = _plan_with_audit
