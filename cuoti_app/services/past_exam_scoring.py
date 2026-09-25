"""Read explicit main-question points without guessing missing allocations."""

from decimal import Decimal
import re
import unicodedata


_NUMBER = r'(\d+(?:\.\d+)?)'
_MAIN_POINTS = re.compile(r'^\s*\(\s*(?:本题\s*)?' + _NUMBER + r'\s*分\s*\)')
_EACH_POINTS = re.compile(r'每(?:小)?题\s*' + _NUMBER + r'\s*分')
_SECTION_TOTAL = re.compile(r'共\s*' + _NUMBER + r'\s*分')
_PAPER_TOTAL = re.compile(r'(?:满分|总分)\s*[:：]?\s*' + _NUMBER + r'\s*分')
_NUMBERED_POINTS = re.compile(
    r'第\s*(\d+(?:\s*[,、]\s*\d+)*)\s*题\s*(?:每题\s*)?' + _NUMBER + r'\s*分')


def _number(value):
    return int(value) if value == value.to_integral_value() else float(value)


def extract_explicit_scoring(structure, problems, source_text):
    """Return a complete allocation or unscored, retaining observed evidence."""
    by_number = {row['source_number']: row for row in problems}
    slot_points, slot_evidence, section_points, observations, issues = {}, {}, [], [], []
    for section in structure['sections']:
        heading = unicodedata.normalize('NFKC', section['label'])
        numbered = list(_NUMBERED_POINTS.finditer(heading))
        each = _EACH_POINTS.search(_NUMBERED_POINTS.sub('', heading))
        stated_total = _SECTION_TOTAL.search(heading)
        values = []
        raw_numbers = section.get('raw_question_numbers', list(map(str, section['question_numbers'])))
        for index, number in enumerate(section['question_numbers']):
            first_line = unicodedata.normalize('NFKC', by_number[number]['content']).lstrip().splitlines()[0]
            explicit = _MAIN_POINTS.match(first_line)
            specific = [match for match in numbered
                        if str(raw_numbers[index]) in re.split(r'\s*[,、]\s*', match.group(1))]
            evidence = {'source_number': number, 'section_heading': section['label'],
                        'main_label': explicit.group(0).strip() if explicit else None,
                        'per_question_label': each.group(0) if each else None}
            if specific:
                evidence['numbered_question_labels'] = [match.group(0) for match in specific]
            observations.append(evidence)
            point_values = [Decimal(match.group(1)) for match in (explicit, each) if match]
            point_values.extend(Decimal(match.group(2)) for match in specific)
            if len(set(point_values)) > 1:
                issues.append(f'conflicting_slot_points:{number}')
            if not point_values:
                issues.append(f'missing_slot_points:{number}')
                continue
            value = point_values[0]
            if value <= 0:
                issues.append(f'nonpositive_slot_points:{number}')
                continue
            values.append(value)
            slot_points[str(number)] = _number(value)
            slot_evidence[str(number)] = evidence | {'points': _number(value)}
        total = sum(values, Decimal(0))
        if stated_total and len(values) == len(section['question_numbers']) and total != Decimal(stated_total.group(1)):
            issues.append(f'conflicting_section_total:{section["label"]}')
        section_points.append({'title': section['label'], 'question_numbers': section['question_numbers'],
                               'points': _number(total),
                               'declared_total': stated_total.group(0) if stated_total else None,
                               'basis': 'sum_of_explicit_slot_points'})
    total = sum((Decimal(str(value)) for value in slot_points.values()), Decimal(0))
    declared = [Decimal(match.group(1)) for match in _PAPER_TOTAL.finditer(unicodedata.normalize('NFKC', source_text))]
    if declared and not issues and any(value != total for value in declared):
        issues.append('conflicting_paper_total')
    evidence = {'observations': observations, 'issues': issues,
                'declared_paper_totals': [_number(value) for value in declared]}
    if issues:
        return {'mode': 'unscored', 'slot_points': {}, 'slot_evidence': {}, 'sections': [],
                'total_points': None, 'evidence': evidence}
    return {'mode': 'verified', 'slot_points': slot_points, 'slot_evidence': slot_evidence,
            'sections': section_points, 'total_points': _number(total),
            'evidence': evidence | {'basis': 'sum_of_explicit_slot_points', 'total_points': _number(total)}}
