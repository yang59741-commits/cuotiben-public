"""Explicit source-flow preparation for exam generation."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .exam_contracts import (
    MAX_MISTAKE_REFERENCES,
    difficulty_contract, difficulty_quota,
    parse_exam_options,
    positive_int,
)
from .exam_coverage_policy import (
    MinimumCoverageTargets,
    preflight_minimum_coverage,
)
from .exam_local_repair import (
    FrozenGenerationEvidence,
    exact_adaptation_limit,
    freeze_generation_evidence,
)
from .exam_mistake_lineage_policy import (
    MistakeLineagePolicyError,
    MistakeLineageSlot,
    build_mistake_lineage_slots,
)
from .exam_prompting import build_structured_mistakes_prompts
from .learning_training import training_target_contract, validate_training_target
from .question_bank import extract_problems_from_tex


class SourceFlowError(ValueError):
    pass


_FROZEN_MISTAKE_FIELDS = (
    "id", "public_id", "revision", "subject_id", "chapter_id", "subject",
    "chapter", "qnum", "content", "note", "reasons", "source_kind",
    "source_ref", "difficulty", "fingerprint", "content_fingerprint",
    "content_fingerprint_version", "date_added", "updated_at", "status",
    "lapse_count", "correct_streak", "review_count",
)


@dataclass(frozen=True)
class MistakesFlowOutcome:
    mistakes: list[dict]
    subject_id: int
    subject: str
    chapters: list[str]
    difficulty: str
    target: str
    question_count: int
    question_types: list[str]
    note: str
    title: str
    exam_type: str
    system_prompt: str
    prompt: str
    max_adaptations: int
    frozen_evidence: FrozenGenerationEvidence
    minimum_coverage: MinimumCoverageTargets
    lineage_slots: tuple[MistakeLineageSlot, ...]
    public_id_by_internal_id: dict[int, str]
    memory_context: str = ""
    training_target: str | None = None


def prepare_mistakes_flow(
    payload,
    *,
    db,
    memory,
    chapter_sources,
    tex_base,
    logger,
    selection_seed=None,
    read_memory_context=None,
):
    training_target = validate_training_target(
        payload.get("training_target"), explicit="training_target" in payload
    )
    mistake_ids = payload.get("mistake_ids", [])
    if not isinstance(mistake_ids, list) or not mistake_ids:
        raise SourceFlowError("请选择至少一道错题")
    if len(mistake_ids) > MAX_MISTAKE_REFERENCES:
        raise SourceFlowError(f"一次最多选择 {MAX_MISTAKE_REFERENCES} 道错题")
    # Selection is a set-like UI operation; canonicalize IDs so chapter order
    # and deterministic reference sampling do not depend on click order.
    mids = sorted(positive_int(value, "错题 ID") for value in mistake_ids)
    if len(mids) != len(set(mids)):
        raise SourceFlowError("错题 ID 不能重复")
    difficulty, target, qcount, types, note = parse_exam_options(
        payload,
        default_count=len(mids),
    )
    # Reject an impossible mistake/type request before any database, memory,
    # question-bank or AI work.  Chapter coverage is frozen below after the
    # selected rows establish the authoritative chapter names.
    preflight_minimum_coverage(
        qcount,
        question_types=types,
        mistake_ids=mids,
    )
    rows = db.execute(
        "SELECT m.*,s.name as subject,c.name as chapter FROM mistakes m "
        "LEFT JOIN subjects s ON m.subject_id=s.id "
        "LEFT JOIN chapters c ON m.chapter_id=c.id "
        "WHERE m.deleted_at IS NULL AND m.id IN ({})".format(",".join("?" * len(mids))),
        mids,
    ).fetchall()
    mistake_map = {int(item["id"]): dict(item) for item in rows}
    if len(mistake_map) != len(mids):
        raise SourceFlowError("部分错题不存在")
    mistakes = [mistake_map[mid] for mid in mids]
    if any(
        item.get("subject_id") is None
        or item.get("chapter_id") is None
        or not item.get("subject")
        or not item.get("chapter")
        for item in mistakes
    ):
        raise SourceFlowError("错题的科目或章节关联无效")
    if len({item["subject_id"] for item in mistakes}) != 1:
        raise SourceFlowError("错题组卷只能选择同一科目的错题")

    subject = str(mistakes[0]["subject"])
    chapter_order = []
    seen_chapter_names = set()
    for mistake in mistakes:
        chapter = str(mistake.get("chapter") or "")
        if chapter not in seen_chapter_names:
            seen_chapter_names.add(chapter)
            chapter_order.append(chapter)
    minimum_coverage = preflight_minimum_coverage(
        qcount,
        question_types=types,
        chapters=chapter_order,
        mistake_ids=mids,
    )
    try:
        lineage_slots = build_mistake_lineage_slots(
            mistakes,
            question_count=qcount,
            allowed_question_types=types,
        )
    except MistakeLineagePolicyError as exc:
        raise SourceFlowError(str(exc)) from exc
    public_id_by_internal_id = {
        int(item["id"]): str(item.get("public_id") or "").strip()
        for item in mistakes
    }
    frozen_mistake_records = [
        {
            key: item.get(key)
            for key in _FROZEN_MISTAKE_FIELDS
            if item.get(key) not in (None, "")
        }
        for item in mistakes
    ]
    memory_filters = {
        "task": "exam_generation",
        "subject": subject,
        "chapters": [item["chapter"] for item in mistakes],
    }
    memory_context = (
        read_memory_context(
            memory, "mistakes", "mistake_exam", memory_filters
        )
        if callable(read_memory_context)
        else memory.read_context(
            "mistakes", "mistake_exam", memory_filters
        )
    )
    system_prompt, prompt = build_structured_mistakes_prompts(
        subject=subject,
        lineage_slots=tuple(slot.payload() for slot in lineage_slots),
        frozen_mistakes=frozen_mistake_records,
        difficulty_quota=difficulty_quota(qcount, difficulty),
        note=note,
        memory_context=str(memory_context or ""),
        training_target=training_target,
    )
    references = []
    reference_evidence = []
    seen_chapters = set()
    for mistake in mistakes:
        chapter = str(mistake.get("chapter") or "")
        key = (str(mistake.get("subject") or ""), chapter)
        if key in seen_chapters:
            continue
        seen_chapters.add(key)
        for filename in sorted(chapter_sources.get(key, [])):
            path = os.path.join(tex_base, filename)
            if not os.path.exists(path):
                continue
            try:
                extracted = extract_problems_from_tex(
                    path, 2, seed=selection_seed
                )
                for source_number, problem in enumerate(extracted, start=1):
                    content = str(problem)
                    references.append(f"[章节:{chapter}] {content}")
                    reference_evidence.append({
                        "candidate_id": (
                            f"mistake-reference:{subject}:{chapter}:"
                            f"{filename}:{source_number}"
                        ),
                        "source_kind": "ordinary",
                        "source_file": filename,
                        "source_number": source_number,
                        "subject": subject,
                        "chapter": chapter,
                        "content": content,
                    })
            except (OSError, UnicodeError, TypeError, ValueError) as exc:
                logger.warning(
                    "mistake exam reference extraction skipped: %s",
                    type(exc).__name__,
                )
            break
    reference_text = "\n\n".join(references[:8]) if references else "（无参考题，请自主出题）"
    mistake_summary = "\n".join(
        f"- [错题ID:{item.get('public_id') or item.get('id')}｜"
        f"{item.get('subject', '')}|{item.get('chapter', '')}] "
        f"{item.get('qnum', '')}\n题目正文：{str(item.get('content', ''))}\n"
        f"用户备注：{item.get('note', '')}\n用户选择的错因：{item.get('reasons', '')}"
        for item in mistakes
    )
    max_adaptations = exact_adaptation_limit(qcount)
    frozen_evidence = freeze_generation_evidence(
        "mistakes",
        expected_count=qcount,
        source_records=frozen_mistake_records,
        candidates=reference_evidence[:8],
        constraints={
            "subject": subject,
            "chapters": chapter_order or ["综合"],
            "question_types": list(types),
            "difficulty": difficulty,
            "target": target,
            "selected_mistake_ids": [
                str(item.get("public_id") or item.get("id")) for item in mistakes
            ],
            "max_adaptations": max_adaptations,
            **({"training_target": training_target_contract(training_target)}
               if training_target else {}),
        },
    )
    return MistakesFlowOutcome(
        mistakes=mistakes,
        subject_id=int(mistakes[0]["subject_id"]),
        subject=subject,
        chapters=chapter_order or ["综合"],
        difficulty=difficulty,
        target=target,
        question_count=qcount,
        question_types=types,
        note=note,
        title=f"错题针对性练习 - {subject} ({difficulty})",
        exam_type="错题组卷",
        system_prompt=system_prompt,
        prompt=prompt,
        max_adaptations=max_adaptations,
        frozen_evidence=frozen_evidence,
        minimum_coverage=minimum_coverage,
        lineage_slots=lineage_slots,
        public_id_by_internal_id=public_id_by_internal_id,
        memory_context=str(memory_context or ""),
        training_target=training_target,
    )


__all__ = ["MistakesFlowOutcome", "SourceFlowError", "prepare_mistakes_flow"]
