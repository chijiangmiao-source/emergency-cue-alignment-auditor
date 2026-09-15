"""Weighted sequence alignment between a planned and an actual broadcast log.

Cost model
----------
- MATCH: a planned item and an actual item may match only when they carry the
  same ``code`` and their timestamps differ by at most ``MATCH_MAX_DRIFT_MS``
  (2000 ms).  The cost of a match is the absolute time difference.
- DELETE: a planned item with no counterpart costs ``DELETE_COST`` (2500).
- INSERT: an actual item with no counterpart costs ``INSERT_COST`` (2500).

The alignment minimises the total cost.  Ties are broken by the
lexicographic order of the complete operation string with the fixed
operation order MATCH < DELETE < INSERT, which makes the optimal path
unique even when codes repeat (an operation string uniquely determines a
path, and the lexicographically smallest one is well defined).

Candidate paths
---------------
When ``alternative_limit`` is given, the suffix dynamic program keeps at
most ``alternative_limit + 1`` best candidates per cell (the preferred path
plus up to ``alternative_limit`` alternatives).  A candidate is a node
holding its total suffix cost, the operation leaving the cell and a
reference to the suffix node in the successor cell.  Complete operation
strings are never copied: suffixes are shared, and because the three
leaving operations have distinct ranks, merging the ordered successor
streams only ever compares ``(cost, op_rank)``.  Full pairs are
materialised once, by backtracking the nodes that are actually reported.

Compliance
----------
An alignment is compliant only when the path consists solely of matches and
every match drifts by at most ``DRIFT_TOLERANCE_MS`` (500 ms).  Otherwise the
first defect scanning the path left to right is reported: ``MISS`` for a
deleted planned item, ``EXTRA`` for an inserted actual item and ``DRIFT``
for a match whose drift exceeds the tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass

MATCH_MAX_DRIFT_MS = 2000
DRIFT_TOLERANCE_MS = 500
DELETE_COST = 2500
INSERT_COST = 2500

OP_MATCH = "MATCH"
OP_DELETE = "DELETE"
OP_INSERT = "INSERT"

# Operation ranks double as the lexicographic order of operation strings.
_RANK_MATCH = 0
_RANK_DELETE = 1
_RANK_INSERT = 2
_RANK_TERMINAL = -1

DEFECT_MISS = "MISS"
DEFECT_EXTRA = "EXTRA"
DEFECT_DRIFT = "DRIFT"


@dataclass(frozen=True)
class Item:
    """A single planned or actual broadcast entry."""

    code: str
    at_ms: int


@dataclass(frozen=True)
class Pair:
    """One operation of the alignment path."""

    op: str
    cost: int
    code: str
    planned_index: int | None = None
    actual_index: int | None = None
    planned_at_ms: int | None = None
    actual_at_ms: int | None = None
    drift_ms: int | None = None


@dataclass(frozen=True)
class Defect:
    """The leftmost non-compliant operation of the path."""

    code: str
    pair_index: int
    pair: Pair


@dataclass(frozen=True)
class Alternative:
    """A runner-up complete alignment path."""

    total_cost: int
    cost_gap: int
    pairs: tuple[Pair, ...]
    compliant: bool
    first_defect: Defect | None
    first_divergence_index: int


@dataclass(frozen=True)
class Alignment:
    total_cost: int
    pairs: tuple[Pair, ...]
    compliant: bool
    first_defect: Defect | None
    alternatives: tuple[Alternative, ...] | None = None


@dataclass(frozen=True)
class _Node:
    """A candidate suffix path stored for one DP cell.

    Candidates of a cell are ordered by ``(total suffix cost, full operation
    string with MATCH < DELETE < INSERT)``.  The operation string is implicit:
    ``rank`` is the first operation and ``rest`` the chosen suffix candidate
    in the successor cell, so suffix nodes are shared instead of copied.
    ``rank`` is :data:`_RANK_TERMINAL` for the unique empty path at ``(n, m)``.
    """

    cost: int
    rank: int
    rest: _Node | None


def _match_drift(planned: Item, actual: Item) -> int | None:
    """Return the match cost when the two items may match, else ``None``."""
    if planned.code != actual.code:
        return None
    drift = abs(planned.at_ms - actual.at_ms)
    if drift > MATCH_MAX_DRIFT_MS:
        return None
    return drift


def _merge_streams(
    streams: list[tuple[int, int, list[_Node]]], keep: int
) -> list[_Node]:
    """Merge the ordered candidate streams leaving one cell.

    Each stream is ``(op_rank, edge_cost, successor_candidates)``.  Every
    stream stays ordered because its successor list is globally ordered and
    the edge prefix is fixed, and streams never share a first operation, so
    comparing heads only needs ``(total_cost, op_rank)``.  At most ``keep``
    nodes are created; successor nodes are referenced, never copied.
    """
    merged: list[_Node] = []
    # Heads: (rank, edge_cost, successors, position, head_key) or None when
    # the stream is exhausted.
    heads: list[tuple | None] = [
        (rank, edge_cost, successors, 0, (edge_cost + successors[0].cost, rank))
        for rank, edge_cost, successors in streams
        if successors
    ]
    for _ in range(keep):
        best_index = -1
        best_key: tuple[int, int] | None = None
        for index, head in enumerate(heads):
            if head is not None and (best_key is None or head[4] < best_key):
                best_key = head[4]
                best_index = index
        if best_index < 0:
            break
        rank, edge_cost, successors, position, _ = heads[best_index]  # type: ignore[misc]
        merged.append(_Node(best_key[0], best_key[1], successors[position]))  # type: ignore[index]
        position += 1
        if position < len(successors):
            heads[best_index] = (
                rank,
                edge_cost,
                successors,
                position,
                (edge_cost + successors[position].cost, rank),
            )
        else:
            heads[best_index] = None
    return merged


def _candidate_table(
    planned: list[Item], actual: list[Item], keep: int
) -> list[list[list[_Node]]]:
    """Suffix DP: ``table[i][j]`` holds the best ``keep`` candidates for
    aligning ``planned[i:]`` with ``actual[j:]``, ordered by
    ``(total cost, operation string)``."""
    n, m = len(planned), len(actual)
    table: list[list[list[_Node]]] = [
        [[] for _ in range(m + 1)] for _ in range(n + 1)
    ]
    terminal = _Node(0, _RANK_TERMINAL, None)
    table[n][m] = [terminal]
    for i in range(n - 1, -1, -1):
        suffix = table[i + 1][m][0]
        table[i][m] = [_Node(suffix.cost + DELETE_COST, _RANK_DELETE, suffix)]
    for j in range(m - 1, -1, -1):
        suffix = table[n][j + 1][0]
        table[n][j] = [_Node(suffix.cost + INSERT_COST, _RANK_INSERT, suffix)]
    for i in range(n - 1, -1, -1):
        p = planned[i]
        for j in range(m - 1, -1, -1):
            streams: list[tuple[int, int, list[_Node]]] = []
            drift = _match_drift(p, actual[j])
            if drift is not None:
                streams.append((_RANK_MATCH, drift, table[i + 1][j + 1]))
            streams.append((_RANK_DELETE, DELETE_COST, table[i + 1][j]))
            streams.append((_RANK_INSERT, INSERT_COST, table[i][j + 1]))
            table[i][j] = _merge_streams(streams, keep)
    return table


def _make_pair(rank: int, i: int, j: int, planned: list[Item], actual: list[Item]) -> Pair:
    if rank == _RANK_MATCH:
        p, a = planned[i], actual[j]
        drift = abs(p.at_ms - a.at_ms)
        return Pair(
            op=OP_MATCH,
            cost=drift,
            code=p.code,
            planned_index=i,
            actual_index=j,
            planned_at_ms=p.at_ms,
            actual_at_ms=a.at_ms,
            drift_ms=drift,
        )
    if rank == _RANK_DELETE:
        p = planned[i]
        return Pair(
            op=OP_DELETE,
            cost=DELETE_COST,
            code=p.code,
            planned_index=i,
            planned_at_ms=p.at_ms,
        )
    a = actual[j]
    return Pair(
        op=OP_INSERT,
        cost=INSERT_COST,
        code=a.code,
        actual_index=j,
        actual_at_ms=a.at_ms,
    )


def _backtrack(
    node: _Node, planned: list[Item], actual: list[Item]
) -> list[Pair]:
    """Materialise the pairs of one candidate node exactly once."""
    pairs: list[Pair] = []
    i = j = 0
    while node.rank != _RANK_TERMINAL:
        pairs.append(_make_pair(node.rank, i, j, planned, actual))
        if node.rank == _RANK_MATCH:
            i += 1
            j += 1
        elif node.rank == _RANK_DELETE:
            i += 1
        else:
            j += 1
        node = node.rest  # type: ignore[assignment]
    return pairs


def _first_defect(pairs: list[Pair]) -> Defect | None:
    for index, pair in enumerate(pairs):
        if pair.op == OP_DELETE:
            return Defect(code=DEFECT_MISS, pair_index=index, pair=pair)
        if pair.op == OP_INSERT:
            return Defect(code=DEFECT_EXTRA, pair_index=index, pair=pair)
        if pair.drift_ms is not None and pair.drift_ms > DRIFT_TOLERANCE_MS:
            return Defect(code=DEFECT_DRIFT, pair_index=index, pair=pair)
    return None


def _divergence_index(preferred: _Node, alternative: _Node) -> int:
    """First operation index at which the two candidate paths differ."""
    index = 0
    while (
        preferred.rank != _RANK_TERMINAL and alternative.rank != _RANK_TERMINAL
    ):
        if preferred.rank != alternative.rank:
            return index
        preferred = preferred.rest  # type: ignore[assignment]
        alternative = alternative.rest  # type: ignore[assignment]
        index += 1
    return index


def align(
    planned: list[Item],
    actual: list[Item],
    alternative_limit: int | None = None,
) -> Alignment:
    """Align ``planned`` against ``actual`` deterministically.

    With ``alternative_limit`` unset, only the preferred path is produced and
    the request/response contract stays unchanged.  When it is an integer in
    1..20, the suffix DP keeps that many plus one candidates per cell and the
    response carries the globally best runner-up paths.
    """
    keep = 1 if alternative_limit is None else alternative_limit + 1
    table = _candidate_table(planned, actual, keep)
    candidates = table[0][0]
    preferred_node = candidates[0]

    preferred_pairs = _backtrack(preferred_node, planned, actual)
    preferred_defect = _first_defect(preferred_pairs)

    alternatives: tuple[Alternative, ...] | None = None
    if alternative_limit is not None:
        produced: list[Alternative] = []
        for node in candidates[1 : alternative_limit + 1]:
            pairs = _backtrack(node, planned, actual)
            defect = _first_defect(pairs)
            produced.append(
                Alternative(
                    total_cost=node.cost,
                    cost_gap=node.cost - preferred_node.cost,
                    pairs=tuple(pairs),
                    compliant=defect is None,
                    first_defect=defect,
                    first_divergence_index=_divergence_index(
                        preferred_node, node
                    ),
                )
            )
        alternatives = tuple(produced)

    return Alignment(
        total_cost=preferred_node.cost,
        pairs=tuple(preferred_pairs),
        compliant=preferred_defect is None,
        first_defect=preferred_defect,
        alternatives=alternatives,
    )
