"""Candidate-only D2 objective solver for the frozen Stage 5 graph.

This is deliberately independent of the historical score/penalty solver.  It
consumes a frozen graph and locally verified factor receipts, then emits a
versioned, reproducible receipt.  No provider, persistence, or rubric calls
are made here.
"""
from __future__ import annotations

import hashlib, json, re
from dataclasses import dataclass
from collections.abc import Callable, Collection
from functools import lru_cache
from typing import Any, Mapping, Sequence

from .exam_generation_stage5_solver import FrozenCandidateEdgeGraph, Stage5GlobalConstraintContract, Stage5InfeasibleError, _maximum_matching
from .exam_generation_stage6_objective import FACTOR_ORDER, CandidateBaseObjectiveError, validate_objective_policy, build_objective_receipt
from .exam_generation_d2_history import HISTORY_EXACT_ORDER, exact_history_usage
from .exam_generation_similarity import validate_similarity_freeze

D2_SOLVER_VERSION = "stage5-d2-lexicographic-solver-v3"
D2_V4_SOLVER_VERSION = "stage5-d2-lexicographic-solver-v4"
D2_LEGACY_SOLVER_VERSIONS = ("stage5-d2-lexicographic-solver-v1", "stage5-d2-lexicographic-solver-v2")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

class D2SolverError(ValueError):
    pass


class D2SearchIncompleteError(D2SolverError):
    """The exact search stopped before proving an optimum."""

    pass

def _remaining_d2_search(ctx, i, src, chap, cov):
    for source, target in ctx['source_targets'].items():
        possible = ctx['source_capacity'][source][i]
        if src.get(source, 0) > target or src.get(source, 0) + possible < target: return False
    for chapter, target in ctx['chapter_targets'].items():
        possible = ctx['chapter_capacity'][chapter][i]
        if chap.get(chapter, 0) > target or chap.get(chapter, 0) + possible < target: return False
    for minimum in ctx['minima']:
        possible = ctx['coverage_capacity'][minimum['id']][i]
        if cov.get(minimum['id'], 0) + possible < minimum['min_count']: return False
    demands = tuple(ctx['chapter_targets'][ch] - chap.get(ch, 0) for ch in ctx['chapters'])
    demands += (ctx['source_targets']['ordinary'] - src.get('ordinary', 0) - sum(demands),
                ctx['source_targets']['past_exam_other'] - src.get('past_exam_other', 0))
    key = (i, demands)
    if key not in ctx['quota_cache']:
        if any(n < 0 for n in demands) or sum(demands) != len(ctx['slots']) - i:
            ctx['quota_cache'][key] = False
        else:
            resources = [[{'candidate_id': f'{k}:{j}'} for j in range(n)] for k,n in enumerate(demands)]
            qe = {slot:[token for k in ctx['slot_resources'][slot] for token in resources[k]] for slot in ctx['slots'][i:]}
            ctx['quota_cache'][key] = len(_maximum_matching(ctx['slots'][i:], qe)[0]) == len(ctx['slots']) - i
    return ctx['quota_cache'][key]


def _d2_search_entry(ctx, i, chosen, src, chap, cov, objective, raw_vector):
    """Enter one search node without recursive Python calls.

    The former implementation used a deeply recursive closure.  On the
    production Python 3.11 build, repeated large replays could corrupt closure
    cells and even terminate the interpreter.  Keeping node state explicit and
    driving it from an iterative stack preserves the exact DFS order while
    avoiding that interpreter path.
    """
    guard = ctx["search_guard"]
    if guard is not None and not guard():
        raise D2SearchIncompleteError("d2_search_incomplete")
    if not _remaining_d2_search(ctx, i, src, chap, cov):
        return False
    upper = tuple(a + b for a, b in zip(objective, ctx["suffix_upper"][i]))
    best = ctx["best"]
    if best is not None:
        if upper < best[0]:
            return False
        if upper == best[0]:
            prefix = tuple(
                (edge["position"], edge["candidate_id"], edge["candidate_fact_hash"],
                 edge["content_hash"], edge["bucket_rank"])
                for edge in chosen
            )
            if prefix > best[1][:len(prefix)]:
                return False
    if i != len(ctx["slots"]):
        return True
    if any(
        src.get(key, 0) != value
        for key, value in ctx["source_targets"].items()
    ) or any(
        chap.get(key, 0) != value
        for key, value in ctx["chapter_targets"].items()
    ):
        return False
    if any(
        cov.get(minimum["id"], 0) < minimum["min_count"]
        for minimum in ctx["minima"]
    ):
        return False
    ordered = sorted(chosen, key=lambda edge: edge["position"])
    tie = tuple(
        (edge["position"], edge["candidate_id"], edge["candidate_fact_hash"],
         edge["content_hash"], edge["bucket_rank"])
        for edge in ordered
    )
    if best is None or objective > best[0] or (
        objective == best[0] and tie < best[1]
    ):
        ctx["best"] = (objective, tie, tuple(raw_vector), ordered)
    return False


def _undo_d2_edge(ctx, edge, chosen, used, used_content, src, chap, exams, cov):
    """Undo one candidate mutation made by the iterative DFS."""
    for minimum in ctx["minima"]:
        if edge.get(minimum["field"]) == minimum["value"]:
            cov[minimum["id"]] -= 1
    source_exam = edge.get("source_exam_id")
    if source_exam is not None:
        exams[source_exam] -= 1
    if edge["source_kind"] == "ordinary":
        chap[edge["chapter"]] -= 1
    src[edge["source_kind"]] -= 1
    used_content.remove(edge["content_hash"])
    used.remove(edge["candidate_id"])
    chosen.pop()


def _run_d2_search(ctx, factor_dimensions):
    """Run the exact candidate DFS using an explicit stack.

    Frames contain the node depth, next domain index, objective state, raw
    vector, and the edge selected to enter the node.  A frame is popped only
    after its domain is exhausted, which is equivalent to the old recursive
    ``visit``/finally-style backtracking but keeps the Python call stack flat.
    """
    slots = ctx["slots"]
    domains = ctx["domains"]
    chosen = []
    used = set()
    used_content = set()
    src = {}
    chap = {}
    exams = {}
    cov = {}
    objective = (0,) * (factor_dimensions + 1)
    raw_vector = (0,) * factor_dimensions
    if not _d2_search_entry(
        ctx, 0, chosen, src, chap, cov, objective, raw_vector
    ):
        return
    # [depth, next-domain-index, objective, raw-vector, incoming-edge]
    stack = [[0, 0, objective, raw_vector, None]]
    while stack:
        frame = stack[-1]
        depth = frame[0]
        domain = domains[slots[depth]]
        if frame[1] >= len(domain):
            stack.pop()
            incoming = frame[4]
            if incoming is not None:
                _undo_d2_edge(
                    ctx, incoming, chosen, used, used_content, src, chap,
                    exams, cov
                )
            continue
        edge = domain[frame[1]]
        frame[1] += 1
        candidate_id = edge["candidate_id"]
        content_hash = edge["content_hash"]
        source_kind = edge["source_kind"]
        if candidate_id in used or content_hash in used_content:
            continue
        if src.get(source_kind, 0) >= ctx["source_targets"].get(
            source_kind, len(slots)
        ):
            continue
        chapter = edge["chapter"]
        if (
            source_kind == "ordinary"
            and chapter in ctx["chapter_targets"]
            and chap.get(chapter, 0) >= ctx["chapter_targets"][chapter]
        ):
            continue
        source_exam = edge.get("source_exam_id")
        if source_exam is not None and exams.get(source_exam, 0) >= ctx["max_exam"]:
            continue
        chosen.append(edge)
        used.add(candidate_id)
        used_content.add(content_hash)
        src[source_kind] = src.get(source_kind, 0) + 1
        if source_kind == "ordinary":
            chap[chapter] = chap.get(chapter, 0) + 1
        if source_exam is not None:
            exams[source_exam] = exams.get(source_exam, 0) + 1
        for minimum in ctx["minima"]:
            if edge.get(minimum["field"]) == minimum["value"]:
                cov[minimum["id"]] = cov.get(minimum["id"], 0) + 1
        oriented_delta, raw_delta = ctx["contributions"][
            (edge["slot_id"], candidate_id)
        ]
        if ctx["similarity_enabled"]:
            pair_count = 0
            for previous in chosen[:-1]:
                if tuple(sorted((previous["candidate_id"], candidate_id))) in ctx["near_pair_keys"]:
                    pair_count += 1
            if pair_count:
                oriented_delta = (
                    *oriented_delta[:2], oriented_delta[2] - pair_count,
                    *oriented_delta[3:]
                )
                raw_delta = (
                    *raw_delta[:1], raw_delta[1] - pair_count,
                    *raw_delta[2:]
                )
        child_objective = tuple(
            a + b for a, b in zip(frame[2], oriented_delta)
        )
        child_raw = tuple(a + b for a, b in zip(frame[3], raw_delta))
        if _d2_search_entry(
            ctx, depth + 1, chosen, src, chap, cov, child_objective, child_raw
        ):
            stack.append([depth + 1, 0, child_objective, child_raw, edge])
        else:
            _undo_d2_edge(
                ctx, edge, chosen, used, used_content, src, chap, exams, cov
            )

def _canon(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
def _hash(v: Any) -> str:
    return hashlib.sha256(_canon(v).encode()).hexdigest()


def _hex64(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise D2SolverError(f"d2_{field}_invalid")
    return value

@dataclass(frozen=True, slots=True)
class D2Selection:
    solver_version: str
    graph_hash: str
    constraint_hash: str
    objective_hash: str
    selection_hash: str
    objective_vector: tuple[int, ...]
    assignments: tuple[Mapping[str, Any], ...]
    _payload_json: str
    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._payload_json)

def _factor_rows(receipts: Sequence[Mapping[str, Any]], graph_edges: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]):
    if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence) or not receipts:
        raise D2SolverError("d2_factor_receipts_invalid")
    by_key = {}
    by_id = {}
    for row in receipts:
        if not isinstance(row, Mapping) or set(row) not in ({"candidate_id", "candidate_fact_hash", "content_hash", "factors"}, {"candidate_id", "candidate_fact_hash", "factors"}):
            raise D2SolverError("d2_factor_receipt_shape_invalid")
        cid, fh = row["candidate_id"], row["candidate_fact_hash"]
        ch = row.get("content_hash")
        if ch is None:
            matches = {e["content_hash"] for e in graph_edges if e.get("candidate_id") == cid and e.get("candidate_fact_hash") == fh}
            if len(matches) != 1:
                raise D2SolverError("d2_factor_identity_ambiguous")
            ch = next(iter(matches))
        if not isinstance(cid, str) or not cid.strip():
            raise D2SolverError("d2_factor_identity_invalid")
        _hex64(fh, "candidate_fact_hash")
        _hex64(ch, "content_hash")
        try:
            # Reuse the strict factor validation and unknown fail-closed policy.
            from .exam_generation_stage6_objective import build_objective_receipt
            build_objective_receipt([{"candidate_id": cid, "candidate_fact_hash": fh, "factors": row["factors"]}], policy=policy, mode="d2")
        except CandidateBaseObjectiveError as exc:
            raise D2SolverError(str(exc)) from exc
        if cid in by_id and by_id[cid] != row:
            raise D2SolverError("d2_factor_identity_ambiguous")
        by_id[cid] = row
        by_key[(cid, fh, ch)] = row
    result = {}
    graph_keys = set()
    for edge in graph_edges:
        if edge.get("source_kind") not in {"ordinary", "past_exam_other"}:
            raise D2SolverError("d2_source_kind_invalid")
        key = (edge["candidate_id"], edge["candidate_fact_hash"], edge["content_hash"])
        graph_keys.add(key)
        row = by_key.get(key)
        if row is None:
            raise D2SolverError("d2_factor_receipt_missing_or_hash_drift")
        result[(edge["slot_id"], edge["candidate_id"])] = row
    if set(by_key) != graph_keys:
        raise D2SolverError("d2_factor_receipt_graph_coverage_invalid")
    return result

def solve_stage5_d2_selection(
    contract: Stage5GlobalConstraintContract,
    graph: FrozenCandidateEdgeGraph,
    *,
    factor_receipts: Sequence[Mapping[str, Any]] | None = None,
    objective_receipt: Mapping[str, Any] | None = None,
    objective_policy: Mapping[str, Any],
    target_mode: str = "free",
    m_once_eligible_identities: Collection[tuple[str, str, str]] | None = None,
    search_guard: Callable[[], bool] | None = None,
    solver_version: str | None = None,
) -> D2Selection:
    version = solver_version or D2_SOLVER_VERSION
    if version not in (D2_SOLVER_VERSION, D2_V4_SOLVER_VERSION, *D2_LEGACY_SOLVER_VERSIONS):
        raise D2SolverError("d2_solver_version_invalid")
    if not isinstance(contract, Stage5GlobalConstraintContract) or not isinstance(graph, FrozenCandidateEdgeGraph):
        raise D2SolverError("d2_stage5_input_invalid")
    policy = validate_objective_policy(objective_policy)
    payload = graph.to_dict()
    edges = payload.get("edges")
    if not isinstance(edges, list) or len(edges) > 500 or graph.constraint_hash != contract.constraint_hash or _hash(payload) != graph.graph_hash:
        raise D2SolverError("d2_graph_identity_invalid")
    exact_order = version in (D2_SOLVER_VERSION, D2_V4_SOLVER_VERSION)
    similarity_receipt = None
    if version == D2_V4_SOLVER_VERSION:
        similarity_receipt = payload.get("similarity_receipt")
        if similarity_receipt is None:
            raise D2SolverError("d2_similarity_freeze_missing")
        try:
            similarity_receipt = validate_similarity_freeze(
                similarity_receipt, graph=payload, subject=str(contract.to_dict().get("subject") or "")
            )
        except (TypeError, ValueError) as exc:
            raise D2SolverError(str(exc)) from exc
    near_pair_keys = frozenset()
    if similarity_receipt is not None:
        near_pair_keys = frozenset(
            tuple(sorted((row["left_candidate_id"], row["right_candidate_id"])))
            for row in similarity_receipt.get("pairs", ())
            if row.get("near_duplicate") is True
        )
    if target_mode not in {"free", "mistakes", "past"}:
        raise D2SolverError("d2_target_mode_invalid")
    if search_guard is not None and not callable(search_guard):
        raise D2SolverError("d2_search_guard_invalid")
    if factor_receipts is None:
        if not isinstance(objective_receipt, Mapping) or not isinstance(objective_receipt.get("candidates"), list):
            raise D2SolverError("d2_factor_receipt_missing")
        factor_receipts = objective_receipt["candidates"]
    if objective_receipt is not None:
        if objective_receipt.get("version") != "candidate-base-objective-v1" or objective_receipt.get("policy_hash") != policy["policy_hash"]:
            raise D2SolverError("d2_objective_identity_invalid")
        if objective_receipt.get("mode", "free") != target_mode:
            raise D2SolverError("d2_objective_mode_mismatch")
        try:
            projected = [{k: row[k] for k in ("candidate_id", "candidate_fact_hash", "factors")} for row in factor_receipts]
            rebuilt = build_objective_receipt(projected, policy=policy, mode=objective_receipt.get("mode", "d2"))
        except CandidateBaseObjectiveError as exc:
            raise D2SolverError(str(exc)) from exc
        if objective_receipt.get("objective_hash") != rebuilt["objective_hash"]:
            raise D2SolverError("d2_objective_hash_drift")
    factors = _factor_rows(factor_receipts, edges, policy)
    c = contract.to_dict()
    exact_usage = exact_history_usage(c.get("recent_usage", {}).get("payload", {})) if exact_order else None
    if exact_usage is not None and target_mode != "past":
        raise D2SolverError("d2_history_mode_mismatch")
    factor_dimensions = len(FACTOR_ORDER) + (len(HISTORY_EXACT_ORDER) if exact_order else 0)
    if similarity_receipt is not None:
        factor_dimensions += 3
    slot_rows = c.get("slots", [])
    if not isinstance(slot_rows, list) or any(not isinstance(row, Mapping) for row in slot_rows):
        raise D2SolverError("d2_slots_invalid")
    slot_rows = sorted(slot_rows, key=lambda row: (row.get("position"), row.get("slot_id")))
    slots = [s["slot_id"] for s in slot_rows]
    if not slots or len(set(slots)) != len(slots):
        raise D2SolverError("d2_slots_invalid")
    domains = {s: [] for s in slots}
    for e in edges:
        if e.get("slot_id") in domains:
            domains[e["slot_id"]].append(e)
    if any(not domains[slot] for slot in slots):
        raise D2SolverError("d2_hard_constraints_infeasible")
    for slot in slots:
        domains[slot].sort(
            key=lambda edge: (
                edge["candidate_id"], edge["candidate_fact_hash"],
                edge["content_hash"], edge["bucket_rank"],
            )
        )

    graph_identities = {
        (edge["candidate_id"], edge["candidate_fact_hash"], edge["content_hash"])
        for edge in edges
    }
    if m_once_eligible_identities is None:
        eligible: frozenset[tuple[str, str, str]] = frozenset()
        eligibility_status = "not_supplied"
    else:
        if version == D2_LEGACY_SOLVER_VERSIONS[0]:
            raise D2SolverError("d2_legacy_m_once_unsupported")
        if target_mode != "past" or isinstance(m_once_eligible_identities, (str, bytes)):
            raise D2SolverError("d2_m_once_eligibility_invalid")
        normalized = set()
        for identity in m_once_eligible_identities:
            if not isinstance(identity, (tuple, list)) or len(identity) != 3:
                raise D2SolverError("d2_m_once_eligibility_invalid")
            candidate_id, candidate_fact_hash, content_hash = identity
            if not isinstance(candidate_id, str) or not candidate_id:
                raise D2SolverError("d2_m_once_eligibility_invalid")
            normalized.add((candidate_id, _hex64(candidate_fact_hash, "m_once_candidate_fact_hash"), _hex64(content_hash, "m_once_content_hash")))
        if not normalized.issubset(graph_identities):
            raise D2SolverError("d2_m_once_identity_not_in_graph")
        eligible = frozenset(normalized)
        eligibility_status = "supplied" if eligible else "supplied_empty"
    eligibility_receipt = {
        "status": eligibility_status,
        "identities": [list(identity) for identity in sorted(eligible)],
    }
    eligibility_hash = _hash(eligibility_receipt)
    raw_source_targets = c.get("source_quotas", {})
    if not isinstance(raw_source_targets, Mapping):
        raise D2SolverError("d2_source_quota_invalid")
    source_targets = dict(raw_source_targets)
    if any(key not in {"ordinary", "past_exam", "past_exam_other"} for key in source_targets):
        raise D2SolverError("d2_source_quota_invalid")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in source_targets.values()):
        raise D2SolverError("d2_source_quota_invalid")
    if "past_exam" in source_targets:
        if "past_exam_other" in source_targets and source_targets["past_exam"] != source_targets["past_exam_other"]:
            raise D2SolverError("d2_source_quota_conflict")
        source_targets["past_exam_other"] = source_targets.get("past_exam_other", source_targets["past_exam"])
        source_targets.pop("past_exam", None)
    source_targets.setdefault("ordinary", 0)
    source_targets.setdefault("past_exam_other", 0)
    chapter_targets = c.get("ordinary_chapter_quotas", {})
    if not isinstance(chapter_targets, Mapping) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in chapter_targets.values()
    ):
        raise D2SolverError("d2_chapter_quota_invalid")
    chapter_targets = dict(chapter_targets)
    minima = c.get("coverage_policy", {}).get("payload", {}).get("minima", [])
    max_exam = c.get("diversity_policy", {}).get("payload", {}).get("max_per_source_exam", len(slots))
    if not isinstance(max_exam, int) or isinstance(max_exam, bool) or max_exam < 1:
        raise D2SolverError("d2_max_source_exam_invalid")
    if not isinstance(minima, list) or any(not isinstance(m, Mapping) or set(m) != {"id", "field", "value", "min_count"} or not isinstance(m["min_count"], int) or isinstance(m["min_count"], bool) or m["min_count"] < 0 or not isinstance(m["id"], str) or not m["id"] or not isinstance(m["field"], str) or m["field"] not in {"source_kind", "chapter", "question_type_family", "section_id"} or not isinstance(m["value"], str) for m in minima):
        raise D2SolverError("d2_coverage_minima_invalid")
    if len({m["id"] for m in minima}) != len(minima):
        raise D2SolverError("d2_coverage_minima_duplicate")
    def contribution(edge):
        row = factors[(edge["slot_id"], edge["candidate_id"])]
        raw = tuple(
            0 if row["factors"][factor]["level"] == "not_applicable"
            else policy["level_mapping"][factor][row["factors"][factor]["level"]]
            for factor in FACTOR_ORDER
        )
        oriented = tuple(
            value if policy["directions"][factor] == "max" else -value
            for value, factor in zip(raw, FACTOR_ORDER)
        )
        if exact_order:
            usage = exact_usage.get(edge["content_hash"], {}) if exact_usage is not None else {}
            enabled = row["factors"]["history_exposure_novelty"]["level"] != "not_applicable"
            history = (-usage.get("success_count", 0), usage.get("latest_order_rank", 0)) if enabled else (0, 0)
            raw = (*raw, *history)
            oriented = (*oriented, *history)
        if similarity_receipt is not None:
            historical = similarity_receipt.get("historical_near_usage", {}).get(edge["candidate_id"], {})
            if not isinstance(historical, Mapping):
                historical = {}
            near_count = historical.get("success_count", 0)
            near_order = historical.get("latest_order_rank", 0)
            if type(near_count) is not int or near_count < 0 or type(near_order) is not int or near_order < 0:
                raise D2SolverError("d2_similarity_history_invalid")
            # The same-paper pair count is added when an edge is selected; the
            # historical near-duplicate dimensions are edge-local.
            raw = (*raw[:1], 0, *raw[1:], -near_count, near_order)
            oriented = (*oriented[:1], 0, *oriented[1:], -near_count, near_order)
        identity = (edge["candidate_id"], edge["candidate_fact_hash"], edge["content_hash"])
        return ((1 if identity in eligible else 0), *oriented), raw

    contributions = {(edge['slot_id'], edge['candidate_id']): contribution(edge) for edge in edges}
    # Visit locally stronger candidates first so the branch-and-bound search
    # gets a useful incumbent before expanding lower-priority combinations.
    # The final tie-break is still evaluated explicitly at every leaf, so this
    # changes traversal cost, not the selected result.
    for slot in slots:
        domains[slot].sort(
            key=lambda edge: (
                tuple(
                    -value
                    for value in contributions[
                        (edge["slot_id"], edge["candidate_id"])
                    ][0]
                ),
                edge["candidate_id"],
                edge["candidate_fact_hash"],
                edge["content_hash"],
                edge["bucket_rank"],
            )
        )
    suffix_upper = [(0,) * (factor_dimensions + 1) for _ in range(len(slots) + 1)]
    for index in range(len(slots) - 1, -1, -1):
        # The objective is lexicographic, so its tight independent-slot upper
        # bound is the lexicographically best whole contribution tuple.  Taking
        # each dimension's maximum from a different edge is valid but extremely
        # loose (for example, combining a novel edge with an exposed edge's
        # latest-use rank), which needlessly expands millions of branches.
        maximum = max(
            contributions[(edge["slot_id"], edge["candidate_id"])][0]
            for edge in domains[slots[index]]
        )
        suffix_upper[index] = tuple(
            a + b for a, b in zip(maximum, suffix_upper[index + 1])
        )

    def suffix_capacity(matches):
        values = [0] * (len(slots) + 1)
        for index in range(len(slots) - 1, -1, -1):
            values[index] = values[index + 1] + int(any(matches(edge) for edge in domains[slots[index]]))
        return values

    source_capacity = {source: suffix_capacity(lambda edge, key=source: edge['source_kind'] == key)
                       for source in source_targets}
    chapter_capacity = {chapter: suffix_capacity(lambda edge, key=chapter: edge['source_kind'] == 'ordinary' and edge['chapter'] == key)
                        for chapter in chapter_targets}
    coverage_capacity = {m['id']: suffix_capacity(lambda edge, rule=m: edge.get(rule['field']) == rule['value'])
                        for m in minima}

    chapters = tuple(chapter_targets)
    chapter_indexes = {chapter: index for index, chapter in enumerate(chapters)}
    slot_resources = {
        slot: sorted({chapter_indexes.get(edge['chapter'], len(chapters))
                      if edge['source_kind'] == 'ordinary' else len(chapters) + 1
                      for edge in domains[slot]})
        for slot in slots
    }

    @lru_cache(maxsize=4096)
    def quotas_have_slots(i, demands):
        if any(count < 0 for count in demands) or sum(demands) != len(slots) - i:
            return False
        # One remaining slot cannot satisfy two chapter/source quotas.  This
        # relaxed matching ignores identity/diversity, so it can only prune
        # impossible quota assignments; full hard checks still run below.
        resources = [[{'candidate_id': f'{kind}:{j}'} for j in range(count)]
                     for kind, count in enumerate(demands)]
        quota_edges = {slot: [token for kind in slot_resources[slot] for token in resources[kind]]
                       for slot in slots[i:]}
        return len(_maximum_matching(slots[i:], quota_edges)[0]) == len(slots) - i

    def remaining_can_satisfy(i, src, chap, cov):
        for source, target in source_targets.items():
            possible = source_capacity[source][i]
            if src.get(source, 0) > target or src.get(source, 0) + possible < target:
                return False
        for chapter, target in chapter_targets.items():
            possible = chapter_capacity[chapter][i]
            if chap.get(chapter, 0) > target or chap.get(chapter, 0) + possible < target:
                return False
        for minimum in minima:
            possible = coverage_capacity[minimum['id']][i]
            if cov.get(minimum["id"], 0) + possible < minimum["min_count"]:
                return False
        chapter_demand = tuple(chapter_targets[ch] - chap.get(ch, 0) for ch in chapters)
        extra_ordinary = source_targets['ordinary'] - src.get('ordinary', 0) - sum(chapter_demand)
        other_demand = source_targets['past_exam_other'] - src.get('past_exam_other', 0)
        return quotas_have_slots(i, (*chapter_demand, extra_ordinary, other_demand))

    search_context = {
        "search_guard": search_guard, "source_capacity": source_capacity,
        "chapter_capacity": chapter_capacity, "coverage_capacity": coverage_capacity,
        "chapters": chapters, "slot_resources": slot_resources, "quota_cache": {},
        "suffix_upper": suffix_upper, "slots": slots, "domains": domains,
        "source_targets": source_targets, "chapter_targets": chapter_targets,
        "minima": minima, "max_exam": max_exam, "contributions": contributions,
        "similarity_enabled": similarity_receipt is not None,
        "near_pair_keys": near_pair_keys, "best": None,
    }
    _run_d2_search(search_context, factor_dimensions)
    best = search_context["best"]
    if best is None: raise D2SolverError("d2_hard_constraints_infeasible")
    objective_vector, _tie, vector, selected = best
    m_once_count = objective_vector[0]
    assignments = tuple({k: e[k] for k in ("slot_id","position","candidate_id","candidate_fact_hash","content_hash","source_kind","chapter","source_exam_id","section_id","question_type_family","target_difficulty","bucket_rank")} | {"factor_levels": {f: factors[(e["slot_id"],e["candidate_id"])] ["factors"][f]["level"] for f in FACTOR_ORDER}} for e in selected)
    objective_order = ["past_m_once", "verified_mistake_relevance"]
    if similarity_receipt is not None:
        objective_order.append("same_paper_near_duplicate_pairs")
    objective_order.append("history_exposure_novelty")
    objective_order.extend(HISTORY_EXACT_ORDER if exact_order else ())
    if similarity_receipt is not None:
        objective_order.extend(("history_near_duplicate_count", "history_near_duplicate_latest_use_order"))
    objective = {"version": version, "target_mode": target_mode, "policy_hash": policy["policy_hash"], "objective_order": objective_order, "m_once_count": m_once_count, "m_once_eligibility_hash": eligibility_hash, "m_once_eligibility_status": eligibility_status, "factor_order": list(FACTOR_ORDER), "vector": [m_once_count, *vector], "graph_hash": graph.graph_hash, "assignments": list(assignments)}
    if similarity_receipt is not None:
        objective["similarity_freeze_hash"] = similarity_receipt["freeze_hash"]
    if version == D2_LEGACY_SOLVER_VERSIONS[0]:
        objective = {"version": version, "policy_hash": policy["policy_hash"], "factor_order": list(FACTOR_ORDER), "vector": list(vector), "graph_hash": graph.graph_hash, "assignments": list(assignments)}
    objective["objective_hash"] = _hash(objective)
    factor_identity = []
    for row in factor_receipts:
        content_hash = row.get("content_hash")
        if content_hash is None:
            matches = {
                edge["content_hash"]
                for edge in edges
                if edge.get("candidate_id") == row.get("candidate_id")
                and edge.get("candidate_fact_hash") == row.get("candidate_fact_hash")
            }
            if len(matches) != 1:
                raise D2SolverError("d2_factor_identity_ambiguous")
            content_hash = next(iter(matches))
        factor_identity.append({
            "candidate_id": row["candidate_id"],
            "candidate_fact_hash": row["candidate_fact_hash"],
            "content_hash": content_hash,
            "factors": row["factors"],
        })
    factor_identity.sort(
        key=lambda row: (row["candidate_id"], row["candidate_fact_hash"], row["content_hash"])
    )
    identity = {"solver_version": version, "graph_hash": graph.graph_hash, "constraint_hash": contract.constraint_hash, "policy_hash": policy["policy_hash"], "factor_receipt_hash": _hash(factor_identity), "objective_hash": objective["objective_hash"], "target_mode": target_mode, "m_once_eligibility": eligibility_receipt, "assignments": list(assignments)}
    if similarity_receipt is not None:
        identity["similarity_freeze_hash"] = similarity_receipt["freeze_hash"]
    if version == D2_LEGACY_SOLVER_VERSIONS[0]:
        identity.pop("target_mode")
        identity.pop("m_once_eligibility")
    payload_out = {**identity, "objective_vector": list(vector) if version == D2_LEGACY_SOLVER_VERSIONS[0] else [m_once_count, *vector], "selection_hash": _hash(identity)}
    return D2Selection(version, graph.graph_hash, contract.constraint_hash, objective["objective_hash"], payload_out["selection_hash"], tuple(payload_out["objective_vector"]), assignments, _canon(payload_out))

# Descriptive aliases used by stage wiring and replay callers.
solve_d2_selection = solve_stage5_d2_selection
build_d2_selection_receipt = solve_stage5_d2_selection

__all__ = ["D2_SOLVER_VERSION", "D2_V4_SOLVER_VERSION", "D2_LEGACY_SOLVER_VERSIONS", "D2SolverError", "D2SearchIncompleteError", "D2Selection", "solve_stage5_d2_selection", "solve_d2_selection", "build_d2_selection_receipt"]
