# -*- coding: utf-8 -*-
"""Unified Offset Core policy rules.

This module is a policy layer for the single authoritative ``cable_offset_core``.
It does not implement a second offset engine. It supplies two topology rules:

1. Return is a real relation, not merely a ``FATRETURN`` endpoint label.
   A Return is valid only when its Pole Edge chain is exactly the reverse of the
   owning Forward chain. The Return starts at the FAT Pole and ends at the
   opposite end of that same Pole Edge chain.
2. Main Lane (slot 0) and actual Pole landing are different concepts. Slot 0 is
   preferred when available, but an occupied Pole cannot be re-used as an
   ordinary Cable landing. A Cable that would hit an occupied Pole is moved off
   slot 0 on the incident Edge while the existing Cable keeps Main Lane ownership.

This is one policy layer on the canonical Offset Core; it is not a second offset
implementation and does not change CRS or route topology.
"""

from math import hypot

from qgis.core import QgsPointXY

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


def _return_candidates(design, sequence_ids):
    """Find the physical FAT-origin Return relation.

    Legacy records may contain a FATRETURN marker. That marker is metadata only.
    The physical Return must be represented by the segment starting at FAT whose
    full Pole Edge chain is the exact reverse of the marker/forward chain.
    """
    segments = design.get("segments", []) or []
    candidates = []
    for marker_index, marker_segment in enumerate(segments):
        start_item = _sequence_item(sequence_ids, marker_index)
        end_item = _sequence_item(sequence_ids, marker_index + 1)
        if not (_is_explicit_return_marker(start_item) or _is_explicit_return_marker(end_item)):
            continue
        marker_edges = _edge_list(marker_segment)
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
                candidates.append({
                    "return_segment": fat_index,
                    "marker_segment": marker_index,
                    "edge_count": len(fat_edges),
                    "fat_at_start": True,
                })
            elif _is_fat(fat_end):
                candidates.append({
                    "return_segment": marker_index,
                    "marker_segment": marker_index,
                    "edge_count": len(marker_edges),
                    "fat_at_start": False,
                })
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

    for candidate in _return_candidates(design, sequence_ids):
        idx = int(candidate["return_segment"])
        if not (0 <= idx < len(segments)):
            continue
        segment = segments[idx]
        start_item = _sequence_item(sequence_ids, idx)
        end_item = _sequence_item(sequence_ids, idx + 1)

        if _is_fat(start_item):
            # Authoritative Return definition: FAT pole is the start point.
            segment["_odn_return_chain"] = True
            segment["_odn_return_start"] = True
            segment["_odn_return_end"] = False
            segment["_odn_return_role"] = "FAT_TO_SAME_POLE_EDGE_FAR_END"
            segment["_odn_special_start"] = True
            # The far end is a normal Pole Edge endpoint, not a shared landing.
            segment["_odn_special_end"] = False
        elif _is_fat(end_item):
            # The topology is in the opposite direction. Keep it explicit so
            # the route producer can reverse it before materializing a Return.
            segment["_odn_return_chain"] = True
            segment["_odn_return_start"] = False
            segment["_odn_return_end"] = True
            segment["_odn_return_role"] = "FAT_AT_END_REVERSE_REQUIRED"
            segment["_odn_special_start"] = False
            segment["_odn_special_end"] = True

        _core._log(
            f"[return-policy] {design.get('fdt', '')}/{design.get('link', '')}; "
            f"segment={idx}; role={segment.get('_odn_return_role')}; "
            f"edge_count={candidate.get('edge_count', 0)}; "
            f"rule=FAT_START_SAME_POLE_EDGE_CHAIN_REVERSED"
        )

    # FATRETURN is only a marker. It must not turn the opposite Pole into a
    # physical shared landing by itself.
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
    records = {}
    for design_index, design in enumerate(designs or []):
        sequence_ids = design.get("sequence_ids", []) or []
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = _edge_list(segment)
            if not edges:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            start_item = _sequence_item(sequence_ids, segment_index)
            end_item = _sequence_item(sequence_ids, segment_index + 1)
            if _is_shared_landing(start_item):
                records.setdefault(_node_key(nodes[0]), []).append({
                    "design_index": design_index,
                    "segment_index": segment_index,
                    "kind": _item_kind(start_item),
                })
            if _is_shared_landing(end_item) and not segment.get("_odn_return_chain"):
                records.setdefault(_node_key(nodes[-1]), []).append({
                    "design_index": design_index,
                    "segment_index": segment_index,
                    "kind": _item_kind(end_item),
                })
    return records


def _candidate_nonzero_slot(side_hint, used):
    preferred = 1 if int(side_hint or 0) >= 0 else -1
    for magnitude in range(1, 21):
        for sign in (preferred, -preferred):
            candidate = sign * magnitude
            if candidate not in used:
                return candidate
    return preferred * 21


def _external_endpoint_points(dc, designs, work):
    """Read endpoints of external DC only; current-link DC is excluded."""
    try:
        memory = _core._occupancy(dc, designs, work)
    except Exception:
        memory = None
    result = []
    if memory is None:
        return result
    for feature in memory.getFeatures():
        try:
            geom = feature.geometry()
            if geom.isEmpty():
                continue
            if geom.isMultipart():
                for part in geom.asMultiPolyline():
                    if part:
                        result.extend((QgsPointXY(part[0]), QgsPointXY(part[-1])))
            else:
                part = geom.asPolyline()
                if part:
                    result.extend((QgsPointXY(part[0]), QgsPointXY(part[-1])))
        except Exception:
            continue
    return result


def _point_occupied(point, endpoints):
    return any(
        hypot(point.x() - other.x(), point.y() - other.y()) <= NODE_EPS_M
        for other in endpoints
    )


def _uses(designs, edge_crs, work):
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
                start = _core._tp(QgsPointXY(float(stored[0][0]), float(stored[0][1])), edge_crs, work)
                end = _core._tp(QgsPointXY(float(stored[-1][0]), float(stored[-1][1])), edge_crs, work)
            else:
                start, end = nodes[0], nodes[-1]
            for edge_index, edge in enumerate(edges):
                a, b = _base._edge_points(edge)
                a = _core._tp(a, edge_crs, work)
                b = _core._tp(b, edge_crs, work)
                result[(design_index, segment_index, edge_index)] = {
                    "design_index": design_index,
                    "segment_index": segment_index,
                    "edge_index": edge_index,
                    "start_node": _node_key(nodes[edge_index]),
                    "end_node": _node_key(nodes[edge_index + 1]),
                    "side_hint": _base._route_side_hint(start, end, a, b),
                    "return_chain": bool(segment.get("_odn_return_chain")),
                }
    return result


def _enforce_pole_landing_exclusion(designs, dc, edge_crs, work, slots):
    """Keep slot 0 primary, but never create a second independent Pole landing."""
    records = _endpoint_landing_records(designs, edge_crs, work)
    external_endpoints = _external_endpoint_points(dc, designs, work)
    uses = _uses(designs, edge_crs, work)
    changed = 0

    for use_key, use in uses.items():
        if use["return_chain"]:
            continue

        start_key = use["start_node"]
        end_key = use["end_node"]
        blocked_nodes = set()
        for node_key in (start_key, end_key):
            owners = records.get(node_key, [])
            other_design_owner = any(
                item["design_index"] != use["design_index"]
                for item in owners
            )
            external_owner = _point_occupied(QgsPointXY(*node_key), external_endpoints)
            if other_design_owner or external_owner:
                blocked_nodes.add(node_key)

        if not blocked_nodes or int(slots.get(use_key, 0)) != 0:
            continue

        # If this design itself owns a special landing at the node, it is the
        # legitimate Main-Lane owner and stays on slot 0. Only another cable
        # is pushed to a parallel lane.
        own_owner = any(
            item["design_index"] == use["design_index"]
            for node_key in blocked_nodes
            for item in records.get(node_key, [])
        )
        if own_owner and not any(
            item["design_index"] != use["design_index"]
            for node_key in blocked_nodes
            for item in records.get(node_key, [])
        ):
            continue

        used = {
            int(value)
            for other_key, value in slots.items()
            if other_key[1:] == use_key[1:]
        }
        chosen = _candidate_nonzero_slot(use.get("side_hint", 0), used)
        slots[use_key] = int(chosen)
        changed += 1
        blocked = next(iter(blocked_nodes))
        _core._log(
            f"[pole-landing-policy] design={use_key[0]}; segment={use_key[1]}; edge={use_key[2]}; "
            f"blocked-node={blocked}; slot=0->{chosen}; "
            f"rule=MAIN_LANE_FIRST_BUT_NO_SECOND_POLE_LANDING"
        )

    return changed


def _patched_plan(designs, dc, edge_layer, spacing):
    edge_crs, work, ordered, routes, slots = _ORIGINAL_PLAN(designs, dc, edge_layer, spacing)
    changed = _enforce_pole_landing_exclusion(designs, dc, edge_crs, work, slots)
    if changed:
        _core._log(
            f"[pole-landing-policy] adjusted={changed}; "
            f"main-lane-priority=ON; ordinary-pole-exclusive=ON"
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
        "[policy] Return + Pole Landing rules installed; "
        "return=FAT_START/SAME_POLE_EDGE_CHAIN_REVERSED; "
        "main-lane-first=ON; ordinary-pole-exclusive=ON"
    )


install()
