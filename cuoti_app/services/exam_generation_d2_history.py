"""Read exact-use facts already frozen into the task's history policy."""

from collections.abc import Mapping


HISTORY_EXACT_ORDER = ("history_exact_success_count", "history_latest_use_order")


def exact_history_usage(recent):
    scope = recent.get("scope")
    hits = recent.get("content_hash_hits")
    if recent.get("mode") != "audit_only" or not isinstance(scope, Mapping) or scope.get("generation_mode") != "past" or not isinstance(hits, list):
        return None
    return {row["content_hash"]: row for row in hits}
