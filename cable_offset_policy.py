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
_ORIGINAL_GEOMETRY = _core._geometry
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

    A Return is identified from topology, not from the FATRETURN label alone:
    there must be a segment starting at FAT whose complete Pole Edge chain has
    an exact reverse match elsewhere in the same Link. FATRETURN, when present,
    is only a legacy metadata marker for the same relation.
    """
    segments = design.get("segments", []) or []
    candidates = []
    seen = set()

    # Preferred path: explicit FATRETURN marker matched against the reverse chain.
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
                candidate = (fat_index, marker_index, True)
            elif _is_fat(fat_end):
                candidate = (marker_index, fat_index, False)
            else:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append({
                "return_segment": candidate[0],
                "paired_segment": candidate[1],
                "edge_count": len(_edge_list(segments[candidate[0]])),
                "fat_at_start": candidate[2],
                "source": "FATRETURN_MARKER",
            })

    # Authoritative fallback: infer Return directly from a reverse Pole Edge
    # chain pair. This is the important path for current Link data that has no
    # explicit FATRETURN marker.
    for fat_index, fat_segment in enumerate(segments):
        fat_start = _sequence_item(sequence_ids, fat_index)
        fat_end = _sequence_item(sequence_ids, fat_index + 1)
        fat_edges = _edge_list(fat_segment)
        if not fat_edges:
            continue
        if not _is_fat(fat_start) and not _is_fat(fat_end):
            continue

        for pair_index, pair_segment in enumerate(segments):
            if pair_index == fat_index:
                continue
            pair_edges = _edge_list(pair_segment)
            if not pair_edges or not _edge_chain_equal_reverse(fat_edges, pair_edges):
                continue

            # FAT must be the physical start of Return. If FAT is at the end of
            # this segment, the pair is the candidate that starts at FAT.
            if _is_fat(fat_start):
                candidate = (fat_index, pair_index, True)
            else:
                candidate = (pair_index, fat_index, False)

            if candidate in seen:
                continue

            # For a physical FAT-origin Return the chosen segment must itself
            # start at FAT. If the pair is the opposite orientation, reject it.
            return_index = candidate[0]
            return_start = _sequence_item(sequence_ids, return_index)
            if not _is_fat(return_start):
                continue

            seen.add(candidate)
            candidates.append({
                "return_segment": return_index,
                "paired_segment": candidate[1],
                "edge_count": len(_edge_list(segments[return_index])),
                "fat_at_start": True,
                "source": "REVERSE_CHAIN_INFERENCE",
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
        segment["_odn_return_pair_segment"] = None

    for candidate in _return_candidates(design, sequence_ids):
        idx = int(candidate["return_segment"])
        if not (0 <= idx < len(segments)):
            continue
        segment = segments[idx]
        paired = int(candidate["paired_segment"])
        start_item = _sequence_item(sequence_ids, idx)
        end_item = _sequence_item(sequence_ids, idx + 1)

        if _is_fat(start_item):
            # Authoritative Return definition:
            # FAT Pole -> exact reverse of Forward Pole Edge chain -> far Pole.
            segment["_odn_return_chain"] = True
            segment["_odn_return_start"] = True
            segment["_odn_return_end"] = True
            segment["_odn_return_role"] = "FAT_POLE_TO_SAME_POLE_EDGE_FAR_POLE"
            segment["_odn_return_pair_segment"] = paired
            # Both endpoints are physical Pole landings. The intermediate
            # geometry remains the ordinary offset Lane geometry.
            segment["_odn_special_start"] = True
            segment["_odn_special_end"] = True
        elif _is_fat(end_item):
            # Unsupported physical orientation: retain explicit metadata but do
            # not pretend the segment already starts at the FAT Pole.
            segment["_odn_return_chain"] = True
            segment["_odn_return_start"] = False
            segment["_odn_return_end"] = True
            segment["_odn_return_role"] = "FAT_AT_END_REVERSE_REQUIRED"
            segment["_odn_return_pair_segment"] = paired
            segment["_odn_special_start"] = False
            segment["_odn_special_end"] = True
        else:
            continue

        _core._log(
            f"[return-policy] {design.get('fdt', '')}/{design.get('link', '')}; "
            f"segment={idx}; paired={paired}; role={segment.get('_odn_return_role')}; "
            f"edge_count={candidate.get('edge_count', 0)}; source={candidate.get('source')}; "
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


def _ensure_return_lane_separation(designs, edge_crs, work, slots):
    """Return is the second cable on an identical route: keep it off slot 0.

    The paired Forward Cable retains Main Lane whenever it already owns slot 0.
    This guarantees that Return does not overlap the Forward cable while still
    allowing Main Lane to remain the first-choice lane for the route group.
    """
    changed = 0
    uses = _uses(designs, edge_crs, work)
    for design_index, design in enumerate(designs or []):
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            if not segment.get("_odn_return_chain"):
                continue
            paired_index = segment.get("_odn_return_pair_segment")
            if paired_index is None:
                continue
            return_edges = _edge_list(segment)
            if not return_edges:
                continue
            for edge_index, _ in enumerate(return_edges):
                return_key = (design_index, segment_index, edge_index)
                paired_key = (design_index, int(paired_index), len(return_edges) - 1 - edge_index)
                if return_key not in slots or paired_key not in slots:
                    continue
                if int(slots.get(return_key, 0)) != 0:
                    continue
                # If the paired Forward is already on Main Lane, keep it there.
                # Otherwise do not steal Main Lane from another cable; just move
                # Return to the nearest free relative lane on this Edge.
                used = {
                    int(value)
                    for other_key, value in slots.items()
                    if other_key[2] == edge_index
                    and other_key[1] == segment_index
                }
                paired_slot = int(slots.get(paired_key, 0))
                if paired_slot == 0:
                    side_hint = uses.get(return_key, {}).get("side_hint", 1)
                    chosen = _candidate_nonzero_slot(side_hint, used)
                    slots[return_key] = int(chosen)
                    changed += 1
                    _core._log(
                        f"[return-lane] design={design_index}; segment={segment_index}; edge={edge_index}; "
                        f"paired_segment={paired_index}; slot=0->{chosen}; "
                        f"rule=FORWARD_MAIN_RETURN_0_50M_OFFSET"
                    )
    return changed


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


def _return_anchored_geometry(segment, slot_by_edge, spacing, work, edge_crs, control):
    """Wrap the canonical geometry only to restore physical Return endpoints.

    The canonical builder already produces the correct continuous offset body.
    For a true Return, however, both ends are physical Pole landings. The
    wrapper therefore changes only the first/last anchor and leaves all
    intermediate Corner Core geometry untouched.
    """
    result = _ORIGINAL_GEOMETRY(segment, slot_by_edge, spacing, work, edge_crs, control)
    if not result or not segment.get("_odn_return_chain"):
        return result

    edges = _edge_list(segment)
    if not edges:
        return result
    nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)
    if len(nodes) != len(edges) + 1:
        return result

    slots = [int(slot_by_edge.get(index, 0)) for index in range(len(edges))]
    first_a = nodes[0]
    first_b = nodes[1]
    last_a = nodes[-2]
    last_b = nodes[-1]

    # Return starts exactly at FAT Pole, then takes off into its assigned lane.
    if segment.get("_odn_return_start"):
        takeoff = _core._takeoff_entry(first_a, first_b, slots[0], spacing, control)
        if result:
            result[0] = QgsPointXY(first_a)
        if len(result) == 1 and hypot(takeoff.x() - first_a.x(), takeoff.y() - first_a.y()) > 1e-7:
            result.append(QgsPointXY(takeoff))
        elif len(result) >= 2 and hypot(takeoff.x() - first_a.x(), takeoff.y() - first_a.y()) > 1e-7:
            result[1] = QgsPointXY(takeoff)

    # Return ends exactly at the opposite end Pole of the same Edge Chain.
    if segment.get("_odn_return_end") and result:
        result[-1] = QgsPointXY(last_b)

    _core._log(
        f"[return-geometry] role={segment.get('_odn_return_role','')}; "
        f"start={_core._corner_debug_point(first_a)}; end={_core._corner_debug_point(last_b)}; "
        f"slots={slots}; physical_endpoints=ON"
    )
    return result


def _patched_plan(designs, dc, edge_layer, spacing):
    edge_crs, work, ordered, routes, slots = _ORIGINAL_PLAN(designs, dc, edge_layer, spacing)
    changed_return = _ensure_return_lane_separation(designs, edge_crs, work, slots)
    changed_pole = _enforce_pole_landing_exclusion(designs, dc, edge_crs, work, slots)
    if changed_return or changed_pole:
        _core._log(
            f"[policy] return-lane-adjusted={changed_return}; pole-landing-adjusted={changed_pole}; "
            f"main-lane-priority=ON; ordinary-pole-exclusive=ON"
        )
    return edge_crs, work, ordered, routes, slots


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _core._set_flags = _patched_set_flags
    _core._plan = _patched_plan
    _core._geometry = _return_anchored_geometry
    _INSTALLED = True
    _core._log(
        "[policy] Return + Pole Landing rules installed; "
        "return=FAT_START/SAME_POLE_EDGE_CHAIN_REVERSED; "
        "return-end=FAR_POLE; return-spacing=0.50m; "
        "main-lane-first=ON; ordinary-pole-exclusive=ON"
    )


install()