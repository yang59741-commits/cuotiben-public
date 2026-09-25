"""Stable API error-code vocabulary shared by service contracts.

Only codes that callers are expected to branch on belong here. Diagnostic
classes and internal failure details remain local to their owning service.
"""

VALIDATION = "VALIDATION"
AUTHENTICATION = "AUTHENTICATION"
REVISION_CONFLICT = "REVISION_CONFLICT"
QUESTION_BANK_BUILDING = "QUESTION_BANK_BUILDING"
QUESTION_BANK_BUILD_FAILED = "QUESTION_BANK_BUILD_FAILED"

STABLE_API_ERROR_CODES = frozenset(
    {
        VALIDATION,
        AUTHENTICATION,
        REVISION_CONFLICT,
        QUESTION_BANK_BUILDING,
        QUESTION_BANK_BUILD_FAILED,
    }
)

__all__ = [
    "AUTHENTICATION",
    "QUESTION_BANK_BUILD_FAILED",
    "QUESTION_BANK_BUILDING",
    "REVISION_CONFLICT",
    "STABLE_API_ERROR_CODES",
    "VALIDATION",
]
