"""A BK-tree over 64-bit hashes for sub-linear Hamming-radius lookups."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from optimus.hashing.perceptual import hamming


@dataclass(slots=True)
class _Node:
    value: int
    payload: str | None
    children: dict[int, _Node] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Match:
    """A hash within the queried radius."""

    value: int
    distance: int
    payload: str | None


class BKTree:
    """Burkhard-Keller tree using Hamming distance as the metric.

    Lookups prune subtrees via the triangle inequality, giving roughly
    O(log n) behavior for small radii instead of a full linear scan.
    """

    def __init__(self) -> None:
        self._root: _Node | None = None
        self._size = 0

    def __len__(self) -> int:
        return self._size

    def add(self, value: int, payload: str | None = None) -> None:
        """Insert ``value`` (an unsigned 64-bit hash) with an optional payload."""
        if self._root is None:
            self._root = _Node(value=value, payload=payload)
            self._size += 1
            return
        node = self._root
        while True:
            dist = hamming(node.value, value)
            child = node.children.get(dist)
            if child is None:
                node.children[dist] = _Node(value=value, payload=payload)
                self._size += 1
                return
            node = child

    def query(self, value: int, radius: int) -> list[Match]:
        """Return all stored hashes within ``radius`` of ``value``."""
        if radius < 0:
            raise ValueError("radius must be >= 0")
        results: list[Match] = []
        if self._root is None:
            return results
        stack = [self._root]
        while stack:
            node = stack.pop()
            dist = hamming(node.value, value)
            if dist <= radius:
                results.append(Match(value=node.value, distance=dist, payload=node.payload))
            lo, hi = dist - radius, dist + radius
            for edge, child in node.children.items():
                if lo <= edge <= hi:
                    stack.append(child)
        return results

    def best(self, value: int, radius: int) -> Match | None:
        """Return the closest match within ``radius``, or ``None``."""
        matches = self.query(value, radius)
        if not matches:
            return None
        return min(matches, key=lambda m: m.distance)

    def values(self) -> Iterator[int]:
        """Yield every stored hash value (unordered)."""
        if self._root is None:
            return
        stack = [self._root]
        while stack:
            node = stack.pop()
            yield node.value
            stack.extend(node.children.values())
