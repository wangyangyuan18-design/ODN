# -*- coding: utf-8 -*-
"""Global Main Lane guard.

This seam covers both the legacy cable_offset_layout allocator and the
authoritative Cable Offset Core. Rule: when physical Main Lane slot 0 is not
reserved by an existing Distribution Cable, one cable on that Pole Edge must
occupy slot 0 before any side lane is used.
"""

from . import cable_offset_core as _core
from . import cable_offset_layout as _base


_ORIGINAL_ASSIGN = _base._assign_slots
_ORIGINAL_PLAN = _core._plan
_INSTALLED = False


def _assign_slots_main_first(edge_users, edge_reserved, previous_slots):
    assigned = {}
    used = set(edge_reserved or set())
    users = list(edge_users or [])

    # Never let a stale previous side-lane assignment bypass an empty Main Lane.
    for use in users:
        use.slot = None

    primary = None
    if 0 not in used and users:
        previous_zero = [u for u in users if previous_slots.get(u.design_index) == 0]
        if previous_zero:
            primary = min(previous_zero, key=lambda u: (u.design_index, u.segment_index, u.edge_index))
        else:
            primary = min(users, key=lambda u: (u.design_index, u.segment_index, u.edge_index))

    if primary is not None:
        primary.slot = 0
        used.add(0)
        assigned[primary.design_index] = 0

    # Preserve non-zero continuity where possible.
    rest = [u for u in users if u is not primary]
    rest.sort(key=lambda u: (
        0 if previous_slots.get(u.design_index) not in (None, 0) else 1,
        abs(int(previous_slots.get(u.design_index, 10**9))) if previous_slots.get(u.design_index) not in (None, 0) else 10**9,
        u.design_index, u.segment_index, u.edge_index,
    ))

    for use in rest:
        previous = previous_slots.get(use.design_index)
        if previous not in (None, 0) and previous not in used:
            use.slot = int(previous)
            used.add(use.slot)
            assigned[use.design_index] = use.slot

    for use in rest:
        if use.slot is not None:
            continue
        side = 1 if use.side_hint >= 0 else -1
        magnitude = 1
        while side * magnitude in used:
            magnitude += 1
        use.slot = side * magnitude
        used.add(use.slot)
        assigned[use.design_index] = use.slot

    return assigned


def _repair_core_plan(designs, dc, edge_layer, spacing, result):
    """Defensive final repair for Core output; changes only edges missing free slot 0."""
    if not result or len(result) < 5:
        return result
    slots = result[-1]
    try:
        edge_crs = edge_layer.crs()
        work = _core._metric_crs(edge_layer, dc)
        occupancy = _core._occupancy(dc, designs, work)
        spatial_index, geometries = _base._build_existing_index(occupancy, work)
    except Exception:
        return result

    uses_by_edge = {}
    for di, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        for si, segment in enumerate(design.get("segments", []) or []):
            edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
            for ei, edge in enumerate(edges):
                uses_by_edge.setdefault(edge, []).append((di, si, ei))

    repaired = 0
    for edge, uses in uses_by_edge.items():
        a, b = _base._edge_points(edge)
        a = _core._tp(a, edge_crs, work)
        b = _core._tp(b, edge_crs, work)
        reserved = _base._existing_slot_occupancy(
            _base.QgsGeometry.fromPolylineXY([a, b]), spacing, spatial_index, geometries
        )
        keys = [(di, si, ei) for di, si, ei in uses]
        values = [int(slots.get(key, 0)) for key in keys]
        if 0 in reserved or 0 in values:
            continue
        if not keys:
            continue

        # Choose the route currently closest to Main Lane as the primary.
        primary_key = min(keys, key=lambda key: (abs(int(slots.get(key, 0))), key[0], key[1], key[2]))
        old_primary = int(slots.get(primary_key, 0))
        slots[primary_key] = 0
        repaired += 1
        if hasattr(_core, "_log"):
            _core._log(
                f"[MAIN-GUARD] edge={edge}; use={primary_key[0]}/{primary_key[1]}/{primary_key[2]}; "
                f"lane={old_primary}->0; reserved=NONE"
            )

        # Repack remaining same-side lanes to remove the vacated gap.
        remaining = [key for key in keys if key != primary_key]
        for side in (1, -1):
            members = [key for key in remaining if int(slots.get(key, 0)) * side > 0]
            members.sort(key=lambda key: (abs(int(slots.get(key, 0))), key[0], key[1], key[2]))
            for magnitude, key in enumerate(members, start=1):
                slots[key] = side * magnitude

    if repaired:
        try:
            for key, value in slots.items():
                # The public result only needs the slot map; downstream geometry reads it.
                pass
        except Exception:
            pass
    return result


def _plan_guarded(designs, dc, edge_layer, spacing):
    result = _ORIGINAL_PLAN(designs, dc, edge_layer, spacing)
    try:
        result = _repair_core_plan(designs, dc, edge_layer, spacing, result)
    except Exception as exc:
        _core._log(f"[MAIN-GUARD-WARN] {type(exc).__name__}: {exc}")
    return result


if not _INSTALLED:
    _base._assign_slots = _assign_slots_main_first
    _core._plan = _plan_guarded
    _INSTALLED = True
