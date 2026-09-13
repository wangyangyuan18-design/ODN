# -*- coding: utf-8 -*-
"""Runtime seam for Cable Offset direction, lane audit and Pole landing.

The authoritative lane allocator remains in cable_offset_core.py.
This module only restores route orientation, adds compact diagnostics, and
ensures a lane transition that returns to Main Lane actually passes through
its physical Pole node.
"""

from hashlib import sha1
from math import hypot
import re

from qgis.core import QgsGeometry, QgsPointXY, Qgis

from . import cable_offset_core as _core
from . import cable_offset_layout as _base


_PATCH_TAG = "[route-direction-fix]"
_COORD_RE = re.compile(r"\((-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\)")
_ORIENT_EDGE_CRS = None
_ORIENT_WORK_CRS = None
_AUDIT_PENDING = None
_AUDIT_EDGE_CRS = None
_AUDIT_WORK_CRS = None


def _same_point(a, b, eps=1e-6):
    return hypot(float(a.x()) - float(b.x()), float(a.y()) - float(b.y())) <= eps


def _point_id_text(point_text):
    match = _COORD_RE.fullmatch(str(point_text).strip())
    if not match:
        return "#----"
    token = f"{round(float(match.group(1)), 3):.3f},{round(float(match.group(2)), 3):.3f}".encode("utf-8")
    return "#" + sha1(token).hexdigest()[:4].upper()


def _edge_id(edge):
    try:
        a, b = _base._edge_points(edge)
        pa = (round(float(a.x()), 3), round(float(a.y()), 3))
        pb = (round(float(b.x()), 3), round(float(b.y()), 3))
        token = repr(tuple(sorted((pa, pb)))).encode("utf-8")
        return "#" + sha1(token).hexdigest()[:4].upper()
    except Exception:
        return "#----"


def _compact_log(message, level=Qgis.Info):
    """Normalize diagnostics into a small evidence chain."""
    text = str(message)

    if text.startswith(_PATCH_TAG):
        same = re.search(r"same=(\d)", text)
        link = re.search(r"link=([^;]+)", text)
        seg = re.search(r"segment=([^;]+)", text)
        edge = re.search(r"edge=([^;]+)", text)
        direction = "S" if same and same.group(1) == "1" else "R"
        text = f"[DIR] {link.group(1).strip() if link else '?'} / S{seg.group(1).strip() if seg else '?'} / E{edge.group(1).strip() if edge else '?'} / {direction}"

    if text.startswith("[corner-debug]") or text.startswith("[corner]"):
        decision = re.search(r"decision=([^;]+)", text)
        prev_slot = re.search(r"prev_slot=([^;]+)", text)
        next_slot = re.search(r"next_slot=([^;]+)", text)
        node = re.search(r"node=(\([^)]*\))", text)
        link = re.search(r"link=([^;]+)", text)
        seg = re.search(r"segment=([^;]+)", text)
        edge = re.search(r"edge=([^;]+)", text)
        text = (
            f"[CORNER] {link.group(1).strip() if link else '?'}"
            f"/S{seg.group(1).strip() if seg else '?'}"
            f"/E{edge.group(1).strip() if edge else '?'} | "
            f"{prev_slot.group(1).strip() if prev_slot else '?'}→"
            f"{next_slot.group(1).strip() if next_slot else '?'} | "
            f"{decision.group(1).strip() if decision else '?'} | "
            f"N={_point_id_text(node.group(1)) if node else '#----'}"
        )

    if text.startswith("[pole-landing-policy]"):
        node = re.search(r"blocked-node=(\([^)]*\))", text)
        node_id = _point_id_text(node.group(1)) if node else "#----"
        text = re.sub(r"blocked-node=\([^)]*\)", f"node={node_id}", text)
        text = text.replace("[pole-landing-policy]", "[POLICY]")

    if text.startswith("[edge-audit]"):
        text = text.replace("[edge-audit]", "[EDGE]", 1)
    elif text.startswith("[pole-audit]"):
        text = text.replace("[pole-audit]", "[POLE]", 1)
    elif text.startswith("[audit-summary]"):
        text = text.replace("[audit-summary]", "[SUMMARY]", 1)

    _ORIGINAL_LOG(text, level)


_ORIGINAL_LOG = _core._log
_core._log = _compact_log


def _oriented_edge(edge, node, incoming):
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


def _collect_uses(designs, edge_crs, work_crs):
    global _ORIENT_EDGE_CRS, _ORIENT_WORK_CRS
    global _AUDIT_PENDING, _AUDIT_EDGE_CRS, _AUDIT_WORK_CRS
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
                use.prev_edge_key = edges[edge_index - 1] if edge_index else None
                ca, cb = _base._edge_points(edge)
                ca = _core._tp(ca, edge_crs, work_crs)
                cb = _core._tp(cb, edge_crs, work_crs)
                same = int(_same_point(a, ca) and _same_point(b, cb))
                _core._log(
                    f"{_PATCH_TAG} link={design.get('link','')}; segment={segment_index}; edge={edge_index}; same={same}; reversed={1-same}"
                )
                pending.append(use)

    _AUDIT_PENDING = pending
    _AUDIT_EDGE_CRS = edge_crs
    _AUDIT_WORK_CRS = work_crs
    return pending, routes


def _audit_plan_result(designs, dc, edge_layer, spacing, slots):
    """Audit planned slots without changing the result."""
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

    for edge, uses in by_edge.items():
        rsv = reserved(edge)
        assigned = []
        values = []
        for use in uses:
            key = (use.design_index, use.segment_index, use.edge_index)
            chosen = int(slots.get(key, getattr(use, "slot", 0)))
            prior = slots.get((use.design_index, use.segment_index, use.edge_index - 1)) if use.edge_index > 0 else None
            if chosen == 0:
                source = "MAIN"
            elif prior == chosen:
                source = "KEEP"
            elif prior not in (None, 0) and (1 if prior > 0 else -1) == (1 if chosen > 0 else -1):
                source = "REPACK"
            elif prior not in (None, 0):
                source = "CROSS"
            else:
                source = "SIDE"
            delta = "NA" if prior is None else f"{chosen-prior:+d}"
            assigned.append(f"{use.design_index}/{use.segment_index}/{use.edge_index}:{chosen}/{delta}/{source}")
            values.append(chosen)

        positive = sorted(v for v in set(values) if v > 0)
        negative = sorted(abs(v) for v in set(values) if v < 0)
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
            f"[edge-audit] edge={_edge_id(edge)}; links={links}; reserved={','.join(str(x) for x in sorted(rsv)) or 'NONE'}; "
            f"slots={'|'.join(assigned)}; gaps={','.join(gaps) or 'NONE'}"
        )

    for design_index, design in enumerate(designs or []):
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
            if not edges:
                continue
            first = int(slots.get((design_index, segment_index, 0), 0))
            last = int(slots.get((design_index, segment_index, len(edges) - 1), 0))
            start_physical = bool(segment.get("_odn_special_start")) or first == 0
            end_physical = bool(segment.get("_odn_special_end")) or last == 0
            _core._log(
                f"[pole-audit] link={design.get('link','')}; segment={segment_index}; "
                f"start={'P' if start_physical else 'O'}; end={'P' if end_physical else 'O'}; "
                f"start_slot={first}; end_slot={last}"
            )

    _core._log(f"[audit-summary] edges={len(by_edge)}")


_ORIGINAL_PLAN = _core._plan


def _plan_with_audit(designs, dc, edge_layer, spacing):
    result = _ORIGINAL_PLAN(designs, dc, edge_layer, spacing)
    try:
        _audit_plan_result(designs, dc, edge_layer, spacing, result[-1])
    except Exception as exc:
        _ORIGINAL_LOG(f"[AUDIT-WARN] {type(exc).__name__}: {exc}", Qgis.Warning)
    return result


def _build_corner_geometry(node, in_edge, out_edge, prev_slot, next_slot, spacing, return_context=False):
    """Use the existing D/E/F core, but preserve physical Pole on lane return.

    The observed failure was:
        nonzero -> 0
    The old seam filtered the physical node from every non-SAME corner. That
    made a cable allocated to Main Lane on the next edge approach the Pole but
    skip the actual Pole coordinate. A symmetric fix is used for 0 -> nonzero:
    the cable must also start the transition at the physical Pole.
    """
    global _ORIENT_EDGE_CRS, _ORIENT_WORK_CRS
    if _ORIENT_EDGE_CRS is None or _ORIENT_WORK_CRS is None:
        in_a, in_b = _core._edge_points_work(in_edge)
        out_a, out_b = _core._edge_points_work(out_edge)
    else:
        in_a, in_b = _oriented_edge(in_edge, node, incoming=True)
        out_a, out_b = _oriented_edge(out_edge, node, incoming=False)

    prev_slot = int(prev_slot)
    next_slot = int(next_slot)
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

    points = [QgsPointXY(p) for p in (points or [])]

    # Physical landing rule at a route Pole. Do not force the node for a
    # normal same-lane nonzero corner; only lane transitions touching Main Lane
    # need an explicit physical Pole coordinate.
    without_node = [p for p in points if not _same_point(p, node, 1e-7)]
    if prev_slot == 0 and next_slot == 0:
        points = [QgsPointXY(node)]
    elif prev_slot == 0 and next_slot != 0:
        points = [QgsPointXY(node)] + without_node
    elif prev_slot != 0 and next_slot == 0:
        points = without_node + [QgsPointXY(node)]
    else:
        points = without_node or points

    landing = "NONE"
    if next_slot == 0:
        landing = "P"
    elif prev_slot == 0:
        landing = "P"

    _core._log(
        f"[corner-debug] BUILT; decision={decision}; prev_slot={prev_slot}; next_slot={next_slot}; "
        f"node={_core._corner_debug_point(node)}; points={_core._corner_debug_points(points)}; "
        f"landing={landing}"
    )
    return decision, points


_core._collect_uses = _collect_uses
_core._build_corner_geometry = _build_corner_geometry
_core._plan = _plan_with_audit
