# -*- coding: utf-8 -*-
"""ODN 2.1 planning diagnostics.

Authoritative engineering limits for ODN 2.1:
- Return cable: repeated physical Pole Edge out-and-back, one-way length * 2.
- BB trigger: return length > 100 m.
- Distribution / pre-link cable: every individual ODN-node-to-ODN-node segment
  must be <= 455 m; an SFC Closure is placed at the last real Pole node that
  stays within 455 m from that segment's start.

The actual automatic insertion is performed by ``odn21_planning.py``.  This
module keeps the diagnostic view consistent with the same rules.
"""

from qgis.core import QgsMessageLog, Qgis

_LOG_TAG = "ODN_Tools_Pro / ODN 2.1"
DEFAULT_RETURN_LIMIT_M = 100.0
DEFAULT_BB_TRIGGER_M = 100.0
DEFAULT_DC_LIMIT_M = 455.0


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), _LOG_TAG, level)
    except Exception:
        pass


def _parameters(payload):
    return (payload or {}).get("parameters") or {}


def _number(payload, names, default):
    params = _parameters(payload)
    for name in names:
        value = params.get(name)
        if value in (None, ""):
            continue
        try:
            value = float(value)
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue
    return float(default)


def is_odn21(payload):
    version = str(((payload or {}).get("project") or {}).get("odn_version", "")).strip().lower()
    return version in ("2.1", "odn 2.1", "odn2.1", "odn_2.1")


def enabled(payload, role):
    if not is_odn21(payload):
        return False
    project = payload.get("project") or {}
    if role in set(project.get("node_types") or []):
        return True
    registry = payload.get("layer_registry") or {}
    for key in (role, role.lower(), role.replace(" ", "_"), role.lower().replace(" ", "_")):
        item = registry.get(key)
        if isinstance(item, dict) and item.get("layer_id"):
            return True
    return False


def limits(payload):
    return {
        "return_cable": _number(
            payload,
            ("return_cable_max_distance", "return_cable_limit", "back_cable_max_distance", "back_cable_limit"),
            DEFAULT_RETURN_LIMIT_M,
        ),
        "bb_trigger": _number(
            payload,
            ("bb_trigger_distance", "bb_return_cable_trigger", "bb_limit", "bb_return_threshold"),
            DEFAULT_BB_TRIGGER_M,
        ),
        "dc": _number(
            payload,
            ("prelinked_cable_max_length", "pre_linked_cable_max_length", "dc_cable_max_length", "optical_cable_max_length"),
            DEFAULT_DC_LIMIT_M,
        ),
    }


def _edge_key(edge):
    try:
        return (int(edge[0]), tuple(edge[1]))
    except Exception:
        return str(edge)


def _edge_length(engine, edge):
    key = _edge_key(edge)
    try:
        return float(engine.edge_len[key])
    except Exception:
        try:
            return float(engine.edge_len.get(edge, 0.0))
        except Exception:
            return 0.0


def _return_loops(segment):
    """Return repeated physical-edge traversals and the defined return endpoint."""
    edges = list(segment.get("edge_sequence") or [])
    nodes = list(segment.get("graph_nodes") or [])
    first_seen = {}
    loops = []
    for i, edge in enumerate(edges):
        key = _edge_key(edge)
        if key in first_seen:
            j = first_seen[key]
            endpoint = nodes[j + 1] if j + 1 < len(nodes) else None
            loops.append({
                "edge": edge,
                "edge_key": key,
                "first": j,
                "second": i,
                "return_endpoint": endpoint,
            })
        else:
            first_seen[key] = i
    return loops


def _last_pole_at_limit(segment, engine, limit):
    nodes = list(segment.get("graph_nodes") or [])
    edges = list(segment.get("edge_sequence") or [])
    if len(nodes) < 2 or len(edges) != len(nodes) - 1:
        return None
    cumulative = float(segment.get("start_connector", 0.0) or 0.0)
    candidate = None
    for i, edge in enumerate(edges):
        nxt = cumulative + _edge_length(engine, edge)
        if nxt <= limit + 1e-6:
            candidate = {
                "node_index": i + 1,
                "distance": nxt,
                "point": nodes[i + 1],
            }
            cumulative = nxt
        else:
            break
    return candidate


def analyze_designs(designs, engine, payload):
    if not is_odn21(payload):
        return {"enabled": False, "bb": [], "sfc": [], "violations": []}
    lim = limits(payload)
    result = {"enabled": True, "limits": lim, "bb": [], "sfc": [], "violations": []}
    bb_on = enabled(payload, "BB")
    sfc_on = enabled(payload, "SFC CL")
    _log(
        f"[ODN2.1 START] designs={len(designs or [])}; BB={int(bb_on)}; SFC Closure={int(sfc_on)}; "
        f"return_limit={lim['return_cable']:.3f}m; BB_trigger={lim['bb_trigger']:.3f}m; DC_limit={lim['dc']:.3f}m"
    )

    for di, design in enumerate(designs or []):
        for si, segment in enumerate(design.get("segments", []) or []):
            if bb_on:
                for loop in _return_loops(segment):
                    one_way = _edge_length(engine, loop["edge"])
                    ret = one_way * 2.0
                    item = {
                        "design": di,
                        "segment": si,
                        "edge_key": loop["edge_key"],
                        "one_way": one_way,
                        "return_length": ret,
                        "return_endpoint": loop["return_endpoint"],
                        "needs_bb": ret > lim["bb_trigger"],
                    }
                    result["bb"].append(item)
                    _log(
                        f"[BB CHECK] design={di}; segment={si + 1}; one_way={one_way:.3f}m; "
                        f"return={ret:.3f}m; trigger={lim['bb_trigger']:.3f}m; "
                        f"needs_bb={int(item['needs_bb'])}; return_endpoint={loop['return_endpoint']}"
                    )
                    if item["needs_bb"]:
                        result["violations"].append(("BB", di, si, item))

            if sfc_on:
                distance = float(segment.get("distance", 0.0) or 0.0)
                if distance > lim["dc"] + 1e-6:
                    candidate = _last_pole_at_limit(segment, engine, lim["dc"])
                    item = {
                        "design": di,
                        "segment": si,
                        "segment_distance": distance,
                        "candidate": candidate,
                    }
                    result["sfc"].append(item)
                    if candidate:
                        _log(
                            f"[SFC CHECK] design={di}; segment={si + 1}; length={distance:.3f}m; "
                            f"limit={lim['dc']:.3f}m; selected_node={candidate['node_index']}; "
                            f"selected_distance={candidate['distance']:.3f}m"
                        )
                    else:
                        _log(
                            f"[SFC CHECK] design={di}; segment={si + 1}; length={distance:.3f}m; "
                            f"limit={lim['dc']:.3f}m; NO NODE <= LIMIT",
                            Qgis.Warning,
                        )
                    result["violations"].append(("SFC Closure", di, si, item))

    _log(
        f"[ODN2.1 END] BB_checks={len(result['bb'])}; SFC_checks={len(result['sfc'])}; "
        f"violations={len(result['violations'])}"
    )
    return result


def run_diagnostics(designs, engine, payload):
    try:
        return analyze_designs(designs, engine, payload)
    except Exception as exc:
        _log(f"[ODN2.1 ERROR] {type(exc).__name__}: {exc}", Qgis.Critical)
        return {"enabled": is_odn21(payload), "error": str(exc), "bb": [], "sfc": [], "violations": []}
