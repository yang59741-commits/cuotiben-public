"""Stable public entry point for exam generation."""

from __future__ import annotations

from typing import Any

from .exam_generation_contracts import ExamGenerationResult, ExamGenerationRuntime
from .exams import generation


def generate_exam(payload: dict[str, Any], runtime: ExamGenerationRuntime) -> ExamGenerationResult:
    return generation.generate_exam(payload, runtime)


__all__ = ["generate_exam", "ExamGenerationResult", "ExamGenerationRuntime"]
