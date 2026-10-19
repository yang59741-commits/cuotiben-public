"""Problem-library ingestion, OCR, and batch routes."""

import json
import os
import re
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request

from ..errors import PDF_OCR_DISABLED_CODE, PDF_OCR_DISABLED_MESSAGE

from ..database import get_db
from ..services.ai import DeepSeekConfigurationError, deepseek_api_call
from ..services.review_schedule import initial_review_date
from ..services.ocr import extract_docx_text
from ..services.tex import append_problem, append_problems
from ..validation import (
    ValidationError,
    date_value,
    json_object,
    mistake_fingerprint,
    positive_int,
    reasons_value,
    subject_chapter,
    text_value,
)

bp = Blueprint("library", __name__)


def _chapter_sources():
    return current_app.extensions["catalog_service"].chapter_sources


def _fallback_source(subject_name):
    return current_app.extensions["catalog_service"].fallback_source(subject_name)


def _ensure_fallback_file(tex_base, target):
    root = Path(tex_base).resolve()
    path = (root / target).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValidationError("综合题目目标路径无效") from exc
    if path.exists():
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=root
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\\documentclass{ctexart}\n\\begin{document}\n\\end{document}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _configured_tex_base() -> str:
    tex_base = str(current_app.config.get('TEX_BASE') or '').strip()
    if not tex_base:
        raise DeepSeekConfigurationError(
            '未配置普通试题库目录，请设置 CUOTI_TEX_BASE'
        )
    if not os.path.isdir(tex_base):
        raise DeepSeekConfigurationError(
            '普通试题库目录不存在，请检查 CUOTI_TEX_BASE'
        )
    return tex_base


def _service_error_response(error: Exception, action: str):
    current_app.logger.exception("%s failed", action)
    if isinstance(error, DeepSeekConfigurationError):
        return jsonify({'error': str(error)}), 503
    return jsonify({'error': f'{action}失败，请检查服务配置或稍后重试'}), 502


@bp.post('/internal/jobs/ocr-pdf')
def api_ocr_pdf():
    """Extract supported local document formats; remote PDF OCR is disabled."""
    if 'file' not in request.files:
        return jsonify({'error': '请上传文件'}), 400
    f = request.files['file']
    ext = (f.filename or '').rsplit('.',1)[-1].lower() if '.' in (f.filename or '') else ''
    if ext not in ('pdf','docx','tex','txt'):
        return jsonify({'error': '仅支持 PDF/DOCX/TeX/TXT 文件'}), 400
    if ext == 'pdf':
        f.close()
        return jsonify({
            'error': PDF_OCR_DISABLED_MESSAGE,
            'code': PDF_OCR_DISABLED_CODE,
        }), 410
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.'+ext)
    f.save(tmp.name); tmp.close()
    try:
        if os.path.getsize(tmp.name) > 20 * 1024 * 1024:
            return jsonify({'error': '上传文件不能超过 20 MB'}), 413
        if ext in ('tex','txt'):
            with open(tmp.name, 'r', encoding='utf-8', errors='replace') as fp:
                text = fp.read()
            os.unlink(tmp.name)
            return jsonify({'text': text, 'method': 'direct'})
        if ext == 'docx':
            text = extract_docx_text(tmp.name)
            os.unlink(tmp.name)
            return jsonify({'text': text, 'method': 'docx'})
    except Exception as e:
        current_app.logger.exception("File processing failed")
        return jsonify({'error': '文件处理失败，请检查文件格式或稍后重试'}), 500
    finally:
        f.close()
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)

@bp.post('/internal/jobs/batch-add-problems')
def api_batch_add_problems():
    try:
        d = json_object(request)
        text = text_value(
            d.get('content'), '题目内容', maximum=6000, required=True
        )
    except ValidationError as exc:
        return jsonify({'error': str(exc)}), 400
    try:
        tex_base = _configured_tex_base()
    except DeepSeekConfigurationError as exc:
        return jsonify({'error': str(exc)}), 503
    db = get_db()
    chs = [dict(r) for r in db.execute("SELECT c.id,c.name,c.subject_id,s.name as subject FROM chapters c JOIN subjects s ON c.subject_id=s.id").fetchall()]
    ch_list = '\n'.join([f'  {c["id"]}: [{c["subject"]}] {c["name"]}' for c in chs])
    prompt = "Split the following math text into individual problems. For each, determine subject and chapter.\n\nAvailable chapters:\n" + ch_list + "\n\nRules:\n1. Split by problem numbers, \\subsection, \\item, or blank lines\n2. Include only the complete question; exclude answers and solutions\n3. If problem spans multiple chapters, set chapter_id=0 and chapter to an empty string\n4. chapter must be the exact chapter name from Available chapters\n5. Remove non-problem text\n\nContent:\n" + text + "\n\nReturn a JSON array: [{\"content\":\"...\", \"subject_id\":1, \"chapter_id\":9, \"chapter\":\"函数与极限\"}]\nReturn ONLY the JSON array."
    try:
        result = deepseek_api_call([
            {'role': 'system', 'content': 'Fast splitter. Return ONLY JSON array.'},
            {'role': 'user', 'content': prompt}
        ], temperature=0.1, timeout=90, task='classification', task_policy='classification_batch')
        import re as _re
        arr_match = _re.search(r'\[.*\]', result, _re.DOTALL)
        if not arr_match:
            return jsonify({'error': 'AI response format error'}), 500
        problems = json.loads(arr_match.group())
        if not isinstance(problems, list) or len(problems) == 0:
            return jsonify({'error': 'No problems identified'}), 400
        if len(problems) > 100:
            return jsonify({'error': '一次最多添加 100 道题'}), 400
        prepared = []
        for index, problem in enumerate(problems, start=1):
            if not isinstance(problem, dict):
                raise ValidationError(f'第 {index} 题格式无效')
            subject, chapter = subject_chapter(
                db,
                problem.get('subject_id'),
                None
                if problem.get('chapter_id') in (None, '', 0, '0')
                else problem.get('chapter_id'),
            )
            content = text_value(
                problem.get('content'),
                f'第 {index} 题内容',
                maximum=100_000,
                required=True,
            )
            key = (subject['name'], chapter['name']) if chapter else None
            tex_files = _chapter_sources().get(key, []) if key else []
            target = tex_files[0] if tex_files else _fallback_source(subject['name'])
            if not tex_files:
                _ensure_fallback_file(tex_base, target)
            fpath = os.path.join(tex_base, target)
            if not os.path.isfile(fpath):
                raise ValidationError(f'第 {index} 题的目标文件不存在: {target}')
            prepared.append((problem, subject, chapter, content, target))
        assigned_numbers = append_problems(
            tex_base,
            [(target, content) for _, _, _, content, target in prepared],
            compile_check=current_app.config.get("LIBRARY_MINIMAL_COMPILE_CHECK"),
        )
        saved = []
        for (problem, subject, chapter, _content, target), next_num in zip(
            prepared, assigned_numbers
        ):
            saved.append({
                'filename': target,
                'num': next_num,
                'chapter': str(problem.get('chapter') or ''),
                'sid': subject['id'],
                'cid': chapter['id'] if chapter else 0,
            })
        return jsonify({'ok': True, 'count': len(saved), 'saved': saved})
    except ValidationError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return _service_error_response(e, '批量题库写入')

def _parse_problem_list(raw_result):
    """Accept a JSON array, fenced JSON, or {"problems": [...]}."""
    text = (raw_result or '').strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        text = '\n'.join(lines).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char not in '[{':
                continue
            try:
                candidate, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, list) or (
                isinstance(candidate, dict) and isinstance(candidate.get('problems'), list)
            ):
                parsed = candidate
                break

    if isinstance(parsed, dict):
        parsed = parsed.get('problems')
    if not isinstance(parsed, list):
        raise ValueError('AI 返回格式错误，未找到题目列表')
    return parsed


_QUESTION_START = re.compile(
    r'(?m)^(?=[ \t]*(?:'
    r'\\\[[ \t]*(?:\r?\n[ \t]*)?\\text[ \t]*\{[ \t]*\d{1,2}[.、．)]'
    r'|\\text[ \t]*\{[ \t]*\d{1,2}[.、．)]'
    r'|(?:题目|问题)\s*\d+'
    r'|第\s*\d+\s*题'
    r'|[（(]?\d{1,2}[）).、．]\s*'
    r'|%\s*\d+(?:[.、)]|\s+)'
    r'|\\subsection\b'
    r'|\\item\b'
    r'))'
)


def _split_numbered_input(text):
    """Conservatively find question boundaries without changing question text."""
    starts = []
    for match in _QUESTION_START.finditer(text):
        start = match.start()
        if starts and re.fullmatch(
            r'[ \t]*\\\[[ \t\r\n]*',
            text[starts[-1]:start],
        ):
            # A standalone display opener and its following \text{1. ...}
            # describe one boundary, not two.
            continue
        starts.append(start)
    if len(starts) < 2:
        return [text]

    # Parenthesized numbering after a common stem normally denotes subparts of
    # one problem, for example "证明：\n(1) ...\n(2) ...". Treating those
    # subparts as separate mistakes loses the shared assumptions and needlessly
    # forces a subject-selected single problem through AI classification.
    prefix = text[:starts[0]]
    subpart_numbers = []
    for start in starts:
        marker = re.match(
            r'[ \t]*[（(](\d{1,2})[）)]',
            text[start:],
        )
        if not marker:
            subpart_numbers = []
            break
        subpart_numbers.append(int(marker.group(1)))
    if (
        prefix.strip()
        and subpart_numbers == list(range(1, len(subpart_numbers) + 1))
    ):
        return [text]

    blocks = [
        text[start:(starts[index + 1] if index + 1 < len(starts) else len(text))].strip()
        for index, start in enumerate(starts)
    ]
    if prefix.strip():
        blocks[0] = prefix.rstrip() + '\n' + blocks[0]
    return [block for block in blocks if block]


def _extract_question_number(block):
    """Read a leading question number without modifying the saved source."""
    patterns = (
        r'^[ \t]*[（(]?(\d{1,2})[）).、．]',
        r'^[ \t]*(?:题目|问题)\s*(\d{1,2})',
        r'^[ \t]*第\s*(\d{1,2})\s*题',
        r'^[ \t]*%\s*(\d{1,2})(?:[.、)]|\s+)',
        r'^[ \t]*\\text[ \t]*\{[ \t]*(\d{1,2})[.、．)]',
        r'^[ \t]*\\\[[ \t\r\n]*\\text[ \t]*\{[ \t]*(\d{1,2})[.、．)]',
        r'^[ \t]*\\subsection\*?\{(?:题目|问题)?\s*(\d{1,2})',
    )
    for pattern in patterns:
        match = re.match(pattern, block)
        if match:
            return match.group(1)
    return ''


def _optional_positive_id(value, label):
    if value in (None, ''):
        return None
    return positive_int(value, f'{label}编号')


def _classification_prompt(text, chapter_list, expected_count=None):
    count_hint = (
        f'本组按题号初步识别为 {expected_count} 题，请逐题返回。'
        if expected_count
        else '请自行识别题目数量。'
    )
    return (
        "把输入拆成独立数学题，并为每题选择最匹配的章节。"
        "识别 1.、1、、（1）、(1)、\\text{1. ...}、题目1、% 1、"
        "\\subsection、\\item 和空行等分隔。"
        "必须保留每题完整原文，不要解题或改写；题号放 qnum。"
        "跨章节题 chapter_id=0，但 subject_id 必须有效。"
        f"{count_hint}\n"
        "章节（只能使用下列ID）：\n"
        f"{chapter_list}\n"
        "输入：\n"
        f"{text}\n"
        '只返回 JSON：{"problems":[{"content":"完整原文","subject_id":1,'
        '"chapter_id":1,"qnum":"1","reasons":""}]}'
    )


def _known_problem_classification_prompt(blocks, chapter_list):
    indexed_problems = '\n\n'.join(
        f'--- 题目索引 {index} ---\n{block}'
        for index, block in enumerate(blocks, start=1)
    )
    return (
        "下面的题目已经由程序可靠拆分。不要重新拆题、不要解题、不要复述题目原文，"
        "只为每个索引选择最匹配的科目和章节。"
        "跨章节题 chapter_id=0，但 subject_id 必须有效。\n"
        "章节（只能使用下列ID）：\n"
        f"{chapter_list}\n"
        "题目：\n"
        f"{indexed_problems}\n"
        '只返回短 JSON：{"problems":[{"index":1,"subject_id":1,'
        '"chapter_id":1,"qnum":"1","reasons":""}]}'
    )


def _classify_problem_text(app, text, chapter_list, expected_count):
    with app.app_context():
        result = deepseek_api_call(
            [
                {
                    'role': 'system',
                    'content': '你是快速、严格的数学题拆分归类器，只返回JSON。',
                },
                {
                    'role': 'user',
                    'content': _classification_prompt(
                        text,
                        chapter_list,
                        expected_count,
                    ),
                },
            ],
            temperature=0.0,
            timeout=app.config['BATCH_CLASSIFY_TIMEOUT'],
            task='classification',
            task_policy='classification_batch_object',
            max_attempts=1,
        )
    return _parse_problem_list(result)


def _classify_known_problem_blocks(app, blocks, chapter_list):
    with app.app_context():
        result = deepseek_api_call(
            [
                {
                    'role': 'system',
                    'content': '你是快速、严格的数学题章节归类器，只返回短JSON。',
                },
                {
                    'role': 'user',
                    'content': _known_problem_classification_prompt(
                        blocks,
                        chapter_list,
                    ),
                },
            ],
            temperature=0.0,
            timeout=app.config['BATCH_CLASSIFY_TIMEOUT'],
            task='classification',
            task_policy='classification_batch_object',
            max_attempts=1,
        )
    classifications = _parse_problem_list(result)
    if len(classifications) != len(blocks):
        raise ValueError(
            f'AI 归类数量不一致：应为 {len(blocks)} 题，'
            f'实际返回 {len(classifications)} 题'
        )

    merged = [None] * len(blocks)
    for position, classification in enumerate(classifications, start=1):
        if not isinstance(classification, dict):
            raise ValueError(f'第 {position} 个 AI 归类结果格式无效')
        try:
            index = int(classification.get('index', position))
        except (TypeError, ValueError):
            raise ValueError(f'第 {position} 个 AI 归类结果索引无效')
        if index < 1 or index > len(blocks) or merged[index - 1] is not None:
            raise ValueError(f'第 {position} 个 AI 归类结果索引重复或越界')
        problem = dict(classification)
        # Preserve the user's source exactly; the model only supplies metadata.
        problem['content'] = blocks[index - 1]
        merged[index - 1] = problem
    return merged


def _problem_rows(db, problems, default_reasons, note, date_added, chapter_map=None):
    """Validate the complete batch before returning rows for one atomic insert."""
    rows = []
    for index, problem in enumerate(problems, start=1):
        if not isinstance(problem, dict):
            raise ValueError(f'第 {index} 题格式无效')
        content = text_value(
            problem.get('content'),
            f'第 {index} 题内容',
            maximum=100_000,
            required=True,
        )

        if chapter_map is not None:
            chapter_key = str(problem.get('chapter') or '').strip()
            chapter = chapter_map.get(chapter_key)
            if not chapter:
                raise ValueError(f'第 {index} 题章节“{chapter_key or "未指定"}”无效')
            subject_id = chapter['subject_id']
            chapter_id = chapter['id']
            subject_name = chapter['subj']
            chapter_name = chapter['name']
        else:
            try:
                subject_id = int(problem.get('subject_id'))
                raw_chapter_id = problem.get('chapter_id')
                chapter_id = (
                    None
                    if raw_chapter_id in (None, '', 0, '0')
                    else int(raw_chapter_id)
                )
            except (TypeError, ValueError):
                raise ValueError(f'第 {index} 题科目或章节编号无效')

            if chapter_id is not None:
                chapter = db.execute(
                    "SELECT c.id,c.subject_id,c.name,s.name AS subject "
                    "FROM chapters c JOIN subjects s ON s.id=c.subject_id "
                    "WHERE c.id=?",
                    (chapter_id,),
                ).fetchone()
                if not chapter:
                    raise ValueError(f'第 {index} 题章节编号不存在')
                # The chapter is authoritative if the model returns mismatched IDs.
                subject_id = chapter['subject_id']
                subject_name = chapter['subject']
                chapter_name = chapter['name']
            else:
                subject = db.execute(
                "SELECT id,name FROM subjects WHERE id=?",
                (subject_id,),
                ).fetchone()
                if not subject:
                    raise ValueError(f'第 {index} 题科目编号不存在')
                subject_name = subject['name']
                chapter_name = ''

        qnum = text_value(problem.get('qnum'), f'第 {index} 题题号', maximum=80)
        problem_reasons = default_reasons or reasons_value(problem.get('reasons'))
        rows.append(
            (
                subject_id,
                chapter_id,
                qnum,
                content,
                problem_reasons,
                note,
                date_added,
                initial_review_date(date_added),
                mistake_fingerprint(
                    subject_name, chapter_name, qnum, content, date_added
                ),
            )
        )
    if not rows:
        raise ValueError('未识别到可保存的题目')
    return rows


def _job_id(value) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("任务编号无效")
    return value


def _insert_mistake_rows(db, rows, source_job_id: str | None) -> int:
    if source_job_id:
        existing = db.execute(
            "SELECT COUNT(*) AS n FROM mistakes WHERE source_job_id=?",
            (source_job_id,),
        ).fetchone()["n"]
        if existing:
            if existing != len(rows):
                raise ValueError("任务已有不完整的保存结果，请检查数据一致性")
            return existing
        db.executemany(
            "INSERT INTO mistakes(subject_id,chapter_id,qnum,content,reasons,note,"
            "date_added,next_review,fingerprint,source_job_id,source_job_index) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [row + (source_job_id, index) for index, row in enumerate(rows, start=1)],
        )
    else:
        db.executemany(
            "INSERT INTO mistakes(subject_id,chapter_id,qnum,content,reasons,note,"
            "date_added,next_review,fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


@bp.post('/internal/jobs/batch-mark-mistakes')
def api_batch_mark_mistakes():
    try:
        d = json_object(request)
        reasons = reasons_value(d.get('reasons'))
        note = text_value(d.get('note'), '备注', maximum=20_000)
        date_added = date_value(
            d.get('date_added') or datetime.now().strftime('%Y-%m-%d'),
            '日期',
        )
        source_job_id = _job_id(d.get('_job_id'))
    except (ValidationError, ValueError) as exc:
        return jsonify({'error': str(exc)}), 400
    db = get_db()

    # Keep the historical pre-split request format for API compatibility.
    if 'problems' in d:
        pre_split = d.get('problems')
        if not isinstance(pre_split, list):
            return jsonify({'error': 'problems 必须是数组'}), 400
        chapter_map = {
            f"{row['subj']}:{row['name']}": dict(row)
            for row in db.execute(
                "SELECT c.id,c.name,c.subject_id,s.name AS subj "
                "FROM chapters c JOIN subjects s ON c.subject_id=s.id"
            ).fetchall()
        }
        try:
            rows = _problem_rows(
                db,
                pre_split,
                reasons,
                note,
                date_added,
                chapter_map=chapter_map,
            )
            saved_count = _insert_mistake_rows(db, rows, source_job_id)
            db.commit()
            return jsonify({'ok': True, 'count': saved_count})
        except ValueError as exc:
            db.rollback()
            return jsonify({'error': str(exc)}), 400

    text = str(d.get('content') or '').strip()
    if not text:
        return jsonify({'error': '请输入题目内容'}), 400
    if len(text) > 6000:
        return jsonify({'error': '内容超过 6000 字，请分批录入以保证 30 秒内完成'}), 400

    try:
        subject_hint = _optional_positive_id(d.get('subject_id'), '科目')
        chapter_hint = _optional_positive_id(d.get('chapter_id'), '章节')
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    if chapter_hint is not None:
        selected_chapter = db.execute(
            "SELECT id,subject_id FROM chapters WHERE id=?",
            (chapter_hint,),
        ).fetchone()
        if not selected_chapter:
            return jsonify({'error': '章节编号不存在'}), 400
        if subject_hint is not None and subject_hint != selected_chapter['subject_id']:
            return jsonify({'error': '所选章节不属于当前科目'}), 400
        subject_hint = selected_chapter['subject_id']
    elif subject_hint is not None and not db.execute(
        "SELECT 1 FROM subjects WHERE id=?",
        (subject_hint,),
    ).fetchone():
        return jsonify({'error': '科目编号不存在'}), 400

    chapter_sql = (
        "SELECT c.id,c.name,c.subject_id,s.name AS subject "
        "FROM chapters c JOIN subjects s ON c.subject_id=s.id"
    )
    chapter_params = ()
    if subject_hint is not None:
        chapter_sql += " WHERE c.subject_id=?"
        chapter_params = (subject_hint,)
    chapters = [
        dict(row)
        for row in db.execute(chapter_sql, chapter_params).fetchall()
    ]
    chapter_list = '\n'.join(
        f'{chapter["id"]}: [{chapter["subject"]}] {chapter["name"]}'
        for chapter in chapters
    )
    try:
        numbered_problems = _split_numbered_input(text)
        if len(numbered_problems) > 10:
            return jsonify({'error': '一次最多录入 10 道题，请分批处理'}), 400
        app = current_app._get_current_object()
        if chapter_hint is not None:
            problems = [
                {
                    'content': block,
                    'subject_id': subject_hint,
                    'chapter_id': chapter_hint,
                    'qnum': _extract_question_number(block),
                }
                for block in numbered_problems
            ]
        elif subject_hint is not None and len(numbered_problems) == 1:
            # Match the normal single-mistake form: selecting a subject is
            # sufficient, while leaving the chapter empty stores a subject-level
            # comprehensive problem without depending on the classification AI.
            problems = [
                {
                    'content': numbered_problems[0],
                    'subject_id': subject_hint,
                    'chapter_id': None,
                    'qnum': _extract_question_number(numbered_problems[0]),
                }
            ]
        elif len(numbered_problems) > 3:
            midpoint = (len(numbered_problems) + 1) // 2
            groups = [
                numbered_problems[:midpoint],
                numbered_problems[midpoint:],
            ]
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        _classify_known_problem_blocks,
                        app,
                        group,
                        chapter_list,
                    )
                    for group in groups
                ]
                problems = []
                for group, future in zip(groups, futures):
                    group_result = future.result()
                    problems.extend(group_result)
        else:
            expected_count = (
                len(numbered_problems)
                if len(numbered_problems) > 1
                else None
            )
            if expected_count:
                problems = _classify_known_problem_blocks(
                    app,
                    numbered_problems,
                    chapter_list,
                )
            else:
                problems = _classify_problem_text(
                    app,
                    text,
                    chapter_list,
                    expected_count,
                )
        if subject_hint is not None and chapter_hint is None:
            allowed_chapter_ids = {chapter['id'] for chapter in chapters}
            for index, problem in enumerate(problems, start=1):
                if not isinstance(problem, dict):
                    raise ValueError(f'第 {index} 题格式无效')
                problem['subject_id'] = subject_hint
                raw_chapter_id = problem.get('chapter_id')
                if raw_chapter_id in (None, '', 0, '0'):
                    continue
                try:
                    classified_chapter_id = int(raw_chapter_id)
                except (TypeError, ValueError):
                    raise ValueError(f'第 {index} 题章节编号无效')
                if classified_chapter_id not in allowed_chapter_ids:
                    raise ValueError(f'第 {index} 题归类到了其他科目')
        if not problems:
            return jsonify({'error': '未识别到题目'}), 400
        if len(problems) > 10:
            return jsonify({'error': '一次最多录入 10 道题，请分批处理'}), 400

        rows = _problem_rows(db, problems, reasons, note, date_added)
        saved_count = _insert_mistake_rows(db, rows, source_job_id)
        db.commit()
        return jsonify({'ok': True, 'count': saved_count})
    except ValueError as exc:
        db.rollback()
        return jsonify({'error': str(exc)}), 400
    except DeepSeekConfigurationError as exc:
        db.rollback()
        return jsonify({'error': str(exc)}), 503
    except Exception as exc:
        db.rollback()
        return _service_error_response(exc, '批量归类')

@bp.route('/api/add-problem', methods=['POST'])
def api_add_problem():
    db = get_db()
    try:
        d = json_object(request)
        raw_cid = d.get('chapter_id')
        if raw_cid is None:
            raise ValidationError('请选择科目和章节')
        # chapter_id=0 is the established "综合题目" sentinel.
        cid = None if raw_cid in (0, '0') else raw_cid
        subj, ch = subject_chapter(db, d.get('subject_id'), cid)
        content = text_value(
            d.get('content'), '题目内容', maximum=100_000, required=True
        )
    except ValidationError as exc:
        return jsonify({'error': str(exc)}), 400
    try:
        tex_base = _configured_tex_base()
    except DeepSeekConfigurationError as exc:
        return jsonify({'error': str(exc)}), 503
    if ch:
        key = (subj['name'], ch['name'])
    else:
        key = None  # 综合题目
    tex_files = _chapter_sources().get(key, [])
    if not tex_files:
        # 综合题目：跨章节或无法归类 → 保存到综合题目.tex
        target = _fallback_source(subj['name'])
        _ensure_fallback_file(tex_base, target)
    else:
        target = tex_files[0]
    fpath = os.path.join(tex_base, target)
    if not os.path.exists(fpath): return jsonify({'error': f'目标文件不存在: {target}'}), 500
    try:
        next_num = append_problem(
            tex_base,
            target,
            content,
            compile_check=current_app.config.get("LIBRARY_MINIMAL_COMPILE_CHECK"),
        )
    except OSError:
        current_app.logger.exception("Problem library write failed")
        return jsonify({'error': '试题库写入失败，请检查目录权限'}), 500
    return jsonify({'ok': True, 'message': f'已保存到 {target}，题目编号：{next_num}', 'filename': target, 'problem_num': next_num})
