"""Compiled immutable graph topology.

``compile_graph`` is the one place that indexes and sorts a validated
``GraphRoot``. Snapshots publish the result (``RoutingSnapshot.compiled``), so
execution never re-indexes nodes or re-sorts edges per request. Duplicate node
ids and unknown ports are rejected by the validators *before* compilation; this
module never patches an invalid graph.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from router.routing.graph.model import Edge, GraphNode, GraphRoot
from router.routing.graph.ports import Port, output_ports, slot_sort_key


@dataclass(frozen=True)
class CompiledGraph:
    root: GraphRoot
    nodes: Mapping[str, GraphNode]
    node_order: tuple[str, ...]
    edges: tuple[Edge, ...]
    incoming: Mapping[str, tuple[Edge, ...]]
    ports: Mapping[str, Mapping[str, Port]]


def edge_order_key(edge: Edge) -> tuple:
    return (edge.target, slot_sort_key(edge.target_port), edge.source, edge.source_port)


def compile_graph(root: GraphRoot) -> CompiledGraph:
    nodes = {node.id: node for node in root.nodes}
    order = tuple(sorted(nodes))
    edges = tuple(sorted(root.edges, key=edge_order_key))
    incoming = {
        node_id: tuple(edge for edge in edges if edge.target == node_id)
        for node_id in order
    }
    port_map = {
        node.id: MappingProxyType({port.name: port for port in output_ports(node)})
        for node in root.nodes
    }
    return CompiledGraph(
        root,
        MappingProxyType(nodes),
        order,
        edges,
        MappingProxyType(incoming),
        MappingProxyType(port_map),
    )
