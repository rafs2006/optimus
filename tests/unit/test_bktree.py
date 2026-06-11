"""Tests for the BK-tree, including equivalence to a linear scan."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from optimus.hashing.bktree import BKTree
from optimus.hashing.perceptual import hamming

_U64 = st.integers(min_value=0, max_value=(1 << 64) - 1)


def _linear_scan(values: list[int], query: int, radius: int) -> set[int]:
    return {v for v in values if hamming(v, query) <= radius}


@settings(max_examples=60, deadline=None)
@given(
    values=st.lists(_U64, min_size=1, max_size=120, unique=True),
    query=_U64,
    radius=st.integers(min_value=0, max_value=64),
)
def test_bktree_matches_linear_scan(values: list[int], query: int, radius: int) -> None:
    tree = BKTree()
    for v in values:
        tree.add(v)
    got = {m.value for m in tree.query(query, radius)}
    assert got == _linear_scan(values, query, radius)


def test_bktree_len_and_values() -> None:
    tree = BKTree()
    items = [1, 2, 4, 8, 16]
    for v in items:
        tree.add(v, payload=f"p{v}")
    assert len(tree) == len(items)
    assert set(tree.values()) == set(items)


def test_bktree_best_returns_closest() -> None:
    tree = BKTree()
    tree.add(0b0000, payload="zero")
    tree.add(0b0111, payload="three")
    best = tree.best(0b0001, radius=64)
    assert best is not None
    assert best.value == 0b0000
    assert best.distance == 1
    assert best.payload == "zero"


def test_bktree_best_none_when_outside_radius() -> None:
    tree = BKTree()
    tree.add((1 << 64) - 1)
    assert tree.best(0, radius=10) is None


def test_bktree_empty_query() -> None:
    tree = BKTree()
    assert tree.query(123, 5) == []
    assert list(tree.values()) == []


def test_query_negative_radius_raises() -> None:
    tree = BKTree()
    tree.add(1)
    import pytest

    with pytest.raises(ValueError):
        tree.query(1, -1)
