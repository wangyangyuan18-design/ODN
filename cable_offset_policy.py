# -*- coding: utf-8 -*-
"""Unified Offset Core policy rules.

This module is a policy layer for the single authoritative ``cable_offset_core``.
It does not implement a second offset engine.  It supplies two missing topology
constraints to the canonical core:

1. Return is a real relation, not merely a ``FATRETURN`` endpoint label.
   A Return is valid only when its Pole Edge chain is exactly the reverse of the
   owning Forward chain.  The Return starts at the FAT Pole and ends at the
   opposite end of that same Pole Edge chain.
2. Main Lane (slot 0) and actual Pole landing are different concepts.  Slot 0
   is preferred when available, but an occupied Pole cannot be re-used as an
   ordinary Cable landing.  When another independent Cable already lands on a
   Pole, the affected Cable is moved off Main Lane on the incident Edge instead
   of producing a zero-slot corner at that Pole.

The policy is installed once from ``__init__.py`` after the canonical core is
loaded.  No legacy versioned Offset implementation is used.
"""

from math import hypot

from qgis.core import QgsGeometry, QgsPointXY, Qgis

from . import cable_offset_core as _core
from . import cable_offset_layout as _base

NODE_EPS_M = 0.05

_ORIGINAL_SET_FLAGS = _core._set_flags
_ORIGINAL_PLAN = _core._plan
_INSTALLED = False


def _edge_list(segment):
    return [
        edge
        for raw in (segment.get("edge_sequence", []) or [])
        if (edge := _base._canonical_edge(raw))
    ]


def _edge_chain_equal_forward(a_edges, b_edges):
    if len(a_edges) != len(b_edges):
        return False
    return all(left == right for left, right in zip(a_edges, b_edges))


def _edge_chain_equal_reverse(a_edges, b_edges):
    if len(a_edges) != len(b_edges):
        return False
    return all(left == right for left, right in zip(a_edges, reversed(b_edges)))


def _item_kind(item):
    try:
        return _core._kind(item)
    except Exception:
        return ""


def _is_fat(item):
    return _item_kind(item) == "FAT"


def _is_explicit_return_marker(item):
    return _item_kind(item).startswith("FATRETURN")


def _is_shared_landing(item):
    return _core._shared_node(item) or _is_fat(item)


def _sequence_item(sequence_ids, position):
    if 0 <= position < len(sequence_ids):
        return sequence_ids[position]
    return None


def _node_key(point):
    return round(float(point.x()), 7), round(float(point.y()), 7)


def _node_from_segment(segment, node_index, work, edge_crs):
    try:
        nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)
    except Exception:
        return None
    if 0 <= node_index < len(nodes):
        return QgsPointXY(nodes[node_index])
    return None


def _dc_endpoint_points(dc, work):
    result = []
    if dc is None:
        return result
    for feature in dc.getFeatures():
        try:
            geometry = _core._transform_geometry(feature.geometry(), dc.crs(), work)
            if geometry.isEmpty():
                continue
            if geometry.isMultipart():
                for part in geometry.asMultiPolyline():
                    if part:
                        result.append(QgsPointXY(part[0]))
                        result.append(QgsPointXY(part[-1]))
            else:
                part = geometry.asPolyline()
                if part:
                    result.append(QgsPointXY(part[0]))
                    result.append(QgsPointXY(part[-1]))
        except Exception:
            continue
    return result


def _point_is_occupied(point, endpoint_points):
    if point is None:
        return False
    return any(
        hypot(point.x() - other.x(), point.y() - other.y()) <= NODE_EPS_M
        for other in endpoint_points
    )


def _return_candidates(design, sequence_ids):
    """Find explicit return candidates and the corresponding FAT-origin segment.

    The legacy data model can contain ``FATRETURN`` as a marker.  That marker
    is not itself the physical Return origin.  The real Return is the segment
    whose starting endpoint is the FAT and whose full Pole Edge chain matches
    the marker chain in reverse.
    """
    segments = design.get("segments", []) or []
    candidates = []
    for marker_index, segment in enumerate(segments):
        start_item = _sequence_item(sequence_ids, marker_index)
        end_item = _sequence_item(sequence_ids, marker_index + 1)
        marker_positions = []
        if _is_explicit_return_marker(start_item):
            marker_positions.append(("start", start_item))
        if _is_explicit_return_marker(end_item):
            marker_positions.append(("end", end_item))
        if not marker_positions:
            continue
        marker_edges = _edge_list(segment)
        if not marker_edges:
            continue

        for fat_index, fat_segment in enumerate(segments):
            if fat_index == marker_index:
                continue
            fat_start = _sequence_item(sequence_ids, fat_index)
            fat_end = _sequence_item(sequence_ids, fat_index + 1)
            fat_edges = _edge_list(fat_segment)
            if not fat_edges or not _edge_chain_equal_reverse(fat_edges, marker_edges):
                continue
            if _is_fat(fat_start):
                candidates.append(
                    {
                        "return_segment": fat_index,
                        "marker_segment": marker_index,
                        "fat_position": "start",
                        "marker_position": marker_positions[0][0],
                        "edge_count": len(fat_edges),
                    }
                )
            elif _is_fat(fat_end):
                candidates.append(
                    {
                        "return_segment": marker_index,
                        "marker_segment": marker_index,
                        "fat_position": "end",
                        "marker_position": marker_positions[0][0],
                        "edge_count": len(marker_edges),
                    }
                )
    return candidates


def _apply_return_semantics(design):
    sequence_ids = design.get("sequence_ids", []) or []
    segments = design.get("segments", []) or []
    for segment in segments:
        segment["_odn_return_chain"] = False
        segment["_odn_return_start"] = False
        segment["_odn_return_end"] = False
        segment["_odn_return_role"] = ""
        segment["_odn_return_marker"] = False

    candidates = _return_candidates(design, sequence_ids)
    for candidate in candidates:
        idx = candidate["return_segment"]
        if not (0 <= idx < len(segments)):
            continue
        segment = segments[idx]
        start_item = _sequence_item(sequence_ids, idx)
        end_item = _sequence_item(sequence_ids, idx + 1)

        # Physical Return always starts at the FAT Pole.
        if _is_fat(start_item):
            segment["_odn_return_chain"] = True
            segment["_odn_return_start"] = True
            segment["_odn_return_end"] = False
            segment["_odn_return_role"] = "FAT_TO_SAME_CHAIN_FAR_END"
            # The far end is a normal Pole Edge endpoint, not a FATRETURN landing.
            segment["_odn_special_end"] = False
        elif _is_fat(end_item):
            # Keep the relation explicit.  The current route must be reversed by
            # the data producer before it can be materialized as FAT-origin Return.
            segment["_odn_return_chain"] = True
            segment["_odn_return_start"] = False
            segment["_odn_return_end"] = True
            segment["_odn_return_role"] = "REVERSE_REQUIRED_FAT_AT_END"

        _core._log(
            f"[return-policy] {design.get('fdt','')}/{design.get('link','')}; "
            f"segment={idx}; role={segment.get('_odn_return_role')}; "
            f"edge_count={candidate.get('edge_count', 0)}; "
            f"rule=FAT_START_SAME_POLE_EDGE_CHAIN_REVERSED"
        )

    # A raw FATRETURN endpoint is only a marker.  It must never by itself make
    # the physical far-end Pole a Return landing.
    for index, segment in enumerate(segments):
        start_item = _sequence_item(sequence_ids, index)
        end_item = _sequence_item(sequence_ids, index + 1)
        if _is_explicit_return_marker(start_item) and not segment.get("_odn_return_start"):
            segment["_odn_return_marker"] = True
            segment["_odn_special_start"] = False
        if _is_explicit_return_marker(end_item) and not segment.get("_odn_return_end"):
            segment["_odn_return_marker"] = True
            segment["_odn_special_end"] = False


def _patched_set_flags(designs):
    _ORIGINAL_SET_FLAGS(designs)
    for design in designs or []:
        _apply_return_semantics(design)


def _endpoint_landing_records(designs, edge_crs, work):
    """Return physical landing owners keyed by Pole node."""
    records = {}
    for design_index, design in enumerate(designs or []):
        sequence_ids = design.get("sequence_ids", []) or []
        segments = design.get("segments", []) or []
        for segment_index, segment in enumerate(segments):
            edges = _edge_list(segment)
            if not edges:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            start_item = _sequence_item(sequence_ids, segment_index)
            end_item = _sequence_item(sequence_ids, segment_index + 1)
            if _is_shared_landing(start_item):
                key = _node_key(nodes[0])
                records.setdefault(key, []).append(
                    {
                        "design_index": design_index,
                        "segment_index": segment_index,
                        "role": "START",
                        "kind": _item_kind(start_item),
                    }
                )
            if _is_shared_landing(end_item) and not segment.get("_odn_return_chain"):
                key = _node_key(nodes[-1])
                records.setdefault(key, []).append(
                    {
                        "design_index": design_index,
                        "segment_index": segment_index,
                        "role": "END",
                        "kind": _item_kind(end_item),
                    }
                )
    return records


def _candidate_nonzero_slot(use_side, used):
    preferred = 1 if use_side >= 0 else -1
    for magnitude in range(1, 21):
        for sign in (preferred, -preferred):
            candidate = sign * magnitude
            if candidate not in used:
                return candidate
    return preferred * 21


def _incident_key_map(designs, edge_crs, work):
    """Map slot keys to endpoint Pole keys and route side hints."""
    result = {}
    for design_index, design in enumerate(designs or []):
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = _edge_list(segment)
            if not edges:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            stored = segment.get("points", []) or []
            if len(stored) >= 2:
                start = QgsPointXY(float(stored[0][0]), float(stored[0][1]))
                end = QgsPointXY(float(stored[-1][0]), float(stored[-1][1]))
                start = _core._tp(start, edge_crs, work)
                end = _core._tp(end, edge_crs, work)
            else:
                start, end = nodes[0], nodes[-1]
            for edge_index, edge in enumerate(edges):
                a, b = _base._edge_points(edge)
                a = _core._tp(a, edge_crs, work)
                b = _core._tp(b, edge_crs, work)
                use = {
                    "design_index": design_index,
                    "segment_index": segment_index,
                    "edge_index": edge_index,
                    "key": (design_index, segment_index, edge_index),
                    "start_node": _node_key(nodes[edge_index]),
                    "end_node": _node_key(nodes[edge_index + 1]),
                    "side_hint": _base._route_side_hint(start, end, a, b),
                    "return_chain": bool(segment.get("_odn_return_chain")),
                }
                result[use["key"]] = use
    return result


def _enforce_pole_landing_exclusion(designs, dc, edge_crs, work, slots):
    """Keep Main Lane occupied first, but never land a second Cable on a Pole."""
    records = _endpoint_landing_records(designs, edge_crs, work)
    endpoint_points = _dc_endpoint_points(dc, work)
    uses = _incident_key_map(designs, edge_crs, work)

    changed = 0
    blocked_keys = set()
    for key, items in records.items():
        independent = {item["design_index"] for item in items}
        if len(independent) > 1:
            blocked_keys.add(key)

    for use_key, use in uses.items():
        if use.get("return_chain"):
            continue
        is_blocked = use["start_node"] in blocked_keys or use["end_node"] in blocked_keys
        if not is_blocked:
            start_point = QgsPointXY(*use["start_node"])
            end_point = QgsPointXY(*use["end_node"])
            is_blocked = _point_is_occupied(start_point, endpoint_points) or _point_is_occupied(end_point, endpoint_points)
        if not is_blocked:
            continue

        current = int(slots.get(use_key, 0))
        if current != 0:
            continue

        # Preserve an existing zero-lane landing owner; only non-owners are
        # moved off Main Lane.  For external DC occupation there is no owner,
        # so the new cable is moved off Main Lane as required.
        owner_designs = set()
        for node_key in (use["start_node"], use["end_node"]):
            for item in records.get(node_key, []):
                owner_designs.add(item["design_index"])

        if owner_designs and use["design_index"] in owner_designs:
            continue

        # Occupancy is local to this Pole Edge; choose a free side while
        # retaining the route's physical side hint whenever possible.
        used = {
            int(value)
            for other_key, value in slots.items()
            if other_key[1:] == use_key[1:]
        }
        chosen = _candidate_nonzero_slot(use.get("side_hint", 0), used)
        slots[use_key] = int(chosen)
        changed += 1
        _core._log(
            f"[pole-landing-policy] design={use_key[0]}; segment={use_key[1]}; "
            f"edge={use_key[2]}; blocked-node="
            f"{use['end_node'] if use['end_node'] in blocked_keys else use['start_node']}; "
            f"slot 0 -> {chosen}; rule=NO_SECOND_INDEPENDENT_POLE_LANDING"
        )

    return changed


def _patched_plan(designs, dc, edge_layer, spacing):
    edge_crs, work, ordered, routes, slots = _ORIGINAL_PLAN(designs, dc, edge_layer, spacing)
    changed = _enforce_pole_landing_exclusion(designs, dc, edge_crs, work, slots)
    if changed:
        _core._log(
            f"[pole-landing-policy] adjusted={changed}; "
            f"main-lane-priority=ON; pole-landing-exclusivity=ON"
        )
    return edge_crs, work, ordered, routes, slots


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _core._set_flags = _patched_set_flags
    _core._plan = _patched_plan
    _INSTALLED = True
    _core._log(
        "[policy] unified Return + Pole Landing rules installed; "
        "return=FAT_START/SAME_EDGE_CHAIN_REVERSED; "
        "main-lane-first=ON; ordinary-pole-exclusive=ON"
    )


install()
