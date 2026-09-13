# -*- coding: utf-8 -*-
"""Non-invasive diagnostics for Offset Core corner geometry.

This module does not change lane allocation or corner geometry.  It wraps the
existing geometry functions and records enough state to distinguish:
1) a wrong lane/slot decision,
2) a wrong corner decision, or
3) a corner geometry function that physically reaches the Pole.

The diagnostic is intentionally isolated so it can be removed without touching
the authoritative Offset Core.
"""

from math import hypot

from qgis.core import Qgis, QgsPointXY

from . import cable_offset_core as _core


_PREFIX = "[CORNER-DIAG]"
_EPS_M = 0.05

_original_build_corner_geometry = getattr(_core, "_build_corner_geometry", None)
_original_geometry = getattr(_core, "_geometry", None)


def _point(point):
    try:
        return f"({float(point.x()):.6f},{float(point.y()):.6f})"
    except Exception:
        return str(point)


def _edge_text(edge):
    try:
        a, b = _core._edge_points_work(edge)
        return f"{_point(a)}->{_point(b)}"
    except Exception:
        try:
            return str(edge)
        except Exception:
            return "<edge>"


def _distance(a, b):
    try:
        return hypot(float(a.x()) - float(b.x()), float(a.y()) - float(b.y()))
    except Exception:
        return 0.0


def _diag_build_corner(node, in_edge, out_edge, prev_slot, next_slot, spacing, return_context=False):
    result = _original_build_corner_geometry(
        node,
        in_edge,
        out_edge,
        prev_slot,
        next_slot,
        spacing,
        return_context=return_context,
    )

    decision, points = result
    distances = [_distance(point, node) for point in (points or [])]
    min_distance = min(distances) if distances else None
    reaches_pole = min_distance is not None and min_distance <= _EPS_M

    # These are the cases that can explain the reported symptom: a return
    # context, a Main-Lane transition, or a generated corner physically landing
    # on the Pole.
    important = (
        bool(return_context)
        or int(prev_slot) == 0
        or int(next_slot) == 0
        or reaches_pole
    )
    if important:
        _core._log(
            f"{_PREFIX} CORNER; decision={decision}; return_context={bool(return_context)}; "
            f"prev_slot={int(prev_slot)}; next_slot={int(next_slot)}; "
            f"node={_point(node)}; min_node_distance={min_distance if min_distance is not None else 'None'}; "
            f"reaches_pole={reaches_pole}; points={_core._corner_debug_points(points)}; "
            f"in_edge={_edge_text(in_edge)}; out_edge={_edge_text(out_edge)}",
            Qgis.Info,
        )

    if reaches_pole and not (int(prev_slot) == 0 and int(next_slot) == 0):
        _core._log(
            f"{_PREFIX} *** POLE-REACH-ANOMALY ***; decision={decision}; "
            f"prev_slot={int(prev_slot)}; next_slot={int(next_slot)}; "
            f"node={_point(node)}; points={_core._corner_debug_points(points)}; "
            f"return_context={bool(return_context)}",
            Qgis.Warning,
        )

    return result


def _diag_geometry(segment, slot_by_edge, spacing, work, edge_crs, control):
    edges = [
        e for raw in segment.get("edge_sequence", []) or []
        if (e := _core._base._canonical_edge(raw))
    ]
    slots = [int(slot_by_edge.get(index, 0)) for index in range(len(edges))]
    return_flags = {
        "special_start": bool(segment.get("_odn_special_start")),
        "special_end": bool(segment.get("_odn_special_end")),
        "return_start": bool(segment.get("_odn_return_start")),
        "return_end": bool(segment.get("_odn_return_end")),
    }

    # Only emit the segment header when it can participate in the reported
    # return/Main-Lane symptom.  This keeps QGIS Log Messages usable.
    if any(return_flags.values()) or 0 in slots:
        sequence_ids = segment.get("sequence_ids", []) or []
        _core._log(
            f"{_PREFIX} SEGMENT; name={segment.get('name', '')}; "
            f"edges={len(edges)}; slots={slots}; sequence_ids={sequence_ids}; "
            f"flags={return_flags}; edge_sequence={edges}",
            Qgis.Info,
        )

    result = _original_geometry(segment, slot_by_edge, spacing, work, edge_crs, control)

    if edges:
        _core._log(
            f"{_PREFIX} SEGMENT-RESULT; name={segment.get('name', '')}; "
            f"slots={slots}; final_points={_core._corner_debug_points(result)}",
            Qgis.Info,
        )
    return result


if _original_build_corner_geometry is not None:
    _core._build_corner_geometry = _diag_build_corner

if _original_geometry is not None:
    _core._geometry = _diag_geometry
