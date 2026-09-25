import json

from cuoti_app.services.exam_generation_past_v3_production import (
    CLASSIFICATION_RESPONSE_VERSION,
    VERIFICATION_RESPONSE_VERSION,
    _difficulty_callback,
    _difficulty_prompt,
)


def _request(round_name="classification"):
    version = (
        CLASSIFICATION_RESPONSE_VERSION
        if round_name == "classification"
        else VERIFICATION_RESPONSE_VERSION
    )
    return {
        "round": round_name,
        "response_schema_version": version,
        "candidates": [{"candidate_id": "cand-b"}, {"candidate_id": "cand-a"}],
    }


def test_difficulty_callback_uses_json_object_for_provider():
    captured = {}

    def provider(messages, **kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "schema_version": CLASSIFICATION_RESPONSE_VERSION,
                "round": "classification",
                "results": [
                    {"candidate_id": "cand-b", "difficulty": None, "confidence": 0},
                    {"candidate_id": "cand-a", "difficulty": "中等", "confidence": 0.8},
                ],
            },
            ensure_ascii=False,
        )

    callback = _difficulty_callback(provider, round_name="classification")
    result = callback(_request())

    assert result["round"] == "classification"
    assert captured["response_format"] == {"type": "json_object"}


def test_difficulty_prompt_pins_exact_output_and_forbidden_aliases():
    prompt = _difficulty_prompt("classification")
    assert '"schema_version"' in prompt and '"results"' in prompt
    assert '"candidate_id"' in prompt and '"confidence"' in prompt
    for forbidden in (
        "response_schema_version",
        "classifications",
        "request_hash",
        "type",
        "Markdown",
    ):
        assert forbidden in prompt


def test_difficulty_callback_chunks_large_candidate_sets_and_merges_results():
    calls = []
    candidates = [{"candidate_id": f"cand-{index}"} for index in range(35)]
    request = {
        "round": "verification",
        "response_schema_version": VERIFICATION_RESPONSE_VERSION,
        "candidates": candidates,
    }

    def provider(messages, **kwargs):
        payload = json.loads(messages[1]["content"].split("\n", 1)[1])
        calls.append((payload["candidates"], kwargs["response_format"]))
        return json.dumps(
            {
                "schema_version": VERIFICATION_RESPONSE_VERSION,
                "round": "verification",
                "results": [
                    {"candidate_id": row["candidate_id"], "difficulty": None, "confidence": 0}
                    for row in payload["candidates"]
                ],
            }
        )

    merged = _difficulty_callback(provider, round_name="verification")(request)
    assert [len(chunk) for chunk, _ in calls] == [16, 16, 3]
    assert all(fmt == {"type": "json_object"} for _, fmt in calls)
    assert [row["candidate_id"] for row in merged["results"]] == [
        row["candidate_id"] for row in candidates
    ]
