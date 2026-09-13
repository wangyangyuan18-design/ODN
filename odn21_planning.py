# -*- coding: utf-8 -*-
"""ODN 2.1 automatic BB / SFC Closure planning.

This module is deliberately outside Link Design and Offset Core.  It wraps
LinkDesignController._make_design so that a saved ODN 2.1 design is expanded
with real BB and SFC Closure nodes before the normal save/write pipeline runs.

Rules:
- Return cable = repeated Pole Edge traversed out and back; one-way edge length
  multiplied by 2.  BB is required only when return length > 100 m.
- BB is placed on a branch-node candidate inside the detected return section;
  candidates are evaluated by real Pole Edge route distance to the upstream
  FAT and the next downstream ODN nodes. If there is no branch candidate, the
  defined return endpoint is used.
- Distribution / pre-link cable: every individual ODN-node-to-ODN-node segment
  must be <= 455 m. When a segment would exceed 455 m, SFC Closure is placed at
  the last real Pole node whose accumulated distance from the segment start is
  still <= 455 m. The remainder is then measured as a new segment.
- BB and SFC layers are real project nodes; they enter sequence_ids so the
  existing Offset Core can treat them as explicit special endpoints.
"""

from math import inf

from qgis.core import QgsFeature, QgsGeometry, QgsMessageLog, QgsPointXY, Qgis

from . import odn_project_context as context
from .odn_project_routing import OdnProjectRouteEngine, feature_name

LOG_TAG = "ODN_Tools_Pro / ODN 2.1 Planning"
RETURN_LIMIT_M = 100.0
DC_LIMIT_M = 455.0
POINT_TOLERANCE_M = 0.05
MAX_ITERATIONS = 64


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), LOG_TAG, level)
    except Exception:
        pass


def _payload(controller):
    try:
        return controller.fresh_payload(controller)  # type: ignore[attr-defined]
    except Exception:
        try:
            from .link_design_core import fresh_payload
            return fresh_payload(controller)
        except Exception:
            return context.current_payload() or {}


def _is_21(payload):
    version = str(((payload or {}).get("project") or {}).get("odn_version", "")).strip().lower()
    return version in {"2.1", "odn 2.1", "odn2.1", "odn_2.1"}


def _enabled(payload, role):
    if not _is_21(payload):
        return False
    project = payload.get("project") or {}
    node_types = set(project.get("node_types") or [])
    if role in node_types:
        return True
    registry = payload.get("layer_registry") or {}
    entry = registry.get(role) or {}
    return bool(entry.get("layer_id"))


def _layer(payload, role):
    return context.project_layer(payload, role)


def _point_distance_m(engine, a, b):
    if a is None or b is None:
        return inf
    try:
        return float(engine._point_distance(QgsPointXY(a), QgsPointXY(b)))
    except Exception:
        try:
            return float(QgsGeometry.fromPointXY(QgsPointXY(a)).distance(QgsGeometry.fromPointXY(QgsPointXY(b))))
        except Exception:
            return inf


def _same_point(engine, a, b):
    return _point_distance_m(engine, a, b) <= POINT_TOLERANCE_M


def _node_degree(engine, node):
    try:
        return len(engine.graph.get(node, []))
    except Exception:
        return 0


def _edge_key(edge):
    try:
        return (int(edge[0]), tuple(edge[1]))
    except Exception:
        return str(edge)


def _edge_length(engine, edge):
    try:
        return float(engine.edge_len[_edge_key(edge)])
    except Exception:
        try:
            return float(engine.edge_len.get(edge, 0.0))
        except Exception:
            return 0.0


def _return_loops(segment):
    """Find repeated physical-edge traversals and their return endpoints."""
    edges = list(segment.get("edge_sequence") or [])
    nodes = list(segment.get("graph_nodes") or [])
    first_seen = {}
    loops = []
    for edge_index, edge in enumerate(edges):
        key = _edge_key(edge)
        if key in first_seen:
            first_index = first_seen[key]
            if first_index + 1 < len(nodes):
                # For A -> B -> A, first_index edge ends at B.  This is the
                # explicitly defined return endpoint requested for BB placement.
                return_endpoint = nodes[first_index + 1]
            else:
                return_endpoint = nodes[first_index]
            loops.append({
                "edge": edge,
                "edge_key": key,
                "first_index": first_index,
                "repeat_index": edge_index,
                "return_endpoint": return_endpoint,
                "one_way": _edge_length_by_index(engine=None, segment=segment, edge=edge),
            })
        else:
            first_seen[key] = edge_index
    return loops


def _edge_length_by_index(engine, segment, edge):
    # Placeholder-compatible helper; real callers inject engine below.
    if engine is not None:
        return _edge_length(engine, edge)
    return 0.0


def _loop_candidates(engine, segment, loop):
    nodes = list(segment.get("graph_nodes") or [])
    start = int(loop["first_index"]) + 1
    end = int(loop["repeat_index"]) + 1
    raw = nodes[start:end + 1]
    seen = set()
    candidates = []
    for node in raw:
        if node in seen:
            continue
        seen.add(node)
        if _node_degree(engine, node) >= 3:
            candidates.append(node)
    if not candidates:
        candidates = [loop["return_endpoint"]]
    return candidates


def _node_distance_from_feature(engine, typ, fid, graph_node):
    attached = engine.attach(typ, int(fid))
    if not attached:
        return inf
    graph_nodes, _, pole_distance, _ = engine._route_with_engineering_preference(
        attached["node"], graph_node
    )
    if graph_nodes is None:
        return inf
    return float(attached["connector"]) + float(pole_distance)


def _choose_bb_node(engine, segment, sequence, segment_index, loop):
    candidates = _loop_candidates(engine, segment, loop)
    upstream = sequence[segment_index]
    downstream = sequence[segment_index + 1]
    downstream2 = sequence[segment_index + 2] if segment_index + 2 < len(sequence) else None

    scored = []
    for node in candidates:
        score = _node_distance_from_feature(engine, upstream[0], upstream[1], node)
        score += _node_distance_from_feature(engine, downstream[0], downstream[1], node)
        if downstream2 is not None:
            score += _node_distance_from_feature(engine, downstream2[0], downstream2[1], node)
        endpoint_distance = _point_distance_m(engine, engine.node_points[node], loop["return_endpoint"])
        scored.append((score, endpoint_distance, node))

    scored.sort(key=lambda item: (item[0], item[1]))
    selected = scored[0][2] if scored else loop["return_endpoint"]
    _log(
        f"[BB SELECT] segment={segment_index + 1}; candidates={len(candidates)}; "
        f"selected={selected}; degree={_node_degree(engine, selected)}; "
        f"return_endpoint={loop['return_endpoint']}"
    )
    return selected


def _find_existing_node(layer, engine, point):
    if layer is None:
        return None
    for feature in layer.getFeatures():
        geometry = feature.geometry()
        if geometry.isEmpty():
            continue
        try:
            candidate = QgsPointXY(geometry.centroid().asPoint())
            candidate = engine._point_in_edge_crs({"point": candidate, "layer": layer})
        except Exception:
            continue
        if _same_point(engine, candidate, point):
            return int(feature.id())
    return None


def _ensure_editable(layer):
    if layer is None:
        raise RuntimeError("ODN 2.1 节点图层未绑定。")
    if not layer.isEditable() and not layer.startEditing():
        raise RuntimeError(f"无法进入 {layer.name()} 编辑状态。")


def _set_name(payload, layer, role, feature, name):
    mapping = (payload.get("field_registry") or {}).get(role, {})
    field_name = mapping.get("名称")
    if field_name and field_name in layer.fields().names():
        feature[field_name] = name
        return
    for candidate in ("Name", "NAME", "name"):
        if candidate in layer.fields().names():
            feature[candidate] = name
            return


def _create_node(payload, engine, role, point, name):
    layer = _layer(payload, role)
    if layer is None:
        raise RuntimeError(f"ODN 2.1 已启用 {role}，但项目没有绑定 {role} 图层。")
    existing = _find_existing_node(layer, engine, point)
    if existing is not None:
        return existing
    _ensure_editable(layer)
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point)))
    _set_name(payload, layer, role, feature, name)
    if not layer.addFeature(feature):
        raise RuntimeError(f"新增 {role} 要素失败。")
    layer.updateExtents()
    layer.triggerRepaint()
    return int(feature.id())


def _label(payload, role, fid):
    layer = _layer(payload, role)
    if layer is None:
        return f"{role}{fid}"
    feature = layer.getFeature(int(fid))
    if not feature.isValid():
        return f"{role}{fid}"
    try:
        return feature_name(payload, layer, role, feature)
    except Exception:
        return f"{role}{fid}"


def _new_name(design, role, serial):
    return f"{role.replace(' ', '_')}-{design.get('fdt', '')}-{design.get('link', '')}-{serial:02d}"


def _route(controller, engine, first, second):
    controller._engine = engine
    route = controller._route(first, second)
    return route


def _segment_from_route(route, first, second):
    return {
        "from": route.get("from_label", first[2]),
        "to": route.get("to_label", second[2]),
        "distance": round(float(route.get("distance", 0.0) or 0.0), 3),
        "pole_edge_distance": round(float(route.get("pole_edge_distance", 0.0) or 0.0), 3),
        "route_score": round(float(route.get("route_score", 0.0) or 0.0), 3),
        "start_connector": round(float(route.get("start_connector", 0.0) or 0.0), 3),
        "end_connector": round(float(route.get("end_connector", 0.0) or 0.0), 3),
        "edge_sequence": list(route.get("edge_sequence", []) or []),
        "graph_nodes": list(route.get("graph_nodes", []) or []),
        "edge_count": len(route.get("edge_sequence", []) or []),
        "points": [[float(p.x()), float(p.y())] for p in route.get("points", [])],
        "zero_length": bool(route.get("zero_length", False)),
    }


def _rebuild(controller, sequence, engine):
    segments = []
    for first, second in zip(sequence[:-1], sequence[1:]):
        route = _route(controller, engine, first, second)
        if route is None:
            raise RuntimeError(f"{first[2]} → {second[2]} 无法沿当前 Pole Edge 建立完整路线。")
        segments.append(_segment_from_route(route, first, second))
    return segments


def _last_pole_at_limit(segment, engine, limit):
    nodes = list(segment.get("graph_nodes") or [])
    edges = list(segment.get("edge_sequence") or [])
    if len(nodes) < 2 or len(edges) != len(nodes) - 1:
        return None
    start_connector = float(segment.get("start_connector", 0.0) or 0.0)
    cumulative = start_connector
    candidate = None
    for index, edge in enumerate(edges):
        nxt = cumulative + _edge_length(engine, edge)
        if nxt <= limit + 1e-6:
            candidate = {
                "node_index": index + 1,
                "distance": nxt,
                "node": nodes[index + 1],
            }
            cumulative = nxt
        else:
            break
    return candidate


def _insertions_for_bb(controller, payload, design, sequence, segments, engine, serial_start=1):
    if not _enabled(payload, "BB"):
        return [], serial_start
    insertions = {}
    seen_points = set()
    serial = serial_start
    for segment_index, segment in enumerate(segments):
        loops = []
        edges = list(segment.get("edge_sequence") or [])
        first_seen = {}
        for edge_index, edge in enumerate(edges):
            key = _edge_key(edge)
            if key in first_seen:
                first_index = first_seen[key]
                one_way = _edge_length(engine, edge)
                return_length = one_way * 2.0
                loops.append((first_index, edge_index, one_way, return_length))
            else:
                first_seen[key] = edge_index
        for first_index, repeat_index, one_way, return_length in loops:
            _log(
                f"[BB CHECK] design={design.get('fdt')}/{design.get('link')}; "
                f"segment={segment_index + 1}; one_way={one_way:.3f}m; "
                f"return={return_length:.3f}m; limit={RETURN_LIMIT_M:.3f}m"
            )
            if return_length <= RETURN_LIMIT_M + 1e-6:
                continue
            loop = {
                "first_index": first_index,
                "repeat_index": repeat_index,
                "return_endpoint": (segment.get("graph_nodes") or [])[first_index + 1],
            }
            node = _choose_bb_node(engine, segment, sequence, segment_index, loop)
            point = engine.node_points[node]
            point_key = (round(float(point.x()), 6), round(float(point.y()), 6))
            if point_key in seen_points:
                continue
            seen_points.add(point_key)
            name = _new_name(design, "BB", serial)
            fid = _create_node(payload, engine, "BB", point, name)
            label = _label(payload, "BB", fid)
            insertions.setdefault(segment_index, []).append((first_index + 1, ("BB", fid, label)))
            serial += 1
            _log(
                f"[BB INSERT] {design.get('fdt')}/{design.get('link')}; "
                f"segment={segment_index + 1}; node={node}; point=({float(point.x()):.3f},{float(point.y()):.3f}); label={label}"
            )
    result = []
    for segment_index in sorted(insertions):
        result.extend([])
    return insertions, serial


def _insertions_for_sfc(payload, design, sequence, segments, engine, serial_start=1):
    if not _enabled(payload, "SFC CL"):
        return {}, serial_start
    insertions = {}
    serial = serial_start
    for segment_index, segment in enumerate(segments):
        length = float(segment.get("distance", 0.0) or 0.0)
        if length <= DC_LIMIT_M + 1e-6:
            continue
        candidate = _last_pole_at_limit(segment, engine, DC_LIMIT_M)
        if candidate is None:
            _log(
                f"[SFC CHECK] {design.get('fdt')}/{design.get('link')}; "
                f"segment={segment_index + 1}; length={length:.3f}m; limit={DC_LIMIT_M:.3f}m; "
                f"NO POLE NODE <= LIMIT",
                Qgis.Warning,
            )
            raise RuntimeError(
                f"{design.get('fdt')}/{design.get('link')} 第 {segment_index + 1} 段超过 {DC_LIMIT_M:g}m，"
                "但当前 Pole Edge 上找不到不超过限制的可放置 SFC CL 节点。"
            )
        node = candidate["node"]
        point = engine.node_points[node]
        name = _new_name(design, "SFC_CL", serial)
        fid = _create_node(payload, engine, "SFC CL", point, name)
        label = _label(payload, "SFC CL", fid)
        insertions.setdefault(segment_index, []).append((candidate["node_index"], ("SFC CL", fid, label)))
        serial += 1
        _log(
            f"[SFC INSERT] {design.get('fdt')}/{design.get('link')}; segment={segment_index + 1}; "
            f"length={length:.3f}m; limit={DC_LIMIT_M:.3f}m; selected_distance={candidate['distance']:.3f}m; "
            f"node={node}; label={label}"
        )
    return insertions, serial


def _apply_insertions(sequence, insertions):
    result = []
    for index, item in enumerate(sequence):
        result.append(item)
        for _, node_item in sorted(insertions.get(index, []), key=lambda x: x[0]):
            if not result or result[-1][:2] != node_item[:2]:
                result.append(node_item)
    return result


def plan_design(controller, design):
    payload = _payload(controller)
    if not _is_21(payload):
        return design

    bb_on = _enabled(payload, "BB")
    sfc_on = _enabled(payload, "SFC CL")
    if not bb_on and not sfc_on:
        return design
    if bb_on and _layer(payload, "BB") is None:
        raise RuntimeError("当前 ODN 2.1 已启用 BB，但没有绑定 BB 图层。")
    if sfc_on and _layer(payload, "SFC CL") is None:
        raise RuntimeError("当前 ODN 2.1 已启用 SFC CL，但没有绑定 SFC CL 图层。")

    sequence = [(str(item[0]), int(item[1]), str(item[2])) for item in design.get("sequence", [])]
    # _make_design stores labels/ids separately; prefer the authoritative ids.
    raw_ids = design.get("sequence_ids") or []
    raw_labels = design.get("sequence") or []
    if raw_ids and len(raw_ids) == len(raw_labels):
        sequence = [(str(item[0]), int(item[1]), str(raw_labels[i])) for i, item in enumerate(raw_ids)]

    engine = OdnProjectRouteEngine(
        controller.iface,
        payload,
        max(0.01, float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0) or 3.0)),
    )
    serial_bb = 1
    serial_sfc = 1

    for iteration in range(MAX_ITERATIONS):
        segments = _rebuild(controller, sequence, engine)

        bb_insertions = {}
        if bb_on:
            bb_insertions, serial_bb = _insertions_for_bb(
                controller, payload, design, sequence, segments, engine, serial_bb
            )
        if bb_insertions:
            sequence = _apply_insertions(sequence, bb_insertions)
            engine = OdnProjectRouteEngine(
                controller.iface,
                payload,
                max(0.01, float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0) or 3.0)),
            )
            continue

        sfc_insertions = {}
        if sfc_on:
            sfc_insertions, serial_sfc = _insertions_for_sfc(
                payload, design, sequence, segments, engine, serial_sfc
            )
        if sfc_insertions:
            sequence = _apply_insertions(sequence, sfc_insertions)
            engine = OdnProjectRouteEngine(
                controller.iface,
                payload,
                max(0.01, float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0) or 3.0)),
            )
            continue

        design["sequence"] = [item[2] for item in sequence]
        design["sequence_ids"] = [(item[0], int(item[1])) for item in sequence]
        design["segments"] = segments
        design["nodes"] = [
            (int(item[1]), item[2]) for item in sequence if item[0] == "FAT"
        ]
        design["length"] = round(sum(float(item.get("distance", 0.0) or 0.0) for item in segments), 3)
        design["odn21_planned"] = True
        design["odn21_limits"] = {
            "return_cable_max_m": RETURN_LIMIT_M,
            "distribution_segment_max_m": DC_LIMIT_M,
        }
        design["source_crs"] = engine.edge_layer.crs().authid()
        _log(
            f"[ODN2.1 PLAN COMPLETE] {design.get('fdt')}/{design.get('link')}; "
            f"sequence={len(sequence)}; segments={len(segments)}; length={design['length']:.3f}m"
        )
        return design

    raise RuntimeError("ODN 2.1 BB / SFC CL 自动规划超过最大迭代次数。")


def install():
    """Install the ODN 2.1 planning seam before Link Design is instantiated."""
    try:
        from . import link_design_core
        controller_cls = link_design_core.LinkDesignController
    except Exception as exc:
        _log(f"[INSTALL FAIL] 无法加载 Link Design Core：{exc}", Qgis.Critical)
        return False

    if getattr(controller_cls, "_odn21_planning_installed", False):
        return True

    original = controller_cls._make_design

    def wrapped(self):
        design = original(self)
        if design is None:
            return None
        try:
            return plan_design(self, design)
        except Exception as exc:
            _log(f"[ODN2.1 PLAN ERROR] {type(exc).__name__}: {exc}", Qgis.Critical)
            self._warn("ODN 2.1 自动节点规划", f"BB / SFC CL 自动规划失败：\n{exc}")
            return None

    controller_cls._make_design = wrapped
    controller_cls._odn21_planning_installed = True
    _log("[INSTALL] ODN 2.1 BB / SFC CL planning installed")
    return True
