# -*- coding: utf-8 -*-
"""Final global lane allocation policy.

Rules: Main Lane continuity, stable physical side, same-side order, Group
Outside, compact same-side gaps, no lane change on straight Pole Edge
continuations, and minimum crossing behaviour at shared edges.
"""
from collections import defaultdict
from math import hypot
from qgis.core import QgsGeometry, QgsPointXY
from . import cable_offset_core as _core
from . import cable_offset_layout as _base

_INSTALLED=False
_ORIGINAL_PLAN=None

def _sgn(v): return 1 if int(v)>0 else -1
def _edge_list(s): return [e for raw in s.get('edge_sequence',[]) or [] if (e:=_base._canonical_edge(raw))]
def _route_rank(routes,i):
    r=routes.get(i,{})
    return (-float(r.get('priority_score',0)),-float(r.get('longest_directional_run',0)),-float(r.get('route_length',0)),int(i))

def _entry_side(u,designs,slots,edge_crs,work):
    s=designs[u.design_index].get('segments',[])[u.segment_index];pts=s.get('points',[]) or []
    try:
        a,b=_base._edge_points(u.edge_key);a=_core._tp(a,edge_crs,work);b=_core._tp(b,edge_crs,work)
        dx,dy=b.x()-a.x(),b.y()-a.y();L=hypot(dx,dy)
        if L>1e-9:
            for raw in pts[1:]:
                if len(raw)<2:continue
                p=_core._tp(QgsPointXY(float(raw[0]),float(raw[1])),edge_crs,work);c=dx*(p.y()-a.y())-dy*(p.x()-a.x())
                if abs(c)>max(1e-7,L*1e-7):return 1 if c>0 else -1
    except Exception:pass
    old=int(slots.get((u.design_index,u.segment_index,u.edge_index),0))
    if old:return _sgn(old)
    h=int(u.side_hint or 0);return _sgn(h) if h else 1

def _first_shared_edges(pending,by_edge):
    out={}
    for u in sorted(pending,key=lambda x:(x.design_index,x.segment_index,x.edge_index)):
        sk=(u.design_index,u.segment_index)
        if sk in out:continue
        if len(by_edge.get(u.edge_key,()))>1:out[sk]=u
    for u in pending:out.setdefault((u.design_index,u.segment_index),u)
    return out

def _corner_transition(u,designs,edge_crs,work):
    if u.edge_index<=0:return False
    s=designs[u.design_index].get('segments',[])[u.segment_index];edges=_edge_list(s);nodes=_base._extract_route_graph_nodes(s,work,edge_crs,edge_crs)
    if len(nodes)!=len(edges)+1:return True
    a,b,c=nodes[u.edge_index-1],nodes[u.edge_index],nodes[u.edge_index+1];v1=_base._unit(a,b);v2=_base._unit(b,c)
    return max(-1,min(1,v1[0]*v2[0]+v1[1]*v2[1]))<0.985

def _components(by_edge):
    at=defaultdict(set);g=defaultdict(set)
    for e in by_edge:
        a,b=_base._edge_points(e);ka=(round(a.x(),7),round(a.y(),7));kb=(round(b.x(),7),round(b.y(),7));at[ka].add(e);at[kb].add(e)
    for es in at.values():
        es=list(es)
        for e in es[1:]:g[es[0]].add(e);g[e].add(es[0])
    comp={};n=0
    for e in by_edge:
        if e in comp:continue
        q=[e];comp[e]=n
        while q:
            x=q.pop()
            for y in g.get(x,()):
                if y not in comp:comp[y]=n;q.append(y)
        n+=1
    return comp

def _reserved(dc,designs,by_edge,spacing,edge_crs,work):
    occ=_core._occupancy(dc,designs,work);idx,geoms=_base._build_existing_index(occ,work);out={}
    for e in by_edge:
        a,b=_base._edge_points(e);a=_core._tp(a,edge_crs,work);b=_core._tp(b,edge_crs,work)
        out[e]=set(_base._existing_slot_occupancy(QgsGeometry.fromPolylineXY([a,b]),spacing,idx,geoms))
    return out

def _plan(designs,dc,edge_layer,spacing):
    result=_ORIGINAL_PLAN(designs,dc,edge_layer,spacing)
    if not result or len(result)<5:return result
    edge_crs,work,ordered,routes,slots=result;pending,_routes=_core._collect_uses(designs,edge_crs,work)
    by_edge=defaultdict(list)
    for u in pending:by_edge[u.edge_key].append(u)
    if not by_edge:return result
    rr={i:n for n,i in enumerate(sorted(_routes,key=lambda x:_route_rank(_routes,x)))}
    first=_first_shared_edges(pending,by_edge);entry_side={k:_entry_side(u,designs,slots,edge_crs,work) for k,u in first.items()}
    rsv=_reserved(dc,designs,by_edge,spacing,edge_crs,work);comp=_components(by_edge)
    edge_order=sorted(by_edge,key=lambda e:min((rr.get(u.design_index,10**9),u.segment_index,u.edge_index) for u in by_edge[e]))
    left=set(edge_order);final_edges=[]
    while left:
        ready=[e for e in left if all(u.prev_edge_key not in left for u in by_edge[e])]
        if not ready:ready=list(left)
        e=min(ready,key=lambda x:min((rr.get(u.design_index,10**9),u.segment_index,u.edge_index) for u in by_edge[x]));final_edges.append(e);left.remove(e)
    main_owner={};side_mem={};order_mem={};assigned={}
    stats={'main_switch':0,'side_cross':0,'straight_preserved':0,'order_preserved':0,'new_outside':0,'compressed':0}
    for e in final_edges:
        uses=by_edge[e];used=set(rsv.get(e,set()));old_owner=main_owner.get(comp.get(e,e));primary=None
        candidates=[u for u in uses if 0 not in rsv.get(e,set())]
        prev0=[u for u in candidates if assigned.get((u.design_index,u.segment_index,u.edge_index-1))==0]
        if prev0:primary=min(prev0,key=lambda u:(rr.get(u.design_index,10**9),u.segment_index,u.edge_index))
        if primary is None and old_owner is not None:
            keep=[u for u in candidates if u.design_index==old_owner]
            if keep:primary=min(keep,key=lambda u:(u.segment_index,u.edge_index))
        if primary is None and candidates:
            base0=[u for u in candidates if int(slots.get((u.design_index,u.segment_index,u.edge_index),999999))==0]
            primary=min(base0 or candidates,key=lambda u:(rr.get(u.design_index,10**9),u.segment_index,u.edge_index))
        if primary is not None:
            if old_owner is not None and old_owner!=primary.design_index:stats['main_switch']+=1
            main_owner[comp.get(e,e)]=primary.design_index;assigned[(primary.design_index,primary.segment_index,primary.edge_index)]=0;used.add(0)
        groups={1:[], -1:[]}
        for u in uses:
            k=(u.design_index,u.segment_index,u.edge_index)
            if k in assigned:continue
            prev=assigned.get((u.design_index,u.segment_index,u.edge_index-1));sk=(u.design_index,u.segment_index)
            if not _corner_transition(u,designs,edge_crs,work) and prev is not None and int(prev) not in used:
                assigned[k]=int(prev);used.add(int(prev));stats['straight_preserved']+=1;continue
            side=side_mem.get(sk)
            if prev not in (None,0):side=_sgn(prev)
            if side not in (1,-1):side=entry_side.get(sk,1)
            groups[side].append((u,prev))
        for side in (1,-1):
            members=groups[side]
            members.sort(key=lambda z:(0 if z[1] not in (None,0) and _sgn(z[1])==side else 1,abs(int(z[1])) if z[1] not in (None,0) and _sgn(z[1])==side else 10**6,order_mem.get((z[0].design_index,z[0].segment_index),10**6),rr.get(z[0].design_index,10**9),z[0].segment_index,z[0].edge_index))
            mag=1
            for u,prev in members:
                while side*mag in used:mag+=1
                k=(u.design_index,u.segment_index,u.edge_index);chosen=side*mag;assigned[k]=chosen;used.add(chosen);sk=(u.design_index,u.segment_index);side_mem[sk]=side;order_mem[sk]=mag
                if prev not in (None,0) and _sgn(prev)==side and int(prev)==chosen:stats['order_preserved']+=1
                if prev in (None,0):stats['new_outside']+=1
                mag+=1
        for u in uses:
            k=(u.design_index,u.segment_index,u.edge_index)
            if k in assigned:continue
            old=int(slots.get(k,0))
            if not old:continue
            side=_sgn(old);mag=abs(old)
            while side*mag in used:mag+=1
            assigned[k]=side*mag;used.add(side*mag)
        for u in uses:
            k=(u.design_index,u.segment_index,u.edge_index);v=int(assigned.get(k,0));slots[k]=v;u.slot=v
        for side in (1,-1):
            mags=sorted(abs(int(assigned[(u.design_index,u.segment_index,u.edge_index)])) for u in uses if int(assigned.get((u.design_index,u.segment_index,u.edge_index),0))*side>0)
            if mags and mags!=list(range(1,len(mags)+1)):stats['compressed']+=1
    _core._log('[LANE-FINAL] strategy=MIN_CROSSING; '+f"edges={len(final_edges)}; main_switch={stats['main_switch']}; side_cross={stats['side_cross']}; straight_preserved={stats['straight_preserved']}; order_preserved={stats['order_preserved']}; new_outside={stats['new_outside']}; compressed={stats['compressed']}")
    return (edge_crs,work,ordered,routes,slots)

def install():
    global _INSTALLED,_ORIGINAL_PLAN
    if _INSTALLED:return
    _ORIGINAL_PLAN=_core._plan;_core._plan=_plan;_INSTALLED=True
    _core._log('[LANE-FINAL] global lane policy installed; objective=minimum crossing + route continuity')
install()
