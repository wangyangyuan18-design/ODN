# -*- coding: utf-8 -*-
"""Global lane optimizer for ODN cable offset.

One authoritative post-plan normalization pass. It minimizes crossings by
preserving route order, physical side, Main ownership and same-side order.
Lane changes are only represented at the route corner; this module never
invents a longitudinal lane-change point.
"""
from collections import defaultdict
from math import hypot
from qgis.core import QgsGeometry, QgsPointXY
from . import cable_offset_core as _core
from . import cable_offset_layout as _base

_INSTALLED = False
_ORIGINAL_PLAN = None

def _sign(v):
    return 1 if int(v) > 0 else -1

def _edge_uses(designs, edge_crs, work):
    pending, routes = _core._collect_uses(designs, edge_crs, work)
    by_edge = defaultdict(list)
    for u in pending:
        by_edge[u.edge_key].append(u)
    return pending, routes, by_edge

def _route_rank(routes, idx):
    r = routes.get(idx, {})
    return (-float(r.get('priority_score', 0)), -float(r.get('longest_directional_run', 0)), -float(r.get('route_length', 0)), int(idx))

def _physical_components(by_edge):
    graph = defaultdict(set); endpoint_to_edges = defaultdict(set)
    for edge in by_edge:
        a,b=_base._edge_points(edge)
        ka=(round(float(a.x()),7),round(float(a.y()),7)); kb=(round(float(b.x()),7),round(float(b.y()),7))
        endpoint_to_edges[ka].add(edge); endpoint_to_edges[kb].add(edge)
    for edges in endpoint_to_edges.values():
        edges=list(edges)
        for e in edges[1:]: graph[edges[0]].add(e); graph[e].add(edges[0])
    comp={}; cid=0
    for edge in by_edge:
        if edge in comp: continue
        q=[edge]; comp[edge]=cid
        while q:
            cur=q.pop()
            for nxt in graph.get(cur,()):
                if nxt not in comp: comp[nxt]=cid; q.append(nxt)
        cid+=1
    return comp

def _reserved_map(dc, designs, spacing, by_edge, edge_crs, work):
    occ=_core._occupancy(dc,designs,work)
    index,geometries=_base._build_existing_index(occ,work)
    out={}
    for edge in by_edge:
        a,b=_base._edge_points(edge); a=_core._tp(a,edge_crs,work); b=_core._tp(b,edge_crs,work)
        out[edge]=set(_base._existing_slot_occupancy(QgsGeometry.fromPolylineXY([a,b]),spacing,index,geometries))
    return out

def _entry_side(use, designs, slots, edge_crs, work):
    seg=designs[use.design_index].get('segments',[])[use.segment_index]; pts=seg.get('points',[]) or []
    try:
        a,b=_base._edge_points(use.edge_key); a=_core._tp(a,edge_crs,work); b=_core._tp(b,edge_crs,work)
        dx,dy=b.x()-a.x(),b.y()-a.y(); L=hypot(dx,dy)
        if L>1e-9:
            for raw in pts[1:]:
                if len(raw)<2: continue
                p=_core._tp(QgsPointXY(float(raw[0]),float(raw[1])),edge_crs,work)
                c=dx*(p.y()-a.y())-dy*(p.x()-a.x())
                if abs(c)>max(1e-7,L*1e-7): return 1 if c>0 else -1
    except Exception: pass
    old=int(slots.get((use.design_index,use.segment_index,use.edge_index),0))
    if old:return _sign(old)
    hint=int(use.side_hint or 0)
    return _sign(hint) if hint else 1

def _landing_owners(designs, edge_crs, work):
    owners=defaultdict(list)
    for di,d in enumerate(designs or []):
        seq=d.get('sequence_ids',[]) or []
        for si,s in enumerate(d.get('segments',[]) or []):
            edges=[e for raw in s.get('edge_sequence',[]) or [] if (e:=_base._canonical_edge(raw))]
            if not edges: continue
            nodes=_base._extract_route_graph_nodes(s,work,edge_crs,edge_crs)
            if len(nodes)!=len(edges)+1: continue
            if si<len(seq):
                p=nodes[0]; owners[(round(float(p.x()),7),round(float(p.y()),7))].append((di,si,'S'))
            if si+1<len(seq):
                p=nodes[-1]; owners[(round(float(p.x()),7),round(float(p.y()),7))].append((di,si,'E'))
    return owners

def _blocked(u,designs,edge_crs,work,external,owners):
    seg=designs[u.design_index].get('segments',[])[u.segment_index]; seq=designs[u.design_index].get('sequence_ids',[]) or []
    edges=[e for raw in seg.get('edge_sequence',[]) or [] if (e:=_base._canonical_edge(raw))]
    if not edges:return False
    nodes=_base._extract_route_graph_nodes(seg,work,edge_crs,edge_crs)
    if len(nodes)!=len(edges)+1:return False
    checks=[]
    if u.edge_index==0 and u.segment_index<len(seq):checks.append(nodes[0])
    if u.edge_index==len(edges)-1 and u.segment_index+1<len(seq):checks.append(nodes[-1])
    for p in checks:
        key=(round(float(p.x()),7),round(float(p.y()),7))
        if any(hypot(p.x()-q.x(),p.y()-q.y())<=0.05 for q in external):return True
        if any(i!=u.design_index for i,_,_ in owners.get(key,())):
            if not (u.edge_index==0 and seg.get('_odn_return_chain')):return True
    return False

def _edge_order(by_edge,routes):
    rank={i:n for n,i in enumerate(sorted(routes,key=lambda x:_route_rank(routes,x)))}; deps={e:set() for e in by_edge}
    for e,uses in by_edge.items():
        for u in uses:
            if u.prev_edge_key in by_edge and u.prev_edge_key!=e:deps[e].add(u.prev_edge_key)
    left=set(by_edge);done=set();out=[]
    while left:
        ready=[e for e in left if deps[e].issubset(done)] or list(left)
        e=min(ready,key=lambda x:min((rank.get(u.design_index,10**9),u.segment_index,u.edge_index) for u in by_edge[x]))
        out.append(e);left.remove(e);done.add(e)
    return out,rank

def _plan(designs,dc,edge_layer,spacing):
    result=_ORIGINAL_PLAN(designs,dc,edge_layer,spacing)
    if not result or len(result)<5:return result
    edge_crs,work,ordered,routes,base_slots=result
    pending,routes,by_edge=_edge_uses(designs,edge_crs,work)
    reserved=_reserved_map(dc,designs,spacing,by_edge,edge_crs,work)
    owners=_landing_owners(designs,edge_crs,work); external=[]
    try:
        mem=_core._occupancy(dc,designs,work)
        for f in mem.getFeatures():
            g=f.geometry()
            if g.isEmpty():continue
            parts=g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            for part in parts:
                if part:external += [QgsPointXY(part[0]),QgsPointXY(part[-1])]
    except Exception:pass
    edge_order,rank=_edge_order(by_edge,routes); component=_physical_components(by_edge)
    main_owner={};side_memory={};order_memory={};assigned={};entry_side={}
    for u in pending:
        key=(u.design_index,u.segment_index)
        if key not in entry_side:entry_side[key]=_entry_side(u,designs,base_slots,edge_crs,work)
    stats={'main_switches':0,'side_crosses':0,'compacted':0,'preserved':0,'cross_prevented':0}
    for edge in edge_order:
        uses=by_edge[edge]; rsv=reserved.get(edge,set()); used=set(rsv); comp=component.get(edge,edge)
        candidates=[u for u in uses if not _blocked(u,designs,edge_crs,work,external,owners)]
        primary=None; old_owner=main_owner.get(comp)
        if 0 not in rsv and candidates:
            keep=[u for u in candidates if old_owner is not None and u.design_index==old_owner]
            if keep:primary=min(keep,key=lambda u:(u.segment_index,u.edge_index))
            if primary is None:
                keep=[u for u in candidates if assigned.get((u.design_index,u.segment_index,u.edge_index-1))==0]
                if keep:primary=min(keep,key=lambda u:(rank.get(u.design_index,10**9),u.segment_index,u.edge_index))
            if primary is None:
                keep=[u for u in candidates if int(base_slots.get((u.design_index,u.segment_index,u.edge_index),999999))==0]
                if keep:primary=min(keep,key=lambda u:(rank.get(u.design_index,10**9),u.segment_index,u.edge_index))
            if primary is None:primary=min(candidates,key=lambda u:(rank.get(u.design_index,10**9),u.segment_index,u.edge_index))
            if old_owner is not None and old_owner!=primary.design_index:stats['main_switches']+=1
            main_owner[comp]=primary.design_index;assigned[(primary.design_index,primary.segment_index,primary.edge_index)]=0;used.add(0)
        groups={1:[], -1:[]}
        for u in uses:
            key=(u.design_index,u.segment_index,u.edge_index)
            if key in assigned:continue
            prev=assigned.get((u.design_index,u.segment_index,u.edge_index-1)); sk=(u.design_index,u.segment_index)
            side=side_memory.get(sk)
            if prev not in (None,0):side=_sign(prev)
            if side not in (1,-1):side=entry_side.get(sk,1)
            groups[side].append((u,prev))
        for side in (1,-1):
            members=groups[side]
            members.sort(key=lambda x:(0 if x[1] not in (None,0) and _sign(x[1])==side else 1,abs(int(x[1])) if x[1] not in (None,0) and _sign(x[1])==side else 10**6,order_memory.get((x[0].design_index,x[0].segment_index),10**6),rank.get(x[0].design_index,10**9),x[0].segment_index,x[0].edge_index))
            mag=1
            for u,prev in members:
                while side*mag in used:mag+=1
                chosen=side*mag;key=(u.design_index,u.segment_index,u.edge_index);assigned[key]=chosen;used.add(chosen)
                sk=(u.design_index,u.segment_index);side_memory[sk]=side;order_memory[sk]=mag
                if prev not in (None,0) and _sign(prev)==side and int(prev)==chosen:stats['preserved']+=1
                mag+=1
        for u in uses:
            key=(u.design_index,u.segment_index,u.edge_index)
            if key in assigned:continue
            old=int(base_slots.get(key,0))
            if old==0:continue
            side=_sign(old);mag=abs(old)
            while side*mag in used:mag+=1
            assigned[key]=side*mag;used.add(side*mag)
        for u in uses:
            key=(u.design_index,u.segment_index,u.edge_index);val=int(assigned.get(key,0));base_slots[key]=val;u.slot=val
        for side in (1,-1):
            mags=sorted(abs(int(assigned[(u.design_index,u.segment_index,u.edge_index)])) for u in uses if int(assigned.get((u.design_index,u.segment_index,u.edge_index),0))*side>0)
            if mags and mags!=list(range(1,len(mags)+1)):stats['compacted']+=1
    _core._log('[LANE-OPT] strategy=MIN_CROSSING; '+f"edges={len(edge_order)}; main_switches={stats['main_switches']}; side_crosses={stats['side_crosses']}; preserved={stats['preserved']}; compacted={stats['compacted']}; cross_prevented={stats['cross_prevented']}")
    return (edge_crs,work,ordered,routes,base_slots)

def install():
    global _INSTALLED,_ORIGINAL_PLAN
    if _INSTALLED:return
    _ORIGINAL_PLAN=_core._plan;_core._plan=_plan;_INSTALLED=True
    _core._log('[LANE-OPT] global allocator installed; objective=minimum-crossing + route-continuity')
install()
