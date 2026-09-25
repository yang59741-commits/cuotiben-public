from cuoti_app import create_app
from cuoti_app.services.settings import get_settings_service


def _app(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}\n", encoding="utf-8")
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "ui-contracts.db"),
            "PDF_DIR": str(tmp_path / "pdfs"),
            "SETTINGS_PATH": str(settings_path),
            "AI_MEMORY_DIR": str(tmp_path / "memory"),
            "QUESTION_BANK_DATABASE": str(tmp_path / "question-bank.db"),
            "REGISTER_JOB_HANDLERS": False,
            "JOB_WORKER_ENABLED": False,
        }
    )


def test_restored_mistake_list_contract_has_cursor_and_card_fields(tmp_path):
    client = _app(tmp_path).test_client()
    subjects = client.get("/api/subjects").get_json()
    subject_id = subjects[0]["id"]
    chapter_id = client.get(
        f"/api/chapters?subject_id={subject_id}"
    ).get_json()[0]["id"]

    created = client.post(
        "/api/v2/mistakes",
        json={
            "subject_id": subject_id,
            "chapter_id": chapter_id,
            "qnum": "1",
            "content": "Find $x$.",
            "reasons": ["概念不清"],
            "note": "card note",
            "date_added": "2026-09-09",
        },
    )
    assert created.status_code == 201

    response = client.get("/api/v2/mistakes?limit=1")
    assert response.status_code == 200
    payload = response.get_json()
    assert set(("items", "next_cursor", "has_more", "total", "search_backend")) <= set(payload)
    assert payload["total"] == 1
    assert payload["has_more"] is False
    assert payload["next_cursor"] is None
    card = payload["items"][0]
    assert set(
        (
            "id",
            "public_id",
            "revision",
            "subject",
            "chapter",
            "qnum",
            "content",
            "note",
            "reasons",
            "status",
            "date_added",
        )
    ) <= set(card)
    assert card["reasons"] == ["概念不清"]


def test_restored_settings_status_contracts_load_without_providers(
    tmp_path, monkeypatch
):
    app = _app(tmp_path)
    client = app.test_client()

    profile = client.get(
        "/api/v2/settings/ai/memory/profile",
        query_string={"memory_type": "free", "page": 1, "page_size": 20},
    )
    assert profile.status_code == 200
    profile_payload = profile.get_json()
    assert profile_payload["items"] == []
    assert profile_payload["page"] == 1
    assert profile_payload["page_size"] == 20
    assert profile_payload["total"] == 0
    assert profile_payload["has_next"] is False

    semantic = client.get("/api/v2/settings/semantic-index/overview")
    assert semantic.status_code == 200
    assert semantic.headers["Cache-Control"] == "no-store"
    semantic_payload = semantic.get_json()
    assert semantic_payload["schema"] == "semantic-index-operations-v1"
    assert isinstance(semantic_payload["settings_revision"], int)
    assert set(("settings", "settings_state", "job_state", "capabilities")) <= set(
        semantic_payload
    )

    cleanup = client.get("/api/v2/settings/semantic-index/cleanup-preview")
    assert cleanup.status_code == 200
    assert cleanup.get_json()["schema"] == "semantic-index-cleanup-preview-v1"

    settings_update = client.put(
        "/api/v2/settings/semantic-index",
        headers={"X-Cuoti-CSRF": "same-origin"},
        json={
            "version": semantic_payload["settings_revision"],
            "settings": semantic_payload["settings"],
            "activation": {
                "provider": False,
                "auto_refresh": False,
                "allow_partial": False,
            },
        },
    )
    assert settings_update.status_code == 200

    protected_action = client.post(
        "/api/v2/settings/semantic-index/actions/refresh",
        json={"version": semantic_payload["settings_revision"]},
    )
    assert protected_action.status_code == 403
    assert protected_action.get_json()["code"] == "semantic_csrf_rejected"

    service = get_settings_service(app)
    monkeypatch.setattr(
        service,
        "available_models",
        lambda: {
            "profile_id": "deepseek",
            "models": [],
            "recommendations": {},
        },
    )
    models = client.post("/api/v2/settings/ai/models")
    assert models.status_code == 200
    assert models.get_json() == {
        "profile_id": "deepseek",
        "models": [],
        "recommendations": {},
    }

    health = client.get("/api/v2/health")
    assert health.status_code == 200
    assert str(health.get_json()["contract_version"]) == "1"
    question_bank = client.get("/api/v2/question-bank/status")
    assert question_bank.status_code == 200
