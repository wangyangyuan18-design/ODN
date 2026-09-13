# -*- coding: utf-8 -*-
"""Unified final policy layer for the authoritative ODN Offset Core.

This is the single policy seam loaded after the direction/orientation seam.
It does not generate a second geometry engine. It enforces the documented
lane/topology invariants on the slot map returned by cable_offset_core and
keeps Return/FAT landing semantics explicit.
"""

from math import hypot

from qgis.core import QgsGeometry, QgsPointXY

from . import cable_offset_core as _core
from . import cable_offset_layout as _base


NODE_EPS_M = 0.05

_ORIGINAL_SET_FLAGS = _core._set_flags
_ORIGINAL_PLAN = _core._plan
_ORIGINAL_GEOMETRY = _core._geometry
_ORIGINAL_VALIDATE_POLE = _core._validate_pole_exclusivity
_INSTALLED = False


def _edge_list(segment):
    return [
        edge
        for raw in (segment.get("edge_sequence", []) or [])
        if (edge := _base._canonical_edge(raw))
    ]


def _kind(item):
    try:
        return _core._kind(item)
    except Exception:
        return ""


def _is_fat(item):
    return _kind(item) == "FAT"


def _is_return_marker(item):
    return _kind(item).startswith("FATRETURN")


def _sequence_item(sequence_ids, index):
    if 0 <= index < len(sequence_ids):
        return sequence_ids[index]
    return None


def _edge_chain_reverse(a_edges, b_edges):
    if len(a_edges) != len(b_edges):
        return False
    return all(a == b for a, b in zip(a_edges, reversed(b_edges)))


def _return_candidates(design):
    """Return only exact reverse-chain FAT-origin returns."""
    sequence_ids = design.get("sequence_ids", []) or []
    segments = design.get("segments", []) or []
    candidates = []
    seen = set()

    for marker_index, marker_segment in enumerate(segments):
        marker_item_a = _sequence_item(sequence_ids, marker_index)
        marker_item_b = _sequence_item(sequence_ids, marker_index + 1)
        if not (_is_return_marker(marker_item_a) or _is_return_marker(marker_item_b)):
            continue
        marker_edges = _edge_list(marker_segment)
        if not marker_edges:
            continue

        for pair_index, pair_segment in enumerate(segments):
            if pair_index == marker_index:
                continue
            pair_edges = _edge_list(pair_segment)
            if not pair_edges or not _edge_chain_reverse(pair_edges, marker_edges):
                continue

            pair_start = _sequence_item(sequence_ids, pair_index)
            pair_end = _sequence_item(sequence_ids, pair_index + 1)
            marker_start = _sequence_item(sequence_ids, marker_index)
            marker_end = _sequence_item(sequence_ids, marker_index + 1)

            if _is_fat(pair_start):
                candidate = (pair_index, marker_index)
            elif _is_fat(marker_start):
                candidate = (marker_index, pair_index)
            elif _is_fat(pair_end) and _is_return_marker(marker_start):
                candidate = (marker_index, pair_index)
            elif _is_fat(marker_end) and _is_return_marker(pair_start):
                candidate = (pair_index, marker_index)
            else:
                continue

            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append((candidate[0], candidate[1], "FATRETURN_MARKER"))

    for fat_index, fat_segment in enumerate(segments):
        fat_edges = _edge_list(fat_segment)
        if not fat_edges:
            continue
        fat_start = _sequence_item(sequence_ids, fat_index)
        fat_end = _sequence_item(sequence_ids, fat_index + 1)
        if not (_is_fat(fat_start) or _is_fat(fat_end)):
            continue

        for pair_index, pair_segment in enumerate(segments):
            if pair_index == fat_index:
                continue
            pair_edges = _edge_list(pair_segment)
            if not pair_edges or not _edge_chain_reverse(fat_edges, pair_edges):
                continue

            if _is_fat(fat_start):
                candidate = (fat_index, pair_index)
            elif _is_fat(fat_end):
                candidate = (pair_index, fat_index)
            else:
                continue

            return_index, paired_index = candidate
            if not _is_fat(_sequence_item(sequence_ids, return_index)):
                continue
            if candidate in seen:
                continue

            seen.add(candidate)
            candidates.append((return_index, paired_index, "REVERSE_CHAIN_INFERENCE"))

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

    for return_index, paired_index, source in _return_candidates(design):
        if not (0 <= return_index < len(segments)):
            continue

        segment = segments[return_index]
        start_item = _sequence_item(sequence_ids, return_index)
        end_item = _sequence_item(sequence_ids, return_index + 1)

        if not _is_fat(start_item):
            if _is_return_marker(start_item) or _is_return_marker(end_item):
                segment["_odn_return_marker"] = True
            continue

        segment["_odn_return_chain"] = True
        segment["_odn_return_start"] = True
        segment["_odn_return_end"] = True
        segment["_odn_return_role"] = "FAT_POLE_TO_SAME_POLE_EDGE_CHAIN_FAR_POLE"
        segment["_odn_return_pair_segment"] = int(paired_index)
        segment["_odn_special_start"] = True
        segment["_odn_special_end"] = True

        _core._log(
            f"[RETURN] {design.get('fdt','')}/{design.get('link','')}; "
            f"segment={return_index}; paired={paired_index}; "
            f"source={source}; rule=EXACT_REVERSE_CHAIN_FAT_START"
        )

    for index, segment in enumerate(segments):
        start_item = _sequence_item(sequence_ids, index)
        end_item = _sequence_item(sequence_ids, index + 1)
        if _is_return_marker(start_item) and not segment.get("_odn_return_start"):
            segment["_odn_return_marker"] = True
            segment["_odn_special_start"] = False
        if _is_return_marker(end_item) and not segment.get("_odn_return_end"):
            segment["_odn_return_marker"] = True
            segment["_odn_special_end"] = False


def _set_flags(designs):
    _ORIGINAL_SET_FLAGS(designs)
    for design in designs or []:
        _apply_return_semantics(design)


def _external_endpoint_points(dc, designs, work):
    try:
        memory = _core._occupancy(dc, designs, work)
    except Exception:
        return []

    points = []
    for feature in memory.getFeatures():
        try:
            geometry = feature.geometry()
            if geometry.isEmpty():
                continue
            parts = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
            for part in parts:
                if part:
                    points.append(QgsPointXY(part[0]))
                    points.append(QgsPointXY(part[-1]))
        except Exception:
            continue
    return points


def _point_occupied(point, endpoints):
    return any(
        hypot(point.x() - other.x(), point.y() - other.y()) <= NODE_EPS_M
        for other in endpoints
    )


def _node_key(point):
    return round(float(point.x()), 7), round(float(point.y()), 7)


def _endpoint_landing_owners(designs, edge_crs, work):
    """Declared segment endpoints are cable landings; internal route nodes are not."""
    owners = {}
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
            if start_item is not None:
                owners.setdefault(_node_key(nodes[0]), []).append(
                    (design_index, segment_index, _kind(start_item))
                )
            if end_item is not None:
                owners.setdefault(_node_key(nodes[-1]), []).append(
                    (design_index, segment_index, _kind(end_item))
                )
    return owners


def _use_endpoint_blocked(use, designs, owners, external_points, edge_crs, work):
    """slot 0 is blocked only by a genuine cable landing conflict."""
    design = designs[use.design_index]
    segment = design["segments"][use.segment_index]
    sequence_ids = design.get("sequence_ids", []) or []
    edges = _edge_list(segment)
    if not edges:
        return False

    nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)
    if len(nodes) != len(edges) + 1:
        return False

    endpoint_keys = []
    if use.edge_index == 0 and use.segment_index < len(sequence_ids):
        endpoint_keys.append((_node_key(nodes[0]), "START"))
    if use.edge_index == len(edges) - 1 and use.segment_index + 1 < len(sequence_ids):
        endpoint_keys.append((_node_key(nodes[-1]), "END"))

    for key, endpoint_side in endpoint_keys:
        point = QgsPointXY(*key)
        if _point_occupied(point, external_points):
            return True
        other_design = any(owner[0] != use.design_index for owner in owners.get(key, []))
        if not other_design:
            continue

        # Only the FAT-origin point of a true Return is a deliberate shared
        # landing. The far Pole remains ordinary/exclusive.
        is_return_start = (
            bool(segment.get("_odn_return_chain"))
            and endpoint_side == "START"
            and use.segment_index < len(sequence_ids)
            and _is_fat(_sequence_item(sequence_ids, use.segment_index))
        )
        if not is_return_start:
            return True
    return False


def _sign(value):
    return 1 if int(value) > 0 else -1


def _route_rank(routes, design_index):
    route = routes.get(design_index, {})
    return (
        -float(route.get("priority_score", 0.0)),
        -float(route.get("longest_directional_run", 0.0)),
        -float(route.get("route_length", 0.0)),
        int(design_index),
    )


def _normalize_lanes(designs, dc, edge_layer, spacing, result):
    """Enforce Main owner, side, order and compactness globally."""
    if not result or len(result) < 5:
        return result

    edge_crs, work, ordered, routes, slots = result
    pending, _ = _core._collect_uses(designs, edge_crs, work)
    by_edge = {}
    for use in pending:
        by_edge.setdefault(use.edge_key, []).append(use)

    occupancy = _core._occupancy(dc, designs, work)
    spatial_index, geometries = _base._build_existing_index(occupancy, work)

    def reserved(edge):
        a, b = _base._edge_points(edge)
        a = _core._tp(a, edge_crs, work)
        b = _core._tp(b, edge_crs, work)
        return set(_base._existing_slot_occupancy(
            QgsGeometry.fromPolylineXY([a, b]), spacing, spatial_index, geometries
        ))

    owners = _endpoint_landing_owners(designs, edge_crs, work)
    external_points = _external_endpoint_points(dc, designs, work)
    route_order = {
        design_index: order
        for order, design_index in enumerate(sorted(routes, key=lambda idx: _route_rank(routes, idx)))
    }

    edge_dependencies = {}
    for edge, uses in by_edge.items():
        dependencies = set()
        for use in uses:
            if use.prev_edge_key is not None and use.prev_edge_key in by_edge:
                if use.prev_edge_key != edge:
                    dependencies.add(use.prev_edge_key)
        edge_dependencies[edge] = dependencies

    remaining_edges = set(by_edge)
    processed_edges = set()
    edge_order = []
    while remaining_edges:
        ready = [
            edge for edge in remaining_edges
            if edge_dependencies.get(edge, set()).issubset(processed_edges)
        ]
        if not ready:
            ready = list(remaining_edges)
        chosen_edge = min(
            ready,
            key=lambda edge: min(
                (
                    route_order.get(use.design_index, 10**9),
                    use.segment_index,
                    use.edge_index,
                )
                for use in by_edge[edge]
            ),
        )
        edge_order.append(chosen_edge)
        remaining_edges.remove(chosen_edge)
        processed_edges.add(chosen_edge)

    main_owner = {}
    side_memory = {}
    order_memory = {}
    assigned_slots = {}
    main_switches = 0
    side_crosses = 0
    gaps_compacted = 0
    blocked_main = 0
    preserved = 0

    for edge in edge_order:
        uses = by_edge[edge]
        reserved_slots = reserved(edge)
        used = set(reserved_slots)
        previous_by_use = {}

        for use in uses:
            key = (use.design_index, use.segment_index, use.edge_index)
            previous = assigned_slots.get((use.design_index, use.segment_index, use.edge_index - 1))
            previous_by_use[key] = previous
            segment_key = (use.design_index, use.segment_index)
            if previous not in (None, 0):
                side_memory.setdefault(segment_key, _sign(previous))
                order_memory.setdefault((segment_key, use.design_index), abs(int(previous)))

        candidates = [
            use for use in uses
            if not _use_endpoint_blocked(use, designs, owners, external_points, edge_crs, work)
        ]
        if 0 in reserved_slots:
            candidates = []

        primary = None
        if candidates:
            previous_zero = [
                use for use in candidates
                if previous_by_use[(use.design_index, use.segment_index, use.edge_index)] == 0
            ]
            if previous_zero:
                primary = min(previous_zero, key=lambda use: (
                    route_order.get(use.design_index, 10**9), use.segment_index, use.edge_index
                ))
            else:
                established = [
                    use for use in candidates
                    if main_owner.get((use.design_index, use.segment_index)) == use.design_index
                ]
                if established:
                    primary = min(established, key=lambda use: (
                        route_order.get(use.design_index, 10**9), use.segment_index, use.edge_index
                    ))
                else:
                    previous_planned = [
                        use for use in candidates
                        if int(slots.get((use.design_index, use.segment_index, use.edge_index), 999999)) == 0
                    ]
                    if previous_planned:
                        primary = min(previous_planned, key=lambda use: (
                            route_order.get(use.design_index, 10**9), use.segment_index, use.edge_index
                        ))
                    else:
                        primary = min(candidates, key=lambda use: (
                            route_order.get(use.design_index, 10**9), use.segment_index, use.edge_index
                        ))

        if primary is not None:
            seg_key = (primary.design_index, primary.segment_index)
            previous_owner = main_owner.get(seg_key)
            assigned_key = (primary.design_index, primary.segment_index, primary.edge_index)
            assigned_slots[assigned_key] = 0
            used.add(0)
            if previous_owner is not None and previous_owner != primary.design_index:
                main_switches += 1
            if previous_owner is None:
                main_owner[seg_key] = primary.design_index

        if primary is None and uses and 0 not in reserved_slots:
            blocked_main += 1

        non_main = [
            use for use in uses
            if (use.design_index, use.segment_index, use.edge_index) not in assigned_slots
        ]
        side_groups = {1: [], -1: []}

        for use in non_main:
            key = (use.design_index, use.segment_index, use.edge_index)
            previous = previous_by_use.get(key)
            segment_key = (use.design_index, use.segment_index)
            if previous not in (None, 0):
                side = _sign(previous)
            elif side_memory.get(segment_key) in (1, -1):
                side = side_memory[segment_key]
            else:
                old = int(slots.get(key, 0))
                side = _sign(old) if old != 0 else (1 if int(use.side_hint or 0) >= 0 else -1)
            side_groups[side].append((use, previous))

        for side in (1, -1):
            members = side_groups[side]

            def member_key(item):
                use, previous = item
                segment_key = (use.design_index, use.segment_index)
                previous_mag = (
                    abs(int(previous))
                    if previous not in (None, 0) and _sign(previous) == side
                    else 10**6
                )
                historical = order_memory.get((segment_key, use.design_index), 10**6)
                had_history = previous_mag < 10**6 or historical < 10**6
                return (
                    0 if had_history else 1,
                    min(previous_mag, historical),
                    route_order.get(use.design_index, 10**9),
                    use.segment_index,
                    use.edge_index,
                )

            members.sort(key=member_key)
            old_mags = [
                abs(int(previous))
                for _, previous in members
                if previous not in (None, 0) and _sign(previous) == side
            ]
            if old_mags and sorted(set(old_mags)) != list(range(1, len(set(old_mags)) + 1)):
                gaps_compacted += 1

            magnitude = 1
            for use, previous in members:
                while side * magnitude in used:
                    magnitude += 1
                chosen = side * magnitude
                key = (use.design_index, use.segment_index, use.edge_index)
                assigned_slots[key] = chosen
                used.add(chosen)
                segment_key = (use.design_index, use.segment_index)
                side_memory[segment_key] = side
                order_memory[(segment_key, use.design_index)] = magnitude
                if previous not in (None, 0):
                    if _sign(previous) != side:
                        side_crosses += 1
                    elif previous == chosen:
                        preserved += 1
                magnitude += 1

        for use in uses:
            key = (use.design_index, use.segment_index, use.edge_index)
            if key in assigned_slots:
                continue
            old = int(slots.get(key, 0))
            segment = designs[use.design_index]["segments"][use.segment_index]
            if old == 0:
                continue
            if _use_endpoint_blocked(use, designs, owners, external_points, edge_crs, work) or segment.get("_odn_return_chain"):
                side = _sign(old)
                magnitude = abs(old)
                while side * magnitude in used:
                    magnitude += 1
                assigned_slots[key] = side * magnitude
                used.add(side * magnitude)
                side_memory.setdefault((use.design_index, use.segment_index), side)

        for use in uses:
            key = (use.design_index, use.segment_index, use.edge_index)
            chosen = int(assigned_slots.get(key, 0))
            slots[key] = chosen
            use.slot = chosen

        planned_zero = sum(
            1 for use in uses
            if int(assigned_slots.get((use.design_index, use.segment_index, use.edge_index), 0)) == 0
        )
        if 0 not in reserved_slots and uses and primary is not None and planned_zero != 1:
            raise RuntimeError(f"ODN lane policy: edge {edge} must have exactly one free Main Lane")
        if 0 in reserved_slots and planned_zero:
            raise RuntimeError(f"ODN lane policy: reserved Main Lane was assigned on edge {edge}")

    _core._log(
        f"[LANE-SUMMARY] edges={len(edge_order)}; main_switches={main_switches}; "
        f"side_crosses={side_crosses}; gaps_compacted={gaps_compacted}; "
        f"blocked_main={blocked_main}; preserved={preserved}; rule=GLOBAL_ROUTE_STATE"
    )
    return (edge_crs, work, ordered, routes, slots)


def _preplan_validate_pole_exclusivity(designs, edge_crs, work):
    """Defer Pole landing validation until after Lane planning."""
    return None


def _validate_final_pole_landings(designs, dc, edge_crs, work, slots):
    owners = {}
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
            first_slot = int(slots.get((design_index, segment_index, 0), 0))
            last_slot = int(slots.get((design_index, segment_index, len(edges) - 1), 0))

            if start_item is not None and (first_slot == 0 or segment.get("_odn_special_start")):
                owners.setdefault(_node_key(nodes[0]), []).append(
                    (design_index, segment_index, "START", _kind(start_item))
                )
            if end_item is not None and (last_slot == 0 or segment.get("_odn_special_end")):
                owners.setdefault(_node_key(nodes[-1]), []).append(
                    (design_index, segment_index, "END", _kind(end_item))
                )

    external_points = _external_endpoint_points(dc, designs, work)
    for node_key, items in owners.items():
        if not external_points or not _point_occupied(QgsPointXY(*node_key), external_points):
            continue
        kinds = [item[3] for item in items]
        if kinds and all(_core._shared_node(kind) or str(kind).startswith("FATRETURN") for kind in kinds):
            continue
        raise RuntimeError(f"Offset Policy: final Cable landing conflicts with existing DC at node={node_key}")

    conflicts = []
    for node_key, items in owners.items():
        independent = sorted({item[0] for item in items})
        if len(independent) <= 1:
            continue
        kinds = [item[3] for item in items]
        if kinds and all(_core._shared_node(kind) or str(kind).startswith("FATRETURN") for kind in kinds):
            continue
        conflicts.append((node_key, independent))

    if conflicts:
        detail = " | ".join(
            f"node={key}; links={[designs[i].get('_link_id', designs[i].get('link', i)) for i in ids]}"
            for key, ids in conflicts[:20]
        )
        raise RuntimeError("Offset Policy: ordinary Pole has multiple final Cable landings: " + detail)


def _patched_plan(designs, dc, edge_layer, spacing):
    result = _ORIGINAL_PLAN(designs, dc, edge_layer, spacing)
    normalized = _normalize_lanes(designs, dc, edge_layer, spacing, result)
    edge_crs, work, ordered, routes, slots = normalized
    _validate_final_pole_landings(designs, dc, edge_crs, work, slots)
    return normalized


def _return_anchored_geometry(segment, slot_by_edge, spacing, work, edge_crs, control):
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
    first_a, first_b = nodes[0], nodes[1]
    last_b = nodes[-1]

    if segment.get("_odn_return_start") and result:
        takeoff = _core._takeoff_entry(first_a, first_b, slots[0], spacing, control)
        result[0] = QgsPointXY(first_a)
        if len(result) == 1 and hypot(takeoff.x() - first_a.x(), takeoff.y() - first_a.y()) > 1e-7:
            result.append(QgsPointXY(takeoff))
        elif len(result) >= 2 and hypot(takeoff.x() - first_a.x(), takeoff.y() - first_a.y()) > 1e-7:
            result[1] = QgsPointXY(takeoff)

    if segment.get("_odn_return_end") and result:
        result[-1] = QgsPointXY(last_b)

    _core._log(
        f"[RETURN-GEOM] role={segment.get('_odn_return_role','')}; "
        f"start={_core._corner_debug_point(first_a)}; end={_core._corner_debug_point(last_b)}; "
        f"slots={slots}; endpoints=PHYSICAL"
    )
    return result


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _core._set_flags = _set_flags
    _core._validate_pole_exclusivity = _preplan_validate_pole_exclusivity
    _core._plan = _patched_plan
    _core._geometry = _return_anchored_geometry
    _INSTALLED = True
    _core._log(
        "[POLICY] unified-lane-state installed; main=stable-owner/free-slot0; "
        "side=sticky-sign; order=group-outside; compact=same-side-only; return=topology-derived"
    )


install()
