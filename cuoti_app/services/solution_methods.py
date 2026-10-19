"""Canonical method identities and explicit roles, separate from question types."""

import unicodedata

from .solution_method_catalog import CATALOG_VERSION, METHODS

ROLES = frozenset({'primary', 'auxiliary', 'unspecified'})


class MethodError(ValueError):
    pass


def label_key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()


def list_methods(subject=None):
    return [dict(item) for item in METHODS if not subject or subject in item['subjects']]


def resolve_method(code, subject=None):
    item = next((item for item in METHODS if item['code'] == code), None)
    if item is None:
        raise MethodError('请选择目录中的解题方法')
    if subject and subject not in item['subjects']:
        raise MethodError('解题方法与科目不一致')
    return item


def match_label(label, subject=None):
    key = label_key(label)
    return next((item for item in list_methods(subject)
                 if key in {label_key(value) for value in [item['name'], *item['aliases']]}), None)


def normalize_annotations(labels, annotations=None, *, subject=None):
    # An unordered legacy label list carries no evidence of main/auxiliary roles.
    if annotations is None:
        annotations = []
        for label in labels:
            method = match_label(label, subject)
            annotations.append({'code': method['code'] if method else None,
                                'name': label, 'role': 'unspecified'})
    if not isinstance(annotations, list) or len(annotations) > 3:
        raise MethodError('解题方法最多 3 项')
    result, seen = [], {}
    for annotation in annotations:
        if not isinstance(annotation, dict) or annotation.get('role') not in ROLES:
            raise MethodError('方法角色须为主要、辅助或未区分')
        code, role = annotation.get('code'), annotation['role']
        if code:
            method = resolve_method(code, subject)
            name = method['name']
        else:
            name = annotation.get('name')
            if not isinstance(name, str):
                raise MethodError('目录外方法须填写名称')
            name = ' '.join(unicodedata.normalize('NFKC', name).split())
            if not 1 <= len(name) <= 40 or role != 'unspecified':
                raise MethodError('目录外标签须为 1 至 40 字，并保持待归类状态')
            method = match_label(name, subject)
            code = method['code'] if method else None
            if method:
                name = method['name']
        identity = code or label_key(name)
        if identity in seen:
            if seen[identity] != role:
                raise MethodError('同一方法不能同时标为主要和辅助')
            continue
        seen[identity] = role
        result.append({'code': code, 'name': name, 'role': role})
    if sum(item['role'] == 'primary' for item in result) > 1:
        raise MethodError('每道题最多一个主要方法')
    if sum(item['role'] == 'auxiliary' for item in result) > 2:
        raise MethodError('每道题最多两个辅助方法')
    return result


def annotations_for(classification, *, subject=None):
    annotations = classification.get('method_annotations')
    if annotations is not None:
        return annotations
    return normalize_annotations(classification.get('methods') or [], subject=subject)


def matches(classification, code, role='any', *, subject=None):
    return any(item.get('code') == code and (role == 'any' or item['role'] == role)
               for item in annotations_for(classification, subject=subject))


def catalog_prompt(subjects):
    return [dict(item) for item in METHODS if set(item['subjects']) & set(subjects)]
