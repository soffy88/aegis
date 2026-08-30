"""Service Topology — Core data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import UUID


@dataclass
class ServiceNode:
    """服务节点"""

    id: UUID
    name: str
    kind: str  # "container" | "service" | "host" | "external"
    hostname: str
    port: int | None = None
    labels: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceEdge:
    """服务依赖边"""

    id: UUID
    source: ServiceNode  # 依赖方
    target: ServiceNode  # 被依赖方
    kind: str  # "depends_on" | "network" | "dns" | "config" | "volume"
    weight: float = 1.0  # 强度
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceGraph:
    """服务拓扑图"""

    nodes: dict[str, ServiceNode] = field(default_factory=dict)
    edges: list[ServiceEdge] = field(default_factory=list)

    def add_node(self, node: ServiceNode) -> None:
        self.nodes[node.name] = node

    def add_edge(self, edge: ServiceEdge) -> None:
        self.edges.append(edge)

    def has_cycle(self) -> bool:
        """检测是否存在循环依赖"""
        # 简实现：DFS 检测
        visited: set[str] = set()
        rec_stack: set[str] = set()

        def dfs(node_name: str) -> bool:
            visited.add(node_name)
            rec_stack.add(node_name)

            node = self.nodes.get(node_name)
            if node is None:
                return False

            for edge in self.edges:
                if edge.source.name == node_name:
                    next_node = edge.target.name
                    if next_node not in visited:
                        if dfs(next_node):
                            return True
                    elif next_node in rec_stack:
                        return True

            rec_stack.remove(node_name)
            return False

        for node_name in self.nodes:
            if node_name not in visited:
                if dfs(node_name):
                    return True
        return False

    def get_ancestors(self, node_name: str) -> set[str]:
        """获取某节点的所有先行依赖"""
        ancestors: set[str] = set()

        def walk(src: str):
            for edge in self.edges:
                if edge.target.name == node_name and edge.source.name == src:
                    ancestors.add(src)
                    walk(edge.source.name)

        walk(node_name)
        return ancestors

    def get_descendants(self, node_name: str) -> set[str]:
        """获取某节点的所有后代"""
        descendants: set[str] = set()

        def walk(tgt: str):
            for edge in self.edges:
                if edge.source.name == tgt and edge.target.name == node_name:
                    descendants.add(tgt)
                    walk(edge.target.name)

        # 反向遍历
        visited: set[str] = set()
        queue: list[str] = [node_name]
        while queue:
            current = queue.pop(0)
            for edge in self.edges:
                if edge.target.name == current and edge.source.name not in visited:
                    visited.add(edge.source.name)
                    descendants.add(edge.source.name)
                    queue.append(edge.source.name)
        return descendants