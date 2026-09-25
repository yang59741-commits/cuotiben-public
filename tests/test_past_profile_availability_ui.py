from tests.frontend_sources import script_source
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cuoti_app" / "static" / "js" / "exam.js"


def _source():
    return script_source("exam.js")


def test_past_availability_is_explicit_and_never_defaults_missing_flags_true():
    source = _source()

    assert "raw?.usable!==false&&raw?.available!==false" not in source
    assert "source?.available===true" in source
    assert "source?.usable===true" in source
    assert "source?.generation_submission_supported===true" in source
    assert "raw?.source_available===true" in source
    assert "raw?.profile_selectable===true" in source
    assert "raw?.available===true&&raw?.usable===true" in source
    assert "const usable=sourceAvailable&&profileSelectable" in source


def test_default_order_comes_from_usable_combinations_not_institution_names():
    source = _source()

    assert "institutionPriority" not in source
    assert "['大连理工大学',0]" not in source
    assert "data.some(item=>" in source
    assert "pastExamSourceUsable(item)" in source
    assert "Number(secondUsable)-Number(firstUsable)" in source
    assert "Number(pastExamSourceUsable(second))-Number(pastExamSourceUsable(first))" in source
    assert "localeCompare(String(second.subject||''),'zh-CN')" in source


def test_unsupported_years_are_visible_but_disabled_with_backend_reason():
    source = _source()

    assert "function pastExamAvailabilityMessage(value)" in source
    assert "profile?.message" in source
    assert "value?.availability_reason??profile?.reason??profile?.code" in source
    assert "missing_active_profile_v3:'尚未激活该组合的正式画像'" in source
    assert "profile_abandoned:'画像建设已停止；现有有效题目仍保留为通用候选'" in source
    assert "const label=`${structure.year} 年${structure.usable?'':`（${reason}）`}`" in source
    assert "option.disabled=!structure.usable" in source
    assert "option.title=reason" in source
    assert "!item.usable&&item.availability_reason" in source


def test_active_hunan_2026_is_not_frontend_blocklisted_or_special_cased():
    source = _source()

    assert "湖南师范大学" not in source
    assert "2026" not in source[source.index("async function loadPastExamSources"):source.index("function updatePastCandidateNote")]
    assert "usableStructures[0]?.year" in source
    assert "usableStructures.some(structure=>structure.year===previousYear)" in source


def test_submission_captures_selection_then_precisely_blocks_unsupported_combo():
    source = _source()
    generation = source.split("async function generateExam(){", 1)[1].split(
        "\nasync function loadExamHistory", 1
    )[0]

    assert "const sid=document.getElementById('ep-subject').value" in generation
    assert "const institution=document.getElementById('ep-institution').value" in generation
    assert "const referenceYear=safeInteger(document.getElementById('ep-reference-year').value)" in generation
    assert ".find(item=>item.year===referenceYear);" in generation
    assert "if(!examPage.selection.pastExamSourceUsable(source)||!structure.usable)" in generation
    assert "structure.availability_reason||examPage.selection.pastExamAvailabilityMessage(source)" in generation
    assert "'当前组合不可提交'" in generation
    assert "payload={\n        source:'past'," in generation
    assert "institution,\n        reference_year:referenceYear" in generation
    assert "const modeInfo=structure.source_modes?.[candidateSource]" in generation
    assert "modeInfo.available===false" in generation


def test_selected_year_displays_escaped_reviewed_reason():
    source = _source()
    assert "year_selection_reason:String(raw?.year_selection_reason??'').trim()" in source
    assert "${escapeHTML(structure.year_selection_reason)}" in source
    assert "const learningYears=structure.learning_years||[]" in source
    assert "const learningYears=usableStructures" not in source


def test_source_mode_status_is_read_from_selected_year():
    source = _source()
    assert "const sourceModes=raw?.source_modes&&typeof raw.source_modes==='object'" in source
    assert "source_modes:sourceModes" in source
    assert "structure.source_modes?.[document.getElementById('ep-candidate-source')?.value]" in source
