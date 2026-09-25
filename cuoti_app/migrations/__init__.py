"""Database migration registry."""

from .v30_performance import migrate_v29_to_v30, schema_issues as v30_schema_issues
from .v31_semantic_shared_terms import (
    migrate_v30_to_v31,
    schema_issues as v31_schema_issues,
)
from .v32_statistics_read_model import (
    migrate_v31_to_v32,
    schema_issues as v32_schema_issues,
)
from .v33_daily_tips import migrate_v32_to_v33, schema_issues as v33_schema_issues
from .v34_ai_memory_cleanup import (
    migrate_v33_to_v34,
    schema_issues as v34_schema_issues,
)
from .v35_exam_job_deadlines import (
    migrate_v34_to_v35,
    schema_issues as v35_schema_issues,
)
from .v36_past_exam_profile_activation import (
    migrate_v35_to_v36,
    schema_issues as v36_schema_issues,
)
from .v37_job_input_immutability import (
    migrate_v36_to_v37,
    schema_issues as v37_schema_issues,
)
from .v38_job_input_payload_hash import (
    migrate_v37_to_v38,
    schema_issues as v38_schema_issues,
)
from .v39_exam_generation_attempt_binding import (
    migrate_v38_to_v39,
    schema_issues as v39_schema_issues,
)
from .v40_exam_generation_stage3_contract import (
    migrate_v39_to_v40,
    schema_issues as v40_schema_issues,
)
from .v41_exam_scoring_semantics import (
    migrate_v40_to_v41,
    schema_issues as v41_schema_issues,
)
from .v42_exam_generation_stage4_facts import (
    migrate_v41_to_v42,
    schema_issues as v42_schema_issues,
)
from .v43_exam_generation_stage5_persistence import (
    migrate_v42_to_v43,
    schema_issues as v43_schema_issues,
)
from .v44_exam_generation_stage6_persistence import (
    migrate_v43_to_v44,
    schema_issues as v44_schema_issues,
)
from .v45_exam_generation_stage7_persistence import (
    migrate_v44_to_v45,
    schema_issues as v45_schema_issues,
)
from .v46_exam_generation_v3_publish import (
    migrate_v45_to_v46,
    schema_issues as v46_schema_issues,
)
from .v47_exam_item_mistake_lineage import (
    migrate_v46_to_v47,
    schema_issues as v47_schema_issues,
)
from .v48_exam_job_publication_state import (
    migrate_v47_to_v48,
    schema_issues as v48_schema_issues,
)
from .v49_exam_generation_stage7_v2 import (
    migrate_v48_to_v49,
    schema_issues as v49_schema_issues,
)
from .v50_exam_generation_mode_memory_outbox import (
    migrate_v49_to_v50,
    schema_issues as v50_schema_issues,
)
from .v51_exam_generation_local_d2 import (
    migrate_v50_to_v51,
    schema_issues as v51_schema_issues,
)
from .versions import MIGRATIONS as LEGACY_MIGRATIONS
from .v52_exam_generation_soft_inputs import (
    migrate_v51_to_v52, schema_issues as v52_schema_issues,
)
from .v53_exam_generation_priority_once import (
    migrate_v52_to_v53, schema_issues as v53_schema_issues,
)
from .v54_exam_generation_memory_delivery import (
    migrate_v53_to_v54, schema_issues as v54_schema_issues,
)
from .v55_exam_generation_d2_content_rejections import (
    migrate_v54_to_v55, schema_issues as v55_schema_issues,
)
from .v56_exam_soft_delete import (
    migrate_v55_to_v56, schema_issues as v56_schema_issues,
)
from .v57_question_review_markers import (
    migrate_v56_to_v57, schema_issues as v57_schema_issues,
)
from .v58_target_relative_past_sources import (
    migrate_v57_to_v58, schema_issues as v58_schema_issues,
)
from .v59_daily_review import (
    migrate_v58_to_v59, schema_issues as v59_schema_issues,
)
from .versions import migration_schema_issues as legacy_schema_issues
from .v60_mistake_classification import migrate_v59_to_v60, schema_issues as v60_schema_issues
from .v61_review_practice import migrate_v60_to_v61, schema_issues as v61_schema_issues
from .v62_learning_evidence import migrate_v61_to_v62, schema_issues as v62_schema_issues
from .v63_daily_review_history import migrate_v62_to_v63, schema_issues as v63_schema_issues


MIGRATIONS = dict(LEGACY_MIGRATIONS)
MIGRATIONS[29] = (30, migrate_v29_to_v30)
MIGRATIONS[30] = (31, migrate_v30_to_v31)
MIGRATIONS[31] = (32, migrate_v31_to_v32)
MIGRATIONS[32] = (33, migrate_v32_to_v33)
MIGRATIONS[33] = (34, migrate_v33_to_v34)
MIGRATIONS[34] = (35, migrate_v34_to_v35)
MIGRATIONS[35] = (36, migrate_v35_to_v36)
MIGRATIONS[36] = (37, migrate_v36_to_v37)
MIGRATIONS[37] = (38, migrate_v37_to_v38)
MIGRATIONS[38] = (39, migrate_v38_to_v39)
MIGRATIONS[39] = (40, migrate_v39_to_v40)
MIGRATIONS[40] = (41, migrate_v40_to_v41)
MIGRATIONS[41] = (42, migrate_v41_to_v42)
MIGRATIONS[42] = (43, migrate_v42_to_v43)
MIGRATIONS[43] = (44, migrate_v43_to_v44)
MIGRATIONS[44] = (45, migrate_v44_to_v45)
MIGRATIONS[45] = (46, migrate_v45_to_v46)
MIGRATIONS[46] = (47, migrate_v46_to_v47)
MIGRATIONS[47] = (48, migrate_v47_to_v48)
MIGRATIONS[48] = (49, migrate_v48_to_v49)
MIGRATIONS[49] = (50, migrate_v49_to_v50)
MIGRATIONS[50] = (51, migrate_v50_to_v51)
MIGRATIONS[51] = (52, migrate_v51_to_v52)
MIGRATIONS[52] = (53, migrate_v52_to_v53)
MIGRATIONS[53] = (54, migrate_v53_to_v54)
MIGRATIONS[54] = (55, migrate_v54_to_v55)
MIGRATIONS[55] = (56, migrate_v55_to_v56)
MIGRATIONS[56] = (57, migrate_v56_to_v57)
MIGRATIONS[57] = (58, migrate_v57_to_v58)
MIGRATIONS[58] = (59, migrate_v58_to_v59)
MIGRATIONS[59] = (60, migrate_v59_to_v60)
MIGRATIONS[60] = (61, migrate_v60_to_v61)
MIGRATIONS[61] = (62, migrate_v61_to_v62)
MIGRATIONS[62] = (63, migrate_v62_to_v63)


def migration_schema_issues(db, target):
    if target == 63:
        return v60_schema_issues(db) + v61_schema_issues(db) + v62_schema_issues(db) + v63_schema_issues(db)
    if target == 62:
        return v60_schema_issues(db) + v61_schema_issues(db) + v62_schema_issues(db)
    if target == 61:
        return v60_schema_issues(db) + v61_schema_issues(db)
    if target == 60:
        return v60_schema_issues(db)
    if target == 59:
        return v59_schema_issues(db)
    if target == 58:
        return v58_schema_issues(db)
    issues = legacy_schema_issues(db, target)
    if target == 30:
        issues.extend(v30_schema_issues(db))
    if target == 31:
        issues.extend(v31_schema_issues(db))
    if target == 32:
        issues.extend(v32_schema_issues(db))
    if target == 33:
        issues.extend(v33_schema_issues(db))
    if target == 34:
        issues.extend(v34_schema_issues(db))
    if target == 35:
        issues.extend(v35_schema_issues(db))
    if target == 36:
        issues.extend(v36_schema_issues(db))
    if target == 37:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
    if target == 38:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
    if target == 39:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
        issues.extend(v39_schema_issues(db))
    if target == 40:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
        issues.extend(v39_schema_issues(db))
        issues.extend(v40_schema_issues(db))
    if target == 41:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
        issues.extend(v39_schema_issues(db))
        issues.extend(v40_schema_issues(db))
        issues.extend(v41_schema_issues(db))
    if target == 42:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
        issues.extend(v39_schema_issues(db))
        issues.extend(v40_schema_issues(db))
        issues.extend(v41_schema_issues(db))
        issues.extend(v42_schema_issues(db))
    if target == 43:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
        issues.extend(v39_schema_issues(db))
        issues.extend(v40_schema_issues(db))
        issues.extend(v41_schema_issues(db))
        issues.extend(v42_schema_issues(db))
        issues.extend(v43_schema_issues(db))
    if target == 44:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
        issues.extend(v39_schema_issues(db))
        issues.extend(v40_schema_issues(db))
        issues.extend(v41_schema_issues(db))
        issues.extend(v42_schema_issues(db))
        issues.extend(v43_schema_issues(db))
        issues.extend(v44_schema_issues(db))
    if target == 45:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
        issues.extend(v39_schema_issues(db))
        issues.extend(v40_schema_issues(db))
        issues.extend(v41_schema_issues(db))
        issues.extend(v42_schema_issues(db))
        issues.extend(v43_schema_issues(db))
        issues.extend(v44_schema_issues(db))
        issues.extend(v45_schema_issues(db))
    if target == 46:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
        issues.extend(v39_schema_issues(db))
        issues.extend(v40_schema_issues(db))
        issues.extend(v41_schema_issues(db))
        issues.extend(v42_schema_issues(db))
        issues.extend(v43_schema_issues(db))
        issues.extend(v44_schema_issues(db))
        issues.extend(v45_schema_issues(db))
        issues.extend(v46_schema_issues(db))
    if target == 47:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
        issues.extend(v39_schema_issues(db))
        issues.extend(v40_schema_issues(db))
        issues.extend(v41_schema_issues(db))
        issues.extend(v42_schema_issues(db))
        issues.extend(v43_schema_issues(db))
        issues.extend(v44_schema_issues(db))
        issues.extend(v45_schema_issues(db))
        issues.extend(v46_schema_issues(db))
        issues.extend(v47_schema_issues(db))
    if target == 48:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
        issues.extend(v39_schema_issues(db))
        issues.extend(v40_schema_issues(db))
        issues.extend(v41_schema_issues(db))
        issues.extend(v42_schema_issues(db))
        issues.extend(v43_schema_issues(db))
        issues.extend(v44_schema_issues(db))
        issues.extend(v45_schema_issues(db))
        issues.extend(v46_schema_issues(db))
        issues.extend(v47_schema_issues(db))
        issues.extend(v48_schema_issues(db))
    if target in {49, 50, 51, 52, 53, 54, 55, 56, 57}:
        issues.extend(v36_schema_issues(db))
        issues.extend(v37_schema_issues(db))
        issues.extend(v38_schema_issues(db))
        issues.extend(v39_schema_issues(db))
        issues.extend(v40_schema_issues(db))
        issues.extend(v41_schema_issues(db))
        issues.extend(v42_schema_issues(db))
        issues.extend(v43_schema_issues(db))
        issues.extend(v44_schema_issues(db))
        issues.extend(v46_schema_issues(db))
        issues.extend(v47_schema_issues(db))
        issues.extend(v48_schema_issues(db))
        issues.extend(v49_schema_issues(db))
    if target in {50, 51, 52, 53, 54, 55, 56, 57}:
        issues.extend(v50_schema_issues(db))
    if target in {51, 52, 53, 54, 55, 56, 57}:
        issues.extend(v51_schema_issues(db))
    if target in {52, 53, 54, 55, 56, 57}:
        issues.extend(v52_schema_issues(db))
    if target in {53, 54, 55, 56, 57}:
        issues.extend(v53_schema_issues(db))
    if target in {54, 55, 56, 57}:
        issues.extend(v54_schema_issues(db))
    if target in {55, 56, 57}:
        issues.extend(v55_schema_issues(db))
    if target in {56, 57}:
        issues.extend(v56_schema_issues(db))
    if target == 57:
        issues.extend(v57_schema_issues(db))
    return issues

__all__ = ["MIGRATIONS", "migration_schema_issues"]
