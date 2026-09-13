# -*- coding: utf-8 -*-
"""Corner timing rule for the authoritative ODN offset engine.

Lane allocation is owned by cable_offset_policy + cable_offset_lane_optimizer.
This module only enforces the timing of an ordinary lane change: the
incoming lane continues to the Corner Pole and the outgoing lane starts there.
It deliberately does not allocate or rewrite lane slots.
"""
from math import hypot
from qgis.core import QgsPointXY
from . import cable_offset_core as _core
from . import cable_offset_layout as _base
from . import cable_offset_direction_fix as _direction_fix

_INSTALLED=False
_ORIGINAL_CORNER=_core._build_corner_geometry

def _node_match(a,b,eps=1e-7):
    return hypot(float(a.x())-float(b.x()),float(a.y())-float(b.y())) <= eps

def _oriented_edge(edge,node,incoming):
    a,b=_base._edge_points(edge)
    edge_crs=getattr(_direction_fix,'_ORIENT_EDGE_CRS',None)
    work=getattr(_direction_fix,'_ORIENT_WORK_CRS',None)
    if edge_crs is not None and work is not None:
        a=_core._tp(a,edge_crs,work); b=_core._tp(b,edge_crs,work)
    if incoming:
        if _node_match(b,node):return a,b
        if _node_match(a,node):return b,a
    else:
        if _node_match(a,node):return a,b
        if _node_match(b,node):return b,a
    return a,b

def _corner_at_pole(node,in_edge,out_edge,prev_slot,next_slot,spacing):
    in_a,in_b=_oriented_edge(in_edge,node,True)
    out_a,out_b=_oriented_edge(out_edge,node,False)
    incoming_anchor=_core._offset_lane_point(node,_base._unit(in_a,in_b),int(prev_slot),spacing)
    outgoing_anchor=_core._offset_lane_point(node,_base._unit(out_a,out_b),int(next_slot),spacing)
    return [QgsPointXY(incoming_anchor),QgsPointXY(outgoing_anchor)]

def _patched_corner(node,in_edge,out_edge,prev_slot,next_slot,spacing,return_context=False):
    prev_slot=int(prev_slot);next_slot=int(next_slot)
    if prev_slot==next_slot or return_context:
        return _ORIGINAL_CORNER(node,in_edge,out_edge,prev_slot,next_slot,spacing,return_context)
    points=_corner_at_pole(node,in_edge,out_edge,prev_slot,next_slot,spacing)
    decision=_core._corner_decision(prev_slot,next_slot,return_context=False)
    _core._log(f'[LANE-CHANGE] decision={decision}; prev={prev_slot}; next={next_slot}; timing=CORNER_POLE; early-change=FORBIDDEN; geometry=AT_POLE')
    return decision,points

def install():
    global _INSTALLED
    if _INSTALLED:return
    _core._build_corner_geometry=_patched_corner
    _INSTALLED=True
    _core._log('[CORNER-RULE] lane-change=AT_CORNER_POLE; early-change=FORBIDDEN; allocator=GLOBAL')
install()
