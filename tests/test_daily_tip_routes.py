import pytest

from cuoti_app import create_app
from cuoti_app.services.daily_tips import (
    DailyTipError,
    build_daily_tips_latex,
    normalize_daily_tip,
)


USER_CASES_TIP = "\n".join(
    [
        r"\(\varlimsup_{n\to\infty}\sqrt[n]{a_n}=c, ",
        r"\quad\begin{cases}",
        r"c<1\text{ 时级数收敛},\\",
        r"c>1\text{ 时级数发散};",
        r"\end{cases}\)",
    ]
)


def _app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "daily-tips.db"),
            "PDF_DIR": str(tmp_path / "pdfs"),
            "REGISTER_JOB_HANDLERS": False,
            "JOB_WORKER_ENABLED": False,
        }
    )


def test_daily_tip_crud_and_daily_card_use_the_saved_tip(tmp_path):
    client = _app(tmp_path).test_client()

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


def test_daily_tip_accepts_and_keeps_user_cases_formula(tmp_path):
    client = _app(tmp_path).test_client()

    created = client.post("/api/v2/daily-tips", json={"content": USER_CASES_TIP})
    assert created.status_code == 201
    tip = created.get_json()
    assert tip["content"] == USER_CASES_TIP
    assert tip["revision"] == 1

    listing = client.get("/api/v2/daily-tips").get_json()
    assert listing["items"][0]["content"] == USER_CASES_TIP

    updated = client.patch(
        f"/api/v2/daily-tips/{tip['public_id']}",
        json={"content": USER_CASES_TIP, "revision": tip["revision"]},
    )
    assert updated.status_code == 200
    assert updated.get_json()["content"] == USER_CASES_TIP
    assert client.get("/api/v2/daily-tips").get_json()["items"][0]["content"] == USER_CASES_TIP

    section = build_daily_tips_latex([{"content": USER_CASES_TIP}])
    assert USER_CASES_TIP in section


ALLOWED_MATH_ENVIRONMENT_TIPS = [
    pytest.param(USER_CASES_TIP, id="cases"),
    pytest.param(r"\(\begin { cases } a & b \end { cases }\)", id="cases-with-whitespace"),
    pytest.param(r"\(\begin{aligned} a &= b \\ c &= d \end{aligned}\)", id="aligned"),
    pytest.param(r"\(\begin{matrix} a & b \\ c & d \end{matrix}\)", id="matrix"),
    pytest.param(r"\(\begin{array}{cc} a & b \\ c & d \end{array}\)", id="array"),
    pytest.param(r"\begin{gather} a = b \end{gather}", id="gather"),
    pytest.param(r"\begin{align*} a &= b \end{align*}", id="align-star"),
    pytest.param(r"\(\begin{split} a &= b \end{split}\)", id="split"),
    pytest.param(r"\(\begin{Vmatrix} a & b \\ c & d \end{Vmatrix}\)", id="Vmatrix"),
]


@pytest.mark.parametrize("content", ALLOWED_MATH_ENVIRONMENT_TIPS)
def test_normalize_daily_tip_allows_math_environments(content):
    assert normalize_daily_tip(content) == content


REJECTED_TIPS = [
    pytest.param(r"\(\begin{enumerate}\item 第一条\end{enumerate}\)", id="enumerate"),
    pytest.param(r"\begin{proof}显然\end{proof}", id="proof"),
    pytest.param(r"\(\begin{tabular}{cc} a & b \end{tabular}\)", id="unknown-environment"),
    pytest.param(
        r"\(\begin{cases} a & b \end{cases}\section{标题}\)",
        id="math-env-plus-section",
    ),
    pytest.param(
        r"\(\begin{aligned} a \end{aligned}\)\item 条目",
        id="math-env-plus-item",
    ),
    pytest.param(
        r"\(\begin{cases} a & b \end{cases}\)\newpage",
        id="math-env-plus-newpage",
    ),
    pytest.param(r"\section{标题}", id="section"),
    pytest.param(r"\item 条目", id="item"),
    pytest.param(r"\newpage", id="newpage"),
    pytest.param(r"\begin", id="bare-begin"),
    pytest.param(r"\end", id="bare-end"),
    pytest.param(r"\begin{cases", id="truncated-begin"),
    pytest.param(r"\end{cases", id="truncated-end"),
    pytest.param(r"\(\begin{cases} a \end{cases", id="unpaired-end"),
    pytest.param(r"\begin{document}", id="document-environment"),
    pytest.param(r"\input{other.tex}", id="input-command"),
    pytest.param(r"正确率 50% 以上", id="unescaped-percent"),
]


@pytest.mark.parametrize("content", REJECTED_TIPS)
def test_normalize_daily_tip_rejects_structural_and_unsafe_content(content):
    with pytest.raises(DailyTipError) as excinfo:
        normalize_daily_tip(content)
    assert excinfo.value.code == "VALIDATION"
    assert excinfo.value.status_code == 400


def test_normalize_daily_tip_structural_message_is_unchanged():
    with pytest.raises(DailyTipError) as excinfo:
        normalize_daily_tip(r"\item 条目")
    assert str(excinfo.value) == "每日提示不能包含章节、列表或分页等结构命令"


def test_normalize_daily_tip_percent_message_is_unchanged():
    with pytest.raises(DailyTipError) as excinfo:
        normalize_daily_tip(r"正确率 50% 以上")
    assert str(excinfo.value) == "百分号请写成 \\%"
