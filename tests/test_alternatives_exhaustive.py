"""Exhaustive verification of alternative (k-best) rankings.

The brute force here enumerates **every** legal alignment path recursively,
deduplicates them and sorts them by ``(total_cost, operation_string)`` with
the fixed operation order MATCH < DELETE < INSERT.  It shares no code with
the k-best dynamic program under test, so the complete top of the global
ranking is an independent oracle: the engine must reproduce every one of the
first ``alternative_limit + 1`` entries item by item without ever returning a
duplicate path.
"""

from __future__ import annotations

import itertools
import random

import pytest

from app.align import (
    DELETE_COST,
    DRIFT_TOLERANCE_MS,
    INSERT_COST,
    MATCH_MAX_DRIFT_MS,
    Item,
    align,
)

OP_RANK = {"MATCH": 0, "DELETE": 1, "INSERT": 2}
RANK_OP = {value: key for key, value in OP_RANK.items()}


def enumerate_paths(
    planned: list[Item], actual: list[Item]
) -> list[tuple[int, tuple[int, ...]]]:
    """Every distinct legal path as ``(total_cost, ranked op string)``,
    sorted by the global ordering ``(cost, op string)``."""
    paths: set[tuple[int, tuple[int, ...]]] = set()

    def rec(i: int, j: int, cost: int, ops: list[int]) -> None:
        if i == len(planned) and j == len(actual):
            paths.add((cost, tuple(ops)))
            return
        if i < len(planned) and j < len(actual):
            p, a = planned[i], actual[j]
            drift = abs(p.at_ms - a.at_ms)
            if p.code == a.code and drift <= MATCH_MAX_DRIFT_MS:
                rec(i + 1, j + 1, cost + drift, ops + [OP_RANK["MATCH"]])
        if i < len(planned):
            rec(i + 1, j, cost + DELETE_COST, ops + [OP_RANK["DELETE"]])
        if j < len(actual):
            rec(i, j + 1, cost + INSERT_COST, ops + [OP_RANK["INSERT"]])

    rec(0, 0, 0, [])
    return sorted(paths)


def _ops_of(pairs) -> tuple[int, ...]:
    return tuple(OP_RANK[pair.op] for pair in pairs)


def assert_alternatives_match_oracle(
    planned: list[Item], actual: list[Item], limit: int
) -> None:
    oracle = enumerate_paths(planned, actual)
    expected = oracle[: limit + 1]

    result = align(planned, actual, alternative_limit=limit)
    preferred_ops = _ops_of(result.pairs)
    assert result.alternatives is not None
    returned = [(result.total_cost, preferred_ops)] + [
        (alternative.total_cost, _ops_of(alternative.pairs))
        for alternative in result.alternatives
    ]

    # Item-by-item check of the leading ranks, shortages included.
    assert returned == expected, (planned, actual, limit)
    assert len(result.alternatives) == min(limit, max(0, len(oracle) - 1))

    # No path may appear twice.
    assert len({ops for _, ops in returned}) == len(returned)

    for rank, alternative in enumerate(result.alternatives, start=1):
        cost, ops = expected[rank]
        # Cost bookkeeping and gap relative to the unique preferred path.
        assert sum(pair.cost for pair in alternative.pairs) == cost
        assert alternative.cost_gap == cost - expected[0][0]
        assert alternative.cost_gap >= 0

        # First divergence: first position where the op strings separate.
        divergence = next(
            index
            for index, (preferred_op, op) in enumerate(zip(preferred_ops, ops))
            if preferred_op != op
        )
        assert alternative.first_divergence_index == divergence
        assert ops[:divergence] == preferred_ops[:divergence]
        assert ops[divergence] != preferred_ops[divergence]

        # The pairs describe exactly the enumerated op string and cover
        # every item once.
        planned_idx = [
            pair.planned_index
            for pair in alternative.pairs
            if pair.planned_index is not None
        ]
        actual_idx = [
            pair.actual_index
            for pair in alternative.pairs
            if pair.actual_index is not None
        ]
        assert planned_idx == list(range(len(planned)))
        assert actual_idx == list(range(len(actual)))

        # Compliance and the leftmost defect follow from the pairs.
        compliant = all(
            pair.op == "MATCH" and pair.drift_ms <= DRIFT_TOLERANCE_MS
            for pair in alternative.pairs
        )
        assert alternative.compliant is compliant
        if compliant:
            assert alternative.first_defect is None
        else:
            defect = alternative.first_defect
            assert defect is not None
            for earlier in alternative.pairs[: defect.pair_index]:
                assert earlier.op == "MATCH"
                assert earlier.drift_ms <= DRIFT_TOLERANCE_MS
            bad = alternative.pairs[defect.pair_index]
            if bad.op == "DELETE":
                assert defect.code == "MISS"
            elif bad.op == "INSERT":
                assert defect.code == "EXTRA"
            else:
                assert defect.code == "DRIFT"
                assert bad.drift_ms > DRIFT_TOLERANCE_MS
            assert RANK_OP[ops[defect.pair_index]] == bad.op


# Boundary-rich time grid; codes include a third letter to widen fork shapes.
GRID_TIMES = [0, 500, 2000, 2500]
GRID_CODES = ["A", "B"]


def _grid_sequences(max_len: int) -> list[list[Item]]:
    sequences: list[list[Item]] = [[]]
    for length in range(1, max_len + 1):
        for codes in itertools.product(GRID_CODES, repeat=length):
            for times in itertools.combinations(GRID_TIMES, length):
                sequences.append([Item(c, t) for c, t in zip(codes, times)])
    return sequences


SHORT_SEQUENCES = _grid_sequences(max_len=3)
LIMITS = [1, 2, 3, 20]


@pytest.mark.parametrize("p_idx", range(len(SHORT_SEQUENCES)))
@pytest.mark.parametrize("limit", LIMITS)
def test_short_sequence_ranks_match_enumerator(p_idx: int, limit: int) -> None:
    planned = SHORT_SEQUENCES[p_idx]
    for actual in SHORT_SEQUENCES:
        assert_alternatives_match_oracle(planned, actual, limit)


def test_seeded_fuzz_ranks_match_enumerator() -> None:
    rng = random.Random(20260915)
    for _ in range(300):
        n, m = rng.randrange(0, 6), rng.randrange(0, 6)
        # rng.sample yields distinct timestamps, matching the strict API.
        planned = [
            Item(rng.choice("AABBC"), t) for t in sorted(rng.sample(range(0, 9000, 250), n))
        ]
        actual = [
            Item(rng.choice("AABBC"), t) for t in sorted(rng.sample(range(0, 9000, 250), m))
        ]
        assert_alternatives_match_oracle(planned, actual, rng.randrange(1, 21))


def test_limit_larger_than_path_count_returns_all() -> None:
    # Only one legal path exists for two empty sequences.
    result = align([], [], alternative_limit=20)
    assert result.alternatives == ()
    # A single matching pair still allows MATCH, DELETE+INSERT and
    # INSERT+DELETE: three legal paths, so two alternatives even though the
    # limit asks for twenty.
    result = align([Item("A", 0)], [Item("A", 0)], alternative_limit=20)
    assert result.alternatives is not None
    assert len(result.alternatives) == 2


def test_omitted_limit_leaves_domain_contract() -> None:
    result = align([Item("A", 0)], [Item("A", 100)])
    assert result.alternatives is None


def test_unique_optimum_has_positive_gap() -> None:
    result = align([Item("A", 0)], [Item("A", 100)], alternative_limit=3)
    assert result.total_cost == 100
    assert _ops_of(result.pairs) == (OP_RANK["MATCH"],)
    # Both non-matching paths cost 5000; DELETE+INSERT ranks ahead.
    assert len(result.alternatives) == 2
    alternative = result.alternatives[0]
    assert _ops_of(alternative.pairs) == (OP_RANK["DELETE"], OP_RANK["INSERT"])
    assert alternative.total_cost == DELETE_COST + INSERT_COST
    assert alternative.cost_gap == DELETE_COST + INSERT_COST - 100
    assert alternative.cost_gap > 0
    assert alternative.first_divergence_index == 0
    assert result.alternatives[1].cost_gap == alternative.cost_gap


def test_duplicate_code_fork_has_zero_gap() -> None:
    planned = [Item("A", 0), Item("A", 1000)]
    actual = [Item("A", 500)]
    result = align(planned, actual, alternative_limit=2)
    assert result.total_cost == 3000
    preferred, alternative = result.alternatives[0], None
    assert _ops_of(result.pairs) == (OP_RANK["MATCH"], OP_RANK["DELETE"])
    assert _ops_of(preferred.pairs) == (OP_RANK["DELETE"], OP_RANK["MATCH"])
    assert preferred.total_cost == 3000
    assert preferred.cost_gap == 0
    assert preferred.first_divergence_index == 0
    # The runner-up match pairs the later planned copy, keeping full pairs
    # internally consistent.
    match = preferred.pairs[1]
    assert match.op == "MATCH"
    assert match.planned_index == 1
    assert match.actual_index == 0


def test_three_way_tie_divergence_positions() -> None:
    planned = [Item("A", 0), Item("A", 1000), Item("A", 2000)]
    actual = [Item("A", 500), Item("A", 1500)]
    # limit=2 asks for the preferred path plus both equal-cost runners-up;
    # every later path is strictly more expensive.
    result = align(planned, actual, alternative_limit=2)
    assert len(result.alternatives) == 2
    assert all(alternative.cost_gap == 0 for alternative in result.alternatives)
    assert _ops_of(result.pairs) == (0, 0, 1)
    assert _ops_of(result.alternatives[0].pairs) == (0, 1, 0)
    assert result.alternatives[0].first_divergence_index == 1
    assert _ops_of(result.alternatives[1].pairs) == (1, 0, 0)
    assert result.alternatives[1].first_divergence_index == 0


def test_repeated_calls_are_identical_with_alternatives() -> None:
    planned = [Item("A", 0), Item("A", 1000), Item("A", 2000)]
    actual = [Item("A", 500), Item("A", 1500)]
    first = align(planned, actual, alternative_limit=5)
    for _ in range(5):
        assert align(planned, actual, alternative_limit=5) == first
