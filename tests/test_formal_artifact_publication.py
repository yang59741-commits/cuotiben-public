"""Source identity and last-good publication are tested with tiny TeX trees."""
import json
from pathlib import Path

import pytest

from cuoti_app.services import semantic_index_sources as sources


def write_exam(root, institution, content, filename='2026真题.tex'):
    path = root / institution / '高等代数' / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\\documentclass{article}\n\\begin{document}\n'
        + f'\\section*{{{institution}2026年高等代数试卷}}\n'
        + content + '\n\\end{document}\n', encoding='utf-8')
    return path


@pytest.fixture
def formal(tmp_path):
    root, artifact = tmp_path / 'sources', tmp_path / 'formal.json'
    path = write_exam(root, '湖南师范大学', '一、计算测试题。\n二、证明测试结论。')
    first = sources.ensure_formal_past_exam_artifact(root, artifact_path=artifact, home_institution='湖南师范大学')
    return root, artifact, path, first


@pytest.mark.parametrize('content,filename', [
    ('一、计算矩阵 é 的秩。', '2026真题.tex'),
    ('一、计算矩阵 e\u0301 的秩。', '2026真题.tex'),
    ('一、证明集合 豈 的性质。', '2026真题.tex'),
    ('一、计算矩阵的秩。', '2026e\u0301真题.tex'),
])
def test_formal_artifact_roundtrip_preserves_existing_identity(formal, content, filename):
    root, artifact, _, first = formal
    path = write_exam(root, '大连理工大学', content, filename)
    built = sources.ensure_formal_past_exam_artifact(root, artifact_path=artifact, home_institution='湖南师范大学')
    loaded = sources.open_formal_past_exam_artifact(artifact)
    document = json.loads(artifact.read_text(encoding='utf-8'))
    record = next(item for item in document['candidates'] if item['source_kind'] == 'past_exam_other')
    candidate = next(item for item in loaded.candidates if item.source_kind == 'past_exam_other')
    assert candidate.candidate_id == sources._other_candidate_id(record['subject'], record['content'])
    assert candidate.content == record['content'].strip()
    assert candidate.source_file == path.relative_to(root).as_posix()
    assert loaded == built
    assert {item.candidate_id for item in first.candidates} <= {item.candidate_id for item in loaded.candidates}


def test_invalid_new_artifact_does_not_replace_last_good(formal, monkeypatch):
    root, artifact, _, first = formal
    original_bytes = artifact.read_bytes()
    write_exam(root, '大连理工大学', '一、计算另一道测试题。')
    enumerate_candidates = sources._enumerate_formal_candidates
    def invalid(*args, **kwargs):
        records = enumerate_candidates(*args, **kwargs)
        records[-1]['candidate_id'] = 'PE' + '0' * 24
        return records
    monkeypatch.setattr(sources, '_enumerate_formal_candidates', invalid)
    with pytest.raises(sources.SemanticIndexSourceError, match='候选身份校验失败'):
        sources.ensure_formal_past_exam_artifact(root, artifact_path=artifact,
                                               home_institution='湖南师范大学', allow_candidate_deletions=True)
    assert artifact.read_bytes() == original_bytes
    assert sources.open_formal_past_exam_artifact(artifact) == first
    assert not list(artifact.parent.glob('.formal.json.*.tmp'))


def test_atomic_replace_failure_keeps_last_good_and_removes_only_its_temp(formal, monkeypatch):
    root, artifact, _, first = formal
    original_bytes = artifact.read_bytes()
    write_exam(root, '大连理工大学', '一、计算另一道测试题。')
    def fail(source, destination):
        assert Path(destination) == artifact
        raise OSError('synthetic replace failure')
    monkeypatch.setattr(sources.os, 'replace', fail)
    with pytest.raises(sources.SemanticIndexSourceError) as caught:
        sources.ensure_formal_past_exam_artifact(root, artifact_path=artifact, home_institution='湖南师范大学')
    assert caught.value.code == 'ARTIFACT_PUBLISH_FAILED'
    assert artifact.read_bytes() == original_bytes
    assert sources.open_formal_past_exam_artifact(artifact) == first
    assert not list(artifact.parent.glob('.formal.json.*.tmp'))


def test_generic_json_writer_still_accepts_non_artifact_documents(tmp_path):
    path = tmp_path / 'receipt.json'
    sources._write_atomic_json(path, {'receipt': 'unchanged contract'})
    assert json.loads(path.read_text(encoding='utf-8')) == {'receipt':'unchanged contract'}
