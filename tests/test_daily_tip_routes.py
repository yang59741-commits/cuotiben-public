from cuoti_app import create_app


def test_daily_tip_crud_and_daily_card_use_the_saved_tip(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "daily-tips.db"),
            "PDF_DIR": str(tmp_path / "pdfs"),
            "REGISTER_JOB_HANDLERS": False,
            "JOB_WORKER_ENABLED": False,
        }
    )
    client = app.test_client()

    created = client.post(
        "/api/v2/daily-tips", json={"content": "  记住 $A^{-1}A=I$  "}
    )
    assert created.status_code == 201
    tip = created.get_json()
    assert tip["content"] == "记住 $A^{-1}A=I$"
    assert tip["revision"] == 1

    listing = client.get("/api/v2/daily-tips").get_json()
    assert listing["total"] == 1
    assert listing["items"] == [tip]
    daily = client.get("/api/daily").get_json()
    assert set(daily) == {"word", "word_desc", "quote", "tip"}
    assert daily["tip"] == tip["content"]

    updated = client.patch(
        f"/api/v2/daily-tips/{tip['public_id']}",
        json={"content": "更新后的提示", "revision": tip["revision"]},
    )
    assert updated.status_code == 200
    updated_tip = updated.get_json()
    assert updated_tip["content"] == "更新后的提示"
    assert updated_tip["revision"] == 2
    assert client.get("/api/daily").get_json()["tip"] == "更新后的提示"

    deleted = client.delete(
        f"/api/v2/daily-tips/{tip['public_id']}",
        json={"revision": updated_tip["revision"]},
    )
    assert deleted.status_code == 200
    assert deleted.get_json() == {"ok": True, "public_id": tip["public_id"]}
    assert client.get("/api/v2/daily-tips").get_json() == {
        "items": [],
        "next_before_id": None,
        "total": 0,
    }
    assert client.get("/api/daily").get_json()["tip"] == ""
