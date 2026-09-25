"""Maintenance-domain durable job handler factories."""

from __future__ import annotations

from .ai_memory import memory_from_app
from .jobs import JobOutcome


def make_memory_maintenance_handler(app):
    def memory_maintenance_handler(context, payload):
        del payload
        context.update_progress(0, 3, "正在整理 AI 记忆")
        memory = memory_from_app(app)
        archived = {}
        for index, memory_type in enumerate(("free", "mistakes", "past"), start=1):
            context.raise_if_cancelled()
            archived[memory_type] = memory.rollover_expired_days(memory_type)
            context.update_progress(index, 3, f"已整理 {memory_type} 记忆")
        return JobOutcome(result={"archived": archived})

    return memory_maintenance_handler


__all__ = ["make_memory_maintenance_handler"]
