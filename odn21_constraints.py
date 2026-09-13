# -*- coding: utf-8 -*-
"""ODN 2.1 planning diagnostics.

These are fixed engineering rules. There is intentionally no parameter or UI
entry that can override them:
- BB: return length > 100 m.
- SFC CL: every individual segment <= 455 m.
"""

from qgis.core import QgsMessageLog, Qgis

_LOG_TAG = "ODN_Tools_Pro / ODN 2.1"
RETURN_LIMIT_M = 100.0
SFC_LIMIT_M = 455.0


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), _LOG_TAG, level)
    except Exception:
        pass


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


def limits(payload=None):
    """Return the only authoritative ODN 2.1 limits."""
    return {
        "return_cable": RETURN_LIMIT_M,
        "bb_trigger": RETURN_LIMIT_M,
        "dc": SFC_LIMIT_M,
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
    """Return repeated physical-edge traversals and their defined endpoint."""
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


def _last_pole_at_limit(segment, engine):
    nodes = list(segment.get("graph_nodes") or [])
    edges = list(segment.get("edge_sequence") or [])
    if len(nodes) < 2 or len(edges) != len(nodes) - 1:
        return None
    cumulative = float(segment.get("start_connector", 0.0) or 0.0)
    candidate = None
    for i, edge in enumerate(edges):
        nxt = cumulative + _edge_length(engine, edge)
        if nxt <= SFC_LIMIT_M + 1e-6:
            candidate = {"node_index": i + 1, "distance": nxt, "point": nodes[i + 1]}
            cumulative = nxt
        else:
            break
    return candidate


def analyze_designs(designs, engine, payload):
    if not is_odn21(payload):
        return {"enabled": False, "bb": [], "sfc": [], "violations": []}
    lim = limits()
    result = {"enabled": True, "limits": lim, "bb": [], "sfc": [], "violations": []}
    bb_on = enabled(payload, "BB")
    sfc_on = enabled(payload, "SFC CL")
    _log(
        f"[ODN2.1 START] designs={len(designs or [])}; BB={int(bb_on)}; SFC Closure={int(sfc_on)}; "
        f"return_limit={RETURN_LIMIT_M:.3f}m; SFC_limit={SFC_LIMIT_M:.3f}m"
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
                        "needs_bb": ret > RETURN_LIMIT_M,
                    }
                    result["bb"].append(item)
                    _log(
                        f"[BB CHECK] design={di}; segment={si + 1}; "
                        f"start_end_pole_edge={one_way:.3f}m; return={ret:.3f}m; "
                        f"endpoint={loop['return_endpoint']}; needs_bb={int(item['needs_bb'])}"
                    )
                    if item["needs_bb"]:
                        result["violations"].append(("BB", di, si, item))

            if sfc_on:
                distance = float(segment.get("distance", 0.0) or 0.0)
                if distance > SFC_LIMIT_M:
                    candidate = _last_pole_at_limit(segment, engine)
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
                            f"limit={SFC_LIMIT_M:.3f}m; selected_distance={candidate['distance']:.3f}m; "
                            f"node={candidate['point']}"
                        )
                    else:
                        _log(
                            f"[SFC CHECK] design={di}; segment={si + 1}; length={distance:.3f}m; "
                            f"limit={SFC_LIMIT_M:.3f}m; NO NODE <= LIMIT",
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
