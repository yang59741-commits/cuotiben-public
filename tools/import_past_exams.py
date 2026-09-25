"""Normalize and import single-year university exam TeX files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cuoti_app.services.past_exams import (
    _document_body,
    _extract_standalone_structure,
    iter_official_tex_files,
    _standalone_question_anchors,
)


_SECTION_HEADING = re.compile(
    r'(?m)^[ \t]*\\section\*?\{(?P<label>[^}\r\n]*)\}[ \t]*'
)
_DALIAN_SUPPLEMENT_FILES = (
    '大工数分真题_03-05年_图像增强.tex',
    '大工数分真题_06-08年（不含07）_图像增强.tex',
    '大工数分真题_09-11年_图像增强.tex',
    '大工数分真题_12-14年（不含12）_图像增强.tex',
    '大工数分真题_15-17年_图像增强.tex',
    '大工数分真题_18-20年_图像增强.tex',
    '大工数分真题_21-22年（原文件仅含21）_图像增强.tex',
    '大连理工大学2023年数学分析试卷.tex',
)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _year_from_path(path: Path) -> int | None:
    match = re.search(r'((?:19|20)\d{2})\s*年', path.stem)
    return int(match.group(1)) if match else None


def _normalize_source(path: Path, institution: str, subject: str, year: int) -> str:
    raw = path.read_text(encoding='utf-8', errors='replace')
    return _normalize_text(raw, institution, subject, year)


def _normalize_text(raw: str, institution: str, subject: str, year: int) -> str:
    preamble = raw.split(r'\begin{document}', 1)[0].strip()
    body = _document_body(raw)
    # Combined source files retain a section heading for each year. The
    # canonical destination already supplies its own heading.
    body = _SECTION_HEADING.sub('', body, count=1).lstrip()
    lines = body.splitlines()
    anchors = _standalone_question_anchors(body)
    anchor_lines = {line for line, _number in anchors}
    output = [preamble, r'\begin{document}']
    output.append(f'\\section*{{{institution}{year}年{subject}真题}}')
    question_number = 0
    for line_number, line in enumerate(lines):
        if line_number in anchor_lines:
            question_number += 1
            output.extend((f'\\subsection{{题目{question_number}}}', ''))
        output.append(line)
    output.extend((r'\end{document}', ''))
    return '\n'.join(output)


def _supplement_sections(text: str) -> list[dict]:
    """Split a combined TeX file at its year section headings."""
    matches = [
        match
        for match in _SECTION_HEADING.finditer(text)
        if re.search(r'(?<!\d)(?:19|20)\d{2}\s*年?', match.group('label'))
    ]
    sections = []
    for index, match in enumerate(matches):
        label = match.group('label')
        year_match = re.search(r'(?<!\d)(?P<year>(?:19|20)\d{2})\s*年?', label)
        if not year_match:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section_text = text[match.start():end]
        section_text = re.sub(r'\\end\{document\}\s*$', '', section_text).strip()
        sections.append(
            {
                'year': int(year_match.group('year')),
                'label': label,
                'text': section_text,
            }
        )
    return sections


def import_dalian_math_supplement(
    supplement_dir: Path,
    source_target: Path,
    formal_target: Path,
) -> dict:
    """Import old combined Dalian math-analysis papers without touching backups."""
    supplement_dir = supplement_dir.resolve(strict=True)
    source_target = source_target.resolve(strict=True)
    formal_target = formal_target.resolve(strict=True)
    institution = '大连理工大学'
    subject = '数学分析'
    imported_years: list[int] = []
    skipped_years: list[int] = []
    warnings: list[str] = []
    source_files: list[str] = []
    sections_by_year: dict[int, dict] = {}

    for filename in _DALIAN_SUPPLEMENT_FILES:
        path = supplement_dir / filename
        if not path.is_file():
            warnings.append(f'补充文件不存在: {filename}')
            continue
        source_files.append(filename)
        raw = path.read_text(encoding='utf-8', errors='replace')
        for section in _supplement_sections(raw):
            # The 2024 combined file is intentionally excluded here because
            # the active source already contains the verified 2024 paper.
            if section['year'] in sections_by_year:
                sections_by_year[section['year']]['text'] += '\n\n' + section['text']
            else:
                sections_by_year[section['year']] = dict(section)

    for year in sorted(sections_by_year):
        section = sections_by_year[year]
        raw = section['text']
        normalized = _normalize_text(raw, institution, subject, year)
        structure = _extract_standalone_structure(_document_body(normalized))
        if not structure['question_count']:
            warnings.append(f'{year}年无法识别题目，已跳过')
            continue
        source_path = source_target / f'{year}年.tex'
        formal_path = formal_target / f'{year}年.tex'
        if source_path.exists() or formal_path.exists():
            skipped_years.append(year)
            continue
        source_path.parent.mkdir(parents=True, exist_ok=True)
        formal_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(normalized, encoding='utf-8', newline='\n')
        formal_path.write_text(normalized, encoding='utf-8', newline='\n')
        imported_years.append(year)

    return {
        'supplement_dir': str(supplement_dir),
        'source_target': str(source_target),
        'formal_target': str(formal_target),
        'source_files': source_files,
        'imported_years': imported_years,
        'skipped_existing_years': skipped_years,
        'warnings': warnings,
    }


def import_past_exams(source: Path, target: Path, *, prune_empty: bool = False) -> dict:
    source = source.resolve(strict=True)
    target = target.resolve(strict=True)
    imported = 0
    skipped = 0
    empty_paths: list[str] = []
    warnings: list[str] = []
    conflicts: list[str] = []
    for path in iter_official_tex_files(source):
        relative = path.relative_to(source)
        if len(relative.parts) < 3:
            warnings.append(f'目录层级异常: {relative}')
            continue
        institution, subject = relative.parts[0], relative.parts[1]
        year = _year_from_path(path)
        body = _document_body(path.read_text(encoding='utf-8', errors='replace'))
        structure = _extract_standalone_structure(body)
        if year is None or not structure['question_count']:
            empty_paths.append(str(relative))
            if prune_empty:
                path.unlink()
            continue
        destination = target / institution / subject / path.name
        normalized = _normalize_source(path, institution, subject, year)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_text(encoding='utf-8', errors='replace') == normalized:
                skipped += 1
                continue
            conflicts.append(str(destination))
            continue
        destination.write_text(normalized, encoding='utf-8', newline='\n')
        imported += 1
    if conflicts:
        raise RuntimeError('目标文件冲突，未覆盖: ' + '; '.join(conflicts))
    return {
        'source': str(source),
        'target': str(target),
        'imported': imported,
        'skipped_identical': skipped,
        'empty_or_invalid': empty_paths,
        'warnings': warnings,
        'source_tex_remaining': len(list(source.rglob('*.tex'))),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    parser.add_argument('--target', type=Path)
    parser.add_argument('--prune-empty', action='store_true')
    parser.add_argument('--dalian-supplement', type=Path)
    parser.add_argument('--dalian-source-target', type=Path)
    parser.add_argument('--dalian-formal-target', type=Path)
    args = parser.parse_args()
    if args.dalian_supplement:
        if not args.dalian_source_target or not args.dalian_formal_target:
            parser.error(
                '--dalian-supplement requires --dalian-source-target and '
                '--dalian-formal-target'
            )
        result = import_dalian_math_supplement(
            args.dalian_supplement,
            args.dalian_source_target,
            args.dalian_formal_target,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not args.source or not args.target:
        parser.error('--source and --target are required for normal import')
    result = import_past_exams(args.source, args.target, prune_empty=args.prune_empty)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
