"""Deterministic normalization and dictionary expansion for semantic recall."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


SYNONYM_GROUPS = (
    ("极限", "limit"),
    ("导数", "微分", "derivative"),
    ("积分", "integral"),
    ("不定积分", "原函数"),
    ("定积分", "riemann积分"),
    ("线性代数", "linearalgebra"),
    ("特征值", "eigenvalue"),
    ("特征向量", "eigenvector"),
    ("微分方程", "ode", "differentialequation"),
    ("概率", "probability"),
    ("证明题", "证明", "proof"),
    ("计算题", "计算", "computation"),
    ("综合题", "综合", "comprehensive"),
)
UNKNOWN_CONCEPT_SENTINELS = frozenset({
    "未知", "未知知识点", "未知方法", "未知题型", "未分类", "不明确",
    "unknown", "n/a", "na", "none", "null",
})
TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.IGNORECASE)


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        char for char in text if char.isalnum() or "\u3400" <= char <= "\u9fff"
    )


def synonym_map() -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for group in SYNONYM_GROUPS:
        normalized = frozenset(filter(None, (normalize(item) for item in group)))
        for item in normalized:
            result[item] = normalized
    return result


SYNONYMS = synonym_map()


def canonical_map() -> dict[str, str]:
    """Map every synonym to the first, stable concept in its dictionary row."""

    result: dict[str, str] = {}
    for group in SYNONYM_GROUPS:
        concepts = tuple(filter(None, (normalize(item) for item in group)))
        if not concepts:
            continue
        canonical = concepts[0]
        for concept in concepts:
            previous = result.setdefault(concept, canonical)
            if previous != canonical:
                raise RuntimeError(f"同义词属于多个 canonical concept: {concept}")
    return result


CANONICAL_BY_TERM = canonical_map()


def canonical_concept(value: Any) -> str:
    """Return one normalized identity for a term or one of its synonyms."""

    term = normalize(value)
    return CANONICAL_BY_TERM.get(term, term)


def canonical_concepts(value: Any, *, limit: int | None = None) -> tuple[str, ...]:
    """Collapse expanded terms to ordered, unique concepts with an optional cap."""

    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
    ):
        raise ValueError("limit 必须是正整数")
    concepts: list[str] = []
    seen: set[str] = set()
    for term in normalized_terms(value):
        concept = canonical_concept(term)
        if not concept or concept in seen:
            continue
        seen.add(concept)
        concepts.append(concept)
        if limit is not None and len(concepts) >= limit:
            break
    return tuple(concepts)


def normalized_terms(value: Any) -> tuple[str, ...]:
    """Return deterministic full terms, word tokens, and Chinese bi-grams."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    terms: set[str] = set()
    full = normalize(text)
    if full:
        terms.add(full)
    for match in TOKEN_RE.findall(text):
        token = normalize(match)
        if not token:
            continue
        terms.add(token)
        if any("\u3400" <= char <= "\u9fff" for char in token) and len(token) > 2:
            terms.update(token[index:index + 2] for index in range(len(token) - 1))
    expanded = set(terms)
    for term in terms:
        expanded.update(SYNONYMS.get(term, ()))
    return tuple(sorted(expanded))


__all__ = [
    "CANONICAL_BY_TERM",
    "SYNONYM_GROUPS",
    "canonical_concept",
    "canonical_concepts",
    "canonical_map",
    "normalize",
    "normalized_terms",
    "synonym_map",
]
