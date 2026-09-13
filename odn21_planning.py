# -*- coding: utf-8 -*-
"""Authoritative ODN 2.1 BB / SFC CL planning.

ODN 2.1 has exactly two fixed physical planning rules:

* BB: return cable is the same Pole Edge traversed out and back. If the
  defined start-to-end Pole Edge distance * 2 is > 100 m, put BB at the
  previously defined return endpoint.
* SFC CL: every segment starts at its ODN node / aggregation point. Walk pole
  by pole from that start. When the next pole would make the cumulative
  distance > 455 m, step back to the last pole whose cumulative distance is
  <= 455 m and put SFC CL there. The next segment starts from that SFC CL.

There is no candidate scoring, no alternative threshold source and no change
inside Offset Core. BB and SFC CL are real ODN nodes and therefore become part
of the authoritative Link sequence before Offset Core runs.
"""

from math import inf

from qgis.core import QgsFeature, QgsGeometry, QgsMessageLog, QgsPointXY, Qgis

from . import odn_project_context as context
from .link_design_core import fresh_payload
from .odn_project_routing import OdnProjectRouteEngine, feature_name

LOG_TAG = "ODN_Tools_Pro / ODN 2.1 Planning"
RETURN_LIMIT_M = 100.0
SFC_LIMIT_M = 455.0
POINT_TOLERANCE_M = 0.05
MAX_ITERATIONS = 64


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), LOG_TAG, level)
    except Exception:
        pass


def _payload(controller):
    return fresh_payload(controller)


def _is_21(payload):
    version = str(((payload or {}).get("project") or {}).get("odn_version", "")).strip().lower()
    return version in {"2.1", "odn 2.1", "odn2.1", "odn_2.1"}


def _enabled(payload, role):
    if not _is_21(payload):
        return False
    project = payload.get("project") or {}
    if role in set(project.get("node_types") or []):
        return True
    registry = payload.get("layer_registry") or {}
    aliases = (role, role.lower(), role.replace(" ", "_"), role.lower().replace(" ", "_"))
    return any(bool((registry.get(key) or {}).get("layer_id")) for key in aliases)


def _layer(payload, role):
    return context.project_layer(payload, role)


def _point_distance_m(engine, a, b):
    if a is None or b is None:
        return inf
    try:
        return float(engine._point_distance(QgsPointXY(a), QgsPointXY(b)))
    except Exception:
        return inf


def _same_point(engine, a, b):
    return _point_distance_m(engine, a, b) <= POINT_TOLERANCE_M


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
    """Find repeated physical Pole Edge traversals and the defined endpoint."""
    edges = list(segment.get("edge_sequence") or [])
    nodes = list(segment.get("graph_nodes") or [])
    first_seen = {}
    loops = []
    for index, edge in enumerate(edges):
        key = _edge_key(edge)
        if key in first_seen:
            first_index = first_seen[key]
            endpoint = nodes[first_index + 1] if first_index + 1 < len(nodes) else None
            loops.append({
                "edge": edge,
                "edge_key": key,
                "first_index": first_index,
                "repeat_index": index,
                "return_endpoint": endpoint,
            })
        else:
            first_seen[key] = index
    return loops


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


def _field_for_name(payload, layer, role):
    mapping = (payload.get("field_registry") or {}).get(role, {})
    configured = mapping.get("名称")
    if configured and configured in layer.fields().names():
        return configured
    names = {name.lower(): name for name in layer.fields().names()}
    for candidate in ("name", "名称"):
        if candidate.lower() in names:
            return names[candidate.lower()]
    return None


def _set_name(payload, layer, role, feature, name):
    field_name = _field_for_name(payload, layer, role)
    if field_name:
        feature[field_name] = name


def _create_node(payload, engine, role, point, name):
    layer = _layer(payload, role)
    if layer is None:
        raise RuntimeError(f"当前 ODN 2.1 已启用 {role}，但没有绑定 {role} 图层。")
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
    return controller._route(first, second)


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


def _last_pole_at_limit(segment, engine):
    """Walk from segment start; return last pole whose cumulative distance <=455m."""
    nodes = list(segment.get("graph_nodes") or [])
    edges = list(segment.get("edge_sequence") or [])
    if len(nodes) < 2 or len(edges) != len(nodes) - 1:
        return None

    cumulative = float(segment.get("start_connector", 0.0) or 0.0)
    candidate = None
    for index, edge in enumerate(edges):
        next_distance = cumulative + _edge_length(engine, edge)
        if next_distance <= SFC_LIMIT_M + 1e-6:
            candidate = {
                "node_index": index + 1,
                "distance": next_distance,
                "node": nodes[index + 1],
            }
            cumulative = next_distance
            continue
        # The next pole is over the limit: the immediately previous legal pole
        # is the fixed SFC CL position.
        break
    return candidate


def _bb_insertion_for_segment(segment, engine, segment_index, design):
    for loop in _return_loops(segment):
        edge_length = _edge_length(engine, loop["edge"])
        return_length = edge_length * 2.0
        endpoint = loop["return_endpoint"]
        if endpoint is None:
            continue
        _log(
            f"[BB CHECK] {design.get('fdt')}/{design.get('link')}; segment={segment_index + 1}; "
            f"start_end_pole_edge={edge_length:.3f}m; return={return_length:.3f}m; limit={RETURN_LIMIT_M:.3f}m"
        )
        if return_length > RETURN_LIMIT_M + 1e-6:
            _log(
                f"[BB FIXED] {design.get('fdt')}/{design.get('link')}; segment={segment_index + 1}; "
                f"return_endpoint={endpoint}"
            )
            return loop["first_index"] + 1, endpoint, return_length
    return None


def _insert_bb(payload, engine, design, segment_index, segment, serial):
    found = _bb_insertion_for_segment(segment, engine, segment_index, design)
    if found is None:
        return None, serial
    node_index, node, return_length = found
    point = engine.node_points[node]
    name = _new_name(design, "BB", serial)
    fid = _create_node(payload, engine, "BB", point, name)
    label = _label(payload, "BB", fid)
    _log(
        f"[BB INSERT] {design.get('fdt')}/{design.get('link')}; segment={segment_index + 1}; "
        f"return={return_length:.3f}m; endpoint={node}; label={label}"
    )
    return (node_index, ("BB", fid, label)), serial + 1


def _insert_sfc(payload, engine, design, segment_index, segment, serial):
    length = float(segment.get("distance", 0.0) or 0.0)
    if length <= SFC_LIMIT_M + 1e-6:
        return None, serial

    candidate = _last_pole_at_limit(segment, engine)
    if candidate is None:
        _log(
            f"[SFC CHECK] {design.get('fdt')}/{design.get('link')}; segment={segment_index + 1}; "
            f"length={length:.3f}m; limit={SFC_LIMIT_M:.3f}m; no legal pole",
            Qgis.Warning,
        )
        raise RuntimeError(
            f"{design.get('fdt')}/{design.get('link')} 第 {segment_index + 1} 段超过 {SFC_LIMIT_M:g}m，"
            "且从该段起点到下一根杆之前没有任何累计距离 <=455m 的可放置 SFC CL 杆。"
        )

    node = candidate["node"]
    point = engine.node_points[node]
    name = _new_name(design, "SFC_CL", serial)
    fid = _create_node(payload, engine, "SFC CL", point, name)
    label = _label(payload, "SFC CL", fid)
    _log(
        f"[SFC INSERT] {design.get('fdt')}/{design.get('link')}; segment={segment_index + 1}; "
        f"segment_length={length:.3f}m; limit={SFC_LIMIT_M:.3f}m; "
        f"selected_distance={candidate['distance']:.3f}m; node={node}; label={label}"
    )
    return (candidate["node_index"], ("SFC CL", fid, label)), serial + 1


def _apply_insertions(sequence, insertion_by_segment):
    """Insert each node between the segment start and segment end."""
    result = []
    for segment_index, item in enumerate(sequence[:-1]):
        result.append(item)
        insertion = insertion_by_segment.get(segment_index)
        if insertion:
            _, node_item = insertion
            if result[-1][:2] != node_item[:2]:
                result.append(node_item)
    result.append(sequence[-1])
    return result


def plan_design(controller, design):
    payload = _payload(controller)
    if not _is_21(payload):
        return design

    bb_on = _enabled(payload, "BB")
    sfc_on = _enabled(payload, "SFC CL")
    if bb_on and _layer(payload, "BB") is None:
        raise RuntimeError("ODN 2.1 已启用 BB，但没有绑定 BB 图层。")
    if sfc_on and _layer(payload, "SFC CL") is None:
        raise RuntimeError("ODN 2.1 已启用 SFC CL，但没有绑定 SFC CL 图层。")
    if not bb_on and not sfc_on:
        return design

    raw_ids = design.get("sequence_ids") or []
    raw_labels = design.get("sequence") or []
    if not raw_ids or len(raw_ids) != len(raw_labels):
        raise RuntimeError("ODN 2.1 自动规划需要完整的 sequence_ids / sequence。")
    sequence = [(str(item[0]), int(item[1]), str(raw_labels[i])) for i, item in enumerate(raw_ids)]

    engine = OdnProjectRouteEngine(
        controller.iface,
        payload,
        max(0.01, float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0) or 3.0)),
    )

    serial_bb = 1
    serial_sfc = 1
    for _ in range(MAX_ITERATIONS):
        segments = _rebuild(controller, sequence, engine)

        if bb_on:
            for segment_index, segment in enumerate(segments):
                insertion, serial_bb_next = _insert_bb(
                    payload, engine, design, segment_index, segment, serial_bb
                )
                if insertion is not None:
                    sequence = _apply_insertions(
                        sequence,
                        {segment_index: insertion},
                    )
                    serial_bb = serial_bb_next
                    engine = OdnProjectRouteEngine(
                        controller.iface,
                        payload,
                        max(0.01, float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0) or 3.0)),
                    )
                    break
            else:
                pass
            if insertion is not None:
                continue

        if sfc_on:
            for segment_index, segment in enumerate(segments):
                insertion, serial_sfc_next = _insert_sfc(
                    payload, engine, design, segment_index, segment, serial_sfc
                )
                if insertion is not None:
                    sequence = _apply_insertions(sequence, {segment_index: insertion})
                    serial_sfc = serial_sfc_next
                    engine = OdnProjectRouteEngine(
                        controller.iface,
                        payload,
                        max(0.01, float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0) or 3.0)),
                    )
                    break
            else:
                insertion = None
            if insertion is not None:
                continue

        final_segments = _rebuild(controller, sequence, engine)
        design["sequence"] = [item[2] for item in sequence]
        design["sequence_ids"] = [(item[0], int(item[1])) for item in sequence]
        design["segments"] = final_segments
        design["nodes"] = [(int(item[1]), item[2]) for item in sequence if item[0] == "FAT"]
        design["length"] = round(sum(float(item.get("distance", 0.0) or 0.0) for item in final_segments), 3)
        design["odn21_planned"] = True
        design["odn21_limits"] = {
            "return_cable_max_m": RETURN_LIMIT_M,
            "sfc_cl_max_segment_m": SFC_LIMIT_M,
        }
        design["source_crs"] = engine.edge_layer.crs().authid()
        _log(
            f"[ODN2.1 COMPLETE] {design.get('fdt')}/{design.get('link')}; "
            f"sequence={len(sequence)}; segments={len(final_segments)}; total={design['length']:.3f}m"
        )
        return design

    raise RuntimeError("ODN 2.1 BB / SFC CL 自动规划超过最大迭代次数。")


def install():
    """Install ODN 2.1 planning before Link Design save runs."""
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
