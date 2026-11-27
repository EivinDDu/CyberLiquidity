"""Small deterministic graph utilities (standard library only)."""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping


def find_cycle(graph: Mapping[str, Iterable[str]]) -> list[str] | None:
    """Return one directed cycle as ``[a, b, ..., a]``, or ``None`` if the graph is acyclic.

    ``graph`` maps each node to the nodes it points to. Edges to nodes that are not
    keys of ``graph`` are ignored (unknown references are reported elsewhere).
    Traversal order is sorted, so the reported cycle is deterministic.
    """
    adjacency = {node: sorted(set(targets)) for node, targets in graph.items()}
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(adjacency, white)
    for root in sorted(adjacency):
        if colour[root] != white:
            continue
        colour[root] = grey
        path = [root]
        stack = [iter(adjacency[root])]
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                colour[path.pop()] = black
                stack.pop()
                continue
            state = colour.get(nxt)
            if state is None:
                continue
            if state == grey:
                return path[path.index(nxt):] + [nxt]
            if state == white:
                colour[nxt] = grey
                path.append(nxt)
                stack.append(iter(adjacency[nxt]))
    return None


def topological_order(graph: Mapping[str, Iterable[str]]) -> list[str]:
    """Order nodes so that every node comes after all nodes it points to.

    With edges ``dependent -> dependency`` this yields dependencies first. Ties are
    broken alphabetically, so the order is deterministic. Raises ``ValueError`` on a
    cycle (scenario validation rejects cycles before this is reached).
    """
    adjacency = {node: sorted(set(targets)) for node, targets in graph.items()}
    pending = {node: sum(1 for t in targets if t in adjacency) for node, targets in adjacency.items()}
    dependents: dict[str, list[str]] = {node: [] for node in adjacency}
    for node, targets in adjacency.items():
        for target in targets:
            if target in adjacency:
                dependents[target].append(node)
    heap = [node for node, count in pending.items() if count == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        node = heapq.heappop(heap)
        order.append(node)
        for dependent in dependents[node]:
            pending[dependent] -= 1
            if pending[dependent] == 0:
                heapq.heappush(heap, dependent)
    if len(order) != len(adjacency):
        raise ValueError(f"dependency cycle among: {sorted(set(adjacency) - set(order))}")
    return order
