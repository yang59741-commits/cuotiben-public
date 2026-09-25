"""Validated catalog configuration for subjects, chapters and TeX sources."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..paths import INSTANCE_DIR, RESOURCE_DIR


CATALOG_SCHEMA_VERSION = 1
DEFAULT_CATALOG_PATH = RESOURCE_DIR / "cuoti_app" / "catalog.default.json"
ACTIVE_CATALOG_PATH = INSTANCE_DIR / "catalog.json"
_CODE = re.compile(r"[a-z][a-z0-9_.-]{1,79}")


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ChapterConfig:
    subject_code: str
    chapter_code: str
    subject_name: str
    name: str
    display_order: int
    parent_code: str | None
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubjectConfig:
    subject_code: str
    name: str
    display_order: int
    fallback_source: str
    chapters: tuple[ChapterConfig, ...]


def _safe_tex_name(value, field: str) -> str:
    name = str(value or "").strip()
    if (
        not name
        or Path(name).name != name
        or not name.lower().endswith(".tex")
        or any(character in name for character in ("/", "\\", "\x00"))
    ):
        raise CatalogError(f"{field} 必须是安全的 .tex 文件名")
    return name


class CatalogService:
    def __init__(
        self,
        path: str | Path = ACTIVE_CATALOG_PATH,
        *,
        default_path: str | Path = DEFAULT_CATALOG_PATH,
    ) -> None:
        self.path = Path(path)
        self.default_path = Path(default_path)
        self._raw: dict = {}
        self.subjects: tuple[SubjectConfig, ...] = ()
        self.exclusions: tuple[dict, ...] = ()

    def ensure(self) -> "CatalogService":
        if not self.path.exists():
            if not self.default_path.is_file():
                raise CatalogError("内置目录配置缺失")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                shutil.copyfile(self.default_path, temporary)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return self.reload()

    def reload(self) -> "CatalogService":
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise CatalogError("catalog.json 必须使用 UTF-8 编码") from exc
        except json.JSONDecodeError as exc:
            raise CatalogError(f"catalog.json 第 {exc.lineno} 行不是有效 JSON") from exc
        except OSError as exc:
            raise CatalogError("catalog.json 无法读取") from exc
        self._load(data)
        return self

    def _load(self, data) -> None:
        if not isinstance(data, dict) or data.get("schema_version") != CATALOG_SCHEMA_VERSION:
            raise CatalogError("catalog.json schema_version 不受支持")
        raw_subjects = data.get("subjects")
        if not isinstance(raw_subjects, list) or not raw_subjects:
            raise CatalogError("catalog.json 至少需要一个科目")
        subjects: list[SubjectConfig] = []
        subject_codes: set[str] = set()
        subject_names: set[str] = set()
        chapter_codes: set[str] = set()
        chapter_names: set[tuple[str, str]] = set()
        for raw_subject in raw_subjects:
            if not isinstance(raw_subject, dict):
                raise CatalogError("科目配置必须是对象")
            code = str(raw_subject.get("subject_code") or "")
            name = str(raw_subject.get("name") or "").strip()
            if not _CODE.fullmatch(code) or not name or len(name) > 100:
                raise CatalogError("科目代码或名称无效")
            if code in subject_codes or name in subject_names:
                raise CatalogError("科目代码和名称必须唯一")
            subject_codes.add(code)
            subject_names.add(name)
            order = int(raw_subject.get("display_order", 0))
            fallback = _safe_tex_name(raw_subject.get("fallback_source"), "fallback_source")
            raw_chapters = raw_subject.get("chapters")
            if not isinstance(raw_chapters, list):
                raise CatalogError(f"科目 {name} 的 chapters 必须是数组")
            chapters: list[ChapterConfig] = []
            for raw_chapter in raw_chapters:
                if not isinstance(raw_chapter, dict):
                    raise CatalogError("章节配置必须是对象")
                chapter_code = str(raw_chapter.get("chapter_code") or "")
                chapter_name = str(raw_chapter.get("name") or "").strip()
                parent = raw_chapter.get("parent_code") or None
                if not _CODE.fullmatch(chapter_code) or not chapter_name or len(chapter_name) > 100:
                    raise CatalogError("章节代码或名称无效")
                if chapter_code in chapter_codes or (code, chapter_name) in chapter_names:
                    raise CatalogError("章节代码和科目内名称必须唯一")
                if parent is not None and not _CODE.fullmatch(str(parent)):
                    raise CatalogError("父章节代码无效")
                sources = raw_chapter.get("sources")
                if not isinstance(sources, list):
                    raise CatalogError("章节 sources 必须是数组")
                source_names = tuple(
                    dict.fromkeys(_safe_tex_name(item, "source") for item in sources)
                )
                chapter_codes.add(chapter_code)
                chapter_names.add((code, chapter_name))
                chapters.append(
                    ChapterConfig(
                        code,
                        chapter_code,
                        name,
                        chapter_name,
                        int(raw_chapter.get("display_order", 0)),
                        str(parent) if parent else None,
                        source_names,
                    )
                )
            local_codes = {chapter.chapter_code for chapter in chapters}
            for chapter in chapters:
                if chapter.parent_code and chapter.parent_code not in local_codes:
                    raise CatalogError(f"章节 {chapter.chapter_code} 的父章节不存在")
            subjects.append(
                SubjectConfig(
                    code,
                    name,
                    order,
                    fallback,
                    tuple(sorted(chapters, key=lambda item: (item.display_order, item.chapter_code))),
                )
            )
        exclusions = data.get("exclusions", [])
        if not isinstance(exclusions, list) or any(not isinstance(item, dict) for item in exclusions):
            raise CatalogError("exclusions 必须是对象数组")
        known_sources = {
            source
            for subject in subjects
            for chapter in subject.chapters
            for source in chapter.sources
        }
        validated_exclusions = []
        for item in exclusions:
            rule = dict(item)
            kind = rule.get("kind")
            source_file = _safe_tex_name(rule.get("source_file"), "排除规则 source_file")
            reason = str(rule.get("reason") or "").strip()
            owner = str(rule.get("owner") or "").strip()
            expires_when = str(rule.get("expires_when") or "").strip()
            if source_file not in known_sources:
                raise CatalogError(f"排除规则引用了未配置的题源：{source_file}")
            if not reason or not owner or not expires_when:
                raise CatalogError("排除规则必须包含 reason、owner 和 expires_when")
            if kind == "question_numbers":
                numbers = rule.get("numbers")
                if (
                    not isinstance(numbers, list)
                    or not numbers
                    or any(not isinstance(number, int) or number <= 0 for number in numbers)
                ):
                    raise CatalogError("question_numbers 规则必须包含正整数 numbers")
                rule["numbers"] = list(dict.fromkeys(numbers))
            elif kind == "range":
                start_pattern = rule.get("start_pattern")
                end_pattern = rule.get("end_pattern")
                if not isinstance(start_pattern, str) or not start_pattern:
                    raise CatalogError("range 规则缺少 start_pattern")
                try:
                    re.compile(start_pattern)
                    if end_pattern is not None:
                        re.compile(str(end_pattern))
                except re.error as exc:
                    raise CatalogError(f"排除规则正则无效：{exc}") from exc
            elif kind != "whole_file":
                raise CatalogError(f"不支持的排除规则类型：{kind}")
            validated_exclusions.append(rule)
        self._raw = data
        self.subjects = tuple(sorted(subjects, key=lambda item: (item.display_order, item.subject_code)))
        self.exclusions = tuple(validated_exclusions)

    @property
    def semantic_digest(self) -> str:
        """Stable digest of source mappings and exclusion behavior."""

        payload = {
            "chapter_sources": [
                [subject, chapter, filenames]
                for (subject, chapter), filenames in self.chapter_sources.items()
            ],
            "exclusions": list(self.exclusions),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def chapter_sources(self) -> dict[tuple[str, str], list[str]]:
        return {
            (subject.name, chapter.name): list(chapter.sources)
            for subject in self.subjects
            for chapter in subject.chapters
        }

    @property
    def subject_chapters(self) -> dict[str, list[str]]:
        return {
            subject.name: [chapter.name for chapter in subject.chapters]
            for subject in self.subjects
        }

    def fallback_source(self, subject_name: str | None) -> str:
        for subject in self.subjects:
            if subject.name == subject_name:
                return subject.fallback_source
        return "综合题目.tex"

    def public_catalog(self) -> dict:
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "path": str(self.path),
            "semantic_digest": self.semantic_digest,
            "subjects": [
                {
                    "subject_code": subject.subject_code,
                    "name": subject.name,
                    "display_order": subject.display_order,
                    "fallback_source": subject.fallback_source,
                    "chapters": [
                        {
                            "chapter_code": chapter.chapter_code,
                            "name": chapter.name,
                            "display_order": chapter.display_order,
                            "parent_code": chapter.parent_code,
                            "sources": list(chapter.sources),
                        }
                        for chapter in subject.chapters
                    ],
                }
                for subject in self.subjects
            ],
            "exclusion_count": len(self.exclusions),
        }

    def sync_database(self, db) -> dict[str, int]:
        """Add configured subjects/chapters without renaming or deleting existing rows."""

        added_subjects = 0
        added_chapters = 0
        subject_columns = {
            row[1] for row in db.execute("PRAGMA table_info(subjects)").fetchall()
        }
        coded = "subject_code" in subject_columns
        for subject in self.subjects:
            existing_subject = db.execute(
                "SELECT id FROM subjects WHERE name=? ORDER BY id LIMIT 1",
                (subject.name,),
            ).fetchone()
            if coded:
                if existing_subject is None:
                    cursor = db.execute(
                        "INSERT INTO subjects(name,subject_code,display_order) VALUES(?,?,?)",
                        (subject.name, subject.subject_code, subject.display_order),
                    )
                    subject_id = cursor.lastrowid
                    added_subjects += 1
                else:
                    subject_id = existing_subject[0]
                db.execute(
                    "UPDATE subjects SET subject_code=?,display_order=? WHERE id=?",
                    (subject.subject_code, subject.display_order, subject_id),
                )
            else:
                if existing_subject is None:
                    cursor = db.execute(
                        "INSERT INTO subjects(name) VALUES(?)", (subject.name,)
                    )
                    subject_id = cursor.lastrowid
                    added_subjects += 1
                else:
                    subject_id = existing_subject[0]
            for chapter in subject.chapters:
                existing_chapter = db.execute(
                    "SELECT id FROM chapters WHERE subject_id=? AND name=? "
                    "ORDER BY id LIMIT 1",
                    (subject_id, chapter.name),
                ).fetchone()
                if coded:
                    if existing_chapter is None:
                        cursor = db.execute(
                            "INSERT INTO chapters("
                            "subject_id,name,chapter_code,display_order,parent_code) "
                            "VALUES(?,?,?,?,?)",
                            (
                                subject_id,
                                chapter.name,
                                chapter.chapter_code,
                                chapter.display_order,
                                chapter.parent_code,
                            ),
                        )
                        chapter_id = cursor.lastrowid
                        added_chapters += 1
                    else:
                        chapter_id = existing_chapter[0]
                    db.execute(
                        "UPDATE chapters SET chapter_code=?,display_order=?,parent_code=? "
                        "WHERE id=?",
                        (
                            chapter.chapter_code,
                            chapter.display_order,
                            chapter.parent_code,
                            chapter_id,
                        ),
                    )
                else:
                    if existing_chapter is None:
                        db.execute(
                            "INSERT INTO chapters(subject_id,name) VALUES(?,?)",
                            (subject_id, chapter.name),
                        )
                        added_chapters += 1
        return {"added_subjects": added_subjects, "added_chapters": added_chapters}


def load_default_catalog() -> CatalogService:
    return CatalogService(DEFAULT_CATALOG_PATH, default_path=DEFAULT_CATALOG_PATH).reload()
