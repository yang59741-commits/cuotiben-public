from cuoti_app.services.exam_contracts import past_candidate_quotas


def test_hunan_2026_math_analysis_mixed_quota_is_target_scoped():
    assert past_candidate_quotas(
        "mixed",
        17,
        institution="湖南师范大学",
        subject="数学分析",
        reference_year=2026,
    ) == {"ordinary": 7, "past_exam": 10}


def test_mixed_quota_default_and_nearby_targets_remain_unchanged():
    expected = {"ordinary": 9, "past_exam": 8}
    assert past_candidate_quotas("mixed", 17) == expected
    assert past_candidate_quotas(
        "mixed", 17, institution="湖南师范大学", subject="高等代数", reference_year=2026
    ) == expected
    assert past_candidate_quotas(
        "mixed", 17, institution="湖南师范大学", subject="数学分析", reference_year=2025
    ) == expected


def test_non_mixed_sources_ignore_target_override():
    assert past_candidate_quotas(
        "ordinary", 17, institution="湖南师范大学", subject="数学分析", reference_year=2026
    ) == {"ordinary": 17}
    assert past_candidate_quotas(
        "other_past", 17, institution="湖南师范大学", subject="数学分析", reference_year=2026
    ) == {"past_exam": 17}
