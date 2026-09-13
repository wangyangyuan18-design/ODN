# -*- coding: utf-8 -*-
"""Small compatibility layer for the baseline Offset Core.

The baseline release owns the Corner Core and lane algorithm. This module only
adds one engineering condition: if an existing DC is already landed on a Pole,
that Pole's Main Lane is considered occupied for newly planned links. The
baseline lane/corner rules are otherwise left untouched.
"""

from qgis.core import QgsGeometry, QgsPointXY

from . import cable_offset_core as _core

_POLE_TOLERANCE_M = 0.05
_original_plan = _core._plan


def _dc_endpoint_geometries(dc, work):
    points = []
    if dc is None:
        return points
    for feature in dc.getFeatures():
        try:
            geometry = _core._transform_geometry(feature.geometry(), dc.crs(), work)
            if geometry.isEmpty():
                continue
            if geometry.isMultipart():
                for part in geometry.asMultiPolyline():
                    if part:
                        points.extend((QgsPointXY(part[0]), QgsPointXY(part[-1])))
            else:
                part = geometry.asPolyline()
                if part:
                    points.extend((QgsPointXY(part[0]), QgsPointXY(part[-1])))
        except Exception:
            continue
    return points


def _pole_has_existing_dc(point, endpoint_points):
    target = QgsGeometry.fromPointXY(QgsPointXY(point))
    return any(target.distance(QgsGeometry.fromPointXY(p)) <= _POLE_TOLERANCE_M for p in endpoint_points)


def _patch_slots_for_occupied_poles(designs, dc, edge_crs, work, slots):
    endpoint_points = _dc_endpoint_geometries(dc, work)
    if not endpoint_points:
        return 0

    changed = 0
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = [
                edge for raw in segment.get("edge_sequence", []) or []
                if (edge := _core._canonical_edge(raw))
            ]
            if len(edges) < 2:
                continue
            nodes = _core._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue

            for node_index in range(1, len(nodes) - 1):
                node = nodes[node_index]
                if not _pole_has_existing_dc(node, endpoint_points):
                    continue

                prev_key = (design_index, segment_index, node_index - 1)
                next_key = (design_index, segment_index, node_index)
                prev_slot = int(slots.get(prev_key, 0))
                next_slot = int(slots.get(next_key, 0))
                if prev_slot != 0 and next_slot != 0:
                    continue

                side = prev_slot if prev_slot != 0 else next_slot
                if side == 0:
                    candidates = []
                    for candidate in (1, -1):
                        conflict = False
                        for key in (prev_key, next_key):
                            if any(
                                other_key != key
                                and other_key[1:] == key[1:]
                                and int(other_slot) == candidate
                                for other_key, other_slot in slots.items()
                            ):
                                conflict = True
                                break
                        if not conflict:
                            candidates.append(candidate)
                    side = candidates[0] if candidates else 1

                if prev_slot == 0:
                    slots[prev_key] = int(side)
                    changed += 1
                if next_slot == 0:
                    slots[next_key] = int(side)
                    changed += 1
    return changed


def _patched_plan(designs, dc, edge_layer, spacing):
    edge_crs, work, ordered, routes, slots = _original_plan(designs, dc, edge_layer, spacing)
    changed = _patch_slots_for_occupied_poles(designs, dc, edge_crs, work, slots)
    if changed:
        _core._log(
            f"[pole-main-occupancy] existing DC occupies Pole Main Lane; "
            f"adjusted {changed} incident slot(s) back to baseline non-main Corner rule"
        )
    return edge_crs, work, ordered, routes, slots


_core._plan = _patched_plan
