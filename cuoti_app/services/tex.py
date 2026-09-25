"""TeX library writes and safe XeLaTeX compilation helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..utils import subsection_numbers


_APPEND_OPERATION_MARKER = re.compile(
    r"(?m)^% cuoti-job:([0-9a-f]{32}):([0-9a-f]{64}):([A-Za-z0-9_-]+)$"
)


class TexCompileError(RuntimeError):
    """Raised when XeLaTeX cannot produce a valid PDF."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "TEX_COMPILE_FAILED",
        hint: str = "请检查题目中的 LaTeX 后重试",
        correlation_id: str | None = None,
        log_name: str | None = None,
    ):
        self.code = code
        self.hint = hint
        self.correlation_id = correlation_id
        self.log_name = log_name
        suffix = []
        if hint:
            suffix.append(hint)
        if correlation_id:
            suffix.append(f"诊断编号 {correlation_id}")
        if log_name:
            suffix.append(f"完整日志 {log_name}")
        super().__init__("；".join([message, *suffix]))


class UnsafeLatexError(ValueError):
    """Raised when generated LaTeX is unsafe or deterministically invalid."""


UNSAFE_LATEX_PATTERN = re.compile(
    r"""
    \\(?:
        @+input|InputIfFileExists|
        CatchFile(?:Def|Edef|Gdef|BetweenTags)|
        documentclass|usepackage|RequirePackage|
        input|include|includeonly|includegraphics|IfFileExists|
        openin|openout|newread|newwrite|read|write\d*|immediate|
        special|catcode|csname|endcsname|loop|repeat|shipout|
        newcommand|renewcommand|providecommand|
        newenvironment|renewenvironment|
        def|gdef|edef|xdef|
        makeatletter|makeatother|
        bibliography|addbibresource|lstinputlisting|verbatiminput|
        directlua|latelua|pdf[a-zA-Z]*|XeTeX[a-zA-Z]*|href
    )\b|
    \\(?:begin|end)\s*\{\s*(?:document|filecontents\*?)\s*\}
    """,
    re.IGNORECASE | re.VERBOSE,
)
_UNSAFE_LINK_SCHEME = re.compile(
    r"\\href\s*\{\s*(?:javascript|run|file)\s*:",
    re.IGNORECASE,
)
_ARTIFACT_STEM = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}\Z")
_LITERAL_LINE_BREAK = re.compile(
    r"(?<=[。！？；：，、.!?;:,$])\\(?:r\\)?n"
    r"(?=[ \t]*(?:[A-Z0-9\u4e00-\u9fff]|\\))"
)
_LATEX_ENVIRONMENT = re.compile(
    r"\\(?P<action>begin|end)\s*\{\s*(?P<name>[A-Za-z]+\*?)\s*\}"
)
_MATH_ENVIRONMENTS = frozenset({
    "align", "align*", "alignat", "alignat*", "aligned", "array", "bmatrix",
    "cases", "displaymath", "equation", "equation*", "gather", "gather*",
    "gathered", "math", "matrix", "multline", "multline*", "pmatrix",
    "smallmatrix", "split", "Vmatrix", "vmatrix",
})
_LIBRARY_WRITE_LOCK = threading.RLock()
_COMPILE_RUNNER = ContextVar("cuoti_compile_runner", default=None)


@contextmanager
def use_compile_runner(runner):
    """Temporarily bind a cancel-aware runner for the current execution context."""

    token = _COMPILE_RUNNER.set(runner)
    try:
        yield
    finally:
        _COMPILE_RUNNER.reset(token)


def normalize_generated_latex(text: str) -> str:
    """Repair literal JSON line-break escapes without touching TeX commands."""

    return _LITERAL_LINE_BREAK.sub("\n", str(text or ""))


def _text_mode_math_marker(latex: str) -> tuple[str, int] | None:
    """Locate a TeX-only math marker that is certainly outside math mode."""

    modes: list[str] = []
    index = 0
    length = len(latex)
    while index < length:
        char = latex[index]
        if char == "%":
            newline = latex.find("\n", index + 1)
            index = length if newline < 0 else newline + 1
            continue
        if char == "\\":
            environment = _LATEX_ENVIRONMENT.match(latex, index)
            if environment is not None:
                name = environment.group("name")
                marker = f"environment:{name}"
                if name in _MATH_ENVIRONMENTS:
                    if environment.group("action") == "begin":
                        modes.append(marker)
                    elif marker in modes:
                        reverse_index = modes[::-1].index(marker)
                        del modes[len(modes) - reverse_index - 1]
                index = environment.end()
                continue
            if latex.startswith(r"\(", index):
                modes.append(r"\(")
                index += 2
                continue
            if latex.startswith(r"\)", index):
                if r"\(" in modes:
                    reverse_index = modes[::-1].index(r"\(")
                    del modes[len(modes) - reverse_index - 1]
                index += 2
                continue
            if latex.startswith(r"\[", index):
                modes.append(r"\[")
                index += 2
                continue
            if latex.startswith(r"\]", index):
                if r"\[" in modes:
                    reverse_index = modes[::-1].index(r"\[")
                    del modes[len(modes) - reverse_index - 1]
                index += 2
                continue
            if latex.startswith(r"\verb", index):
                cursor = index + len(r"\verb")
                if cursor < length and latex[cursor] == "*":
                    cursor += 1
                if cursor < length and not latex[cursor].isspace():
                    delimiter = latex[cursor]
                    closing = latex.find(delimiter, cursor + 1)
                    if closing >= 0:
                        index = closing + 1
                        continue
            # Escaped text-mode \_ / \^ and ordinary TeX commands cannot make
            # the immediately following character a bare marker.
            index += 2
            continue
        if char == "$":
            marker = "$$" if index + 1 < length and latex[index + 1] == "$" else "$"
            if modes and modes[-1] == marker:
                modes.pop()
            else:
                modes.append(marker)
            index += len(marker)
            continue
        if char in {"^", "_"} and not modes:
            return char, latex.count("\n", 0, index) + 1
        index += 1
    return None


def escape_latex_text(value: object) -> str:
    """Escape plain metadata before inserting it into a LaTeX document."""
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "$": r"\$",
        "&": r"\&",
        "#": r"\#",
        "_": r"\_",
        "%": r"\%",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in str(value))


def validate_latex_body(
    latex: str,
    *,
    label: str = "LaTeX 正文",
    max_length: int = 500_000,
) -> str:
    """Validate an untrusted body fragment and return it unchanged."""
    if not isinstance(latex, str):
        raise UnsafeLatexError(f"{label}不是文本")
    if not latex.strip():
        raise UnsafeLatexError(f"{label}为空")
    if len(latex) > max_length:
        raise UnsafeLatexError(f"{label}过长")
    if "\x00" in latex or "^^" in latex:
        raise UnsafeLatexError(f"{label}包含不安全的 TeX 控制字符")
    if (
        UNSAFE_LATEX_PATTERN.search(latex)
        or _UNSAFE_LINK_SCHEME.search(latex)
    ):
        raise UnsafeLatexError(f"{label}包含文件、文档或执行类禁用命令")
    return latex


def validate_generated_latex(latex: str, *, max_length: int = 500_000) -> str:
    """Validate AI-generated body LaTeX and return the unchanged body."""

    validated = validate_latex_body(
        latex,
        label="AI 返回的试卷正文",
        max_length=max_length,
    )
    invalid_marker = _text_mode_math_marker(validated)
    if invalid_marker is not None:
        marker, line_number = invalid_marker
        raise UnsafeLatexError(
            f"AI 返回的试卷正文第 {line_number} 行在数学模式外包含 {marker}"
        )
    return validated


def append_problem(
    tex_base: str, filename: str, content: str, *, compile_check=None
) -> int:
    """Append one numbered problem while preserving the existing document."""
    return append_problems(
        tex_base, [(filename, content)], compile_check=compile_check
    )[0]


def append_problems(
    tex_base: str,
    entries: list[tuple[str, str]],
    *,
    compile_check=None,
    operation_id: str | None = None,
    operation_input_hash: str | None = None,
    operation_metadata: list[dict] | None = None,
) -> list[int]:
    """Atomically append a batch of problems across one or more TeX files."""
    if not entries:
        return []
    if operation_id is not None and not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("题库写入 operation_id 无效")
    if operation_id is not None and not re.fullmatch(
        r"[0-9a-f]{64}", str(operation_input_hash or "")
    ):
        raise ValueError("题库写入 input hash 无效")
    metadata = operation_metadata or [{} for _ in entries]
    if len(metadata) != len(entries) or any(
        not isinstance(item, dict) for item in metadata
    ):
        raise ValueError("题库写入 operation metadata 无效")
    base = Path(tex_base).resolve()
    grouped: dict[Path, list[tuple[int, str]]] = defaultdict(list)
    for index, (filename, content) in enumerate(entries):
        path = (base / filename).resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise ValueError("题库目标文件必须位于配置目录内") from exc
        validate_latex_body(content, label="题目内容")
        grouped[path].append((index, content))

    with _LIBRARY_WRITE_LOCK:
        if operation_id is not None:
            replay = load_append_operation(
                base, operation_id, input_hash=operation_input_hash
            )
            if replay is not None:
                expected = [
                    (filename, hashlib.sha256(content.encode("utf-8")).hexdigest())
                    for filename, content in entries
                ]
                actual = [
                    (str(item["filename"]), str(item["content_sha256"]))
                    for item in replay
                ]
                if actual != expected:
                    raise ValueError("同一 operation_id 已用于不同题库内容")
                return [int(item["num"]) for item in replay]
        originals: dict[Path, str] = {}
        rendered: dict[Path, str] = {}
        assigned = [0] * len(entries)
        for path, items in grouped.items():
            try:
                existing = path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"题库文件 {path.name} 不是有效 UTF-8/UTF-8-SIG，请先显式转换编码"
                ) from exc
            begin_count = existing.count(r"\begin{document}")
            end_count = existing.count(r"\end{document}")
            if begin_count != 1 or end_count != 1:
                raise ValueError(
                    f"题库文件 {path.name} 文档结构无效：必须且只能有一个 document 边界"
                )
            begin_at = existing.index(r"\begin{document}")
            end_at = existing.index(r"\end{document}")
            if begin_at >= end_at or existing[end_at + len(r"\end{document}") :].strip():
                raise ValueError(f"题库文件 {path.name} 的 \\end{{document}} 位置无效")
            originals[path] = existing
            numbers = subsection_numbers(existing)
            next_number = max(numbers, default=0) + 1
            sections = []
            normalized_existing = re.sub(r"\s+", "", existing)
            normalized_pending: set[str] = set()
            for original_index, content in items:
                normalized = re.sub(r"\s+", "", content)
                if normalized in normalized_pending or (
                    len(normalized) >= 25 and normalized in normalized_existing
                ):
                    raise ValueError(f"题库文件 {path.name} 已包含明显重复的题目内容")
                normalized_pending.add(normalized)
                assigned[original_index] = next_number
                marker = ""
                if operation_id is not None:
                    marker_payload = {
                        "index": original_index + 1,
                        "total": len(entries),
                        "filename": entries[original_index][0],
                        "num": next_number,
                        "content_sha256": hashlib.sha256(
                            content.encode("utf-8")
                        ).hexdigest(),
                        "result": metadata[original_index],
                    }
                    encoded = base64.urlsafe_b64encode(
                        json.dumps(
                            marker_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).decode("ascii").rstrip("=")
                    marker = (
                        f"% cuoti-job:{operation_id}:"
                        f"{operation_input_hash}:{encoded}\n"
                    )
                sections.append(
                    f"\n{marker}\\subsection{{题目{next_number}}}\n{content}\n"
                )
                next_number += 1
            addition = "".join(sections)
            if r"\end{document}" in existing:
                rendered[path] = existing.replace(
                    r"\end{document}", addition + r"\end{document}", 1
                )
            else:
                rendered[path] = existing + addition

        temporary: dict[Path, Path] = {}
        published: list[Path] = []
        try:
            for path, text in rendered.items():
                temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                with temp_path.open("w", encoding="utf-8", newline="") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                if temp_path.read_text(encoding="utf-8") != text:
                    raise OSError(f"题库临时文件校验失败: {path.name}")
                if compile_check is not None:
                    compile_check(temp_path)
                temporary[path] = temp_path
            for path, temp_path in temporary.items():
                os.replace(temp_path, path)
                published.append(path)
            return assigned
        except Exception:
            # Restore already-published files from memory; no persistent backup is created.
            for path in reversed(published):
                rollback = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rollback")
                rollback.write_text(originals[path], encoding="utf-8")
                os.replace(rollback, path)
            raise
        finally:
            for temp_path in temporary.values():
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def load_append_operation(
    tex_base: str | Path,
    operation_id: str,
    *,
    input_hash: str | None = None,
) -> list[dict] | None:
    """Load and validate an atomically published append operation marker set."""

    if not re.fullmatch(r"[0-9a-f]{32}", str(operation_id or "")):
        raise ValueError("题库写入 operation_id 无效")
    root = Path(tex_base).resolve()
    found: list[dict] = []
    seen_hashes: set[str] = set()
    for path in sorted(root.rglob("*.tex")):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        for match in _APPEND_OPERATION_MARKER.finditer(text):
            if match.group(1) != operation_id:
                continue
            seen_hashes.add(match.group(2))
            encoded = match.group(3)
            padding = "=" * (-len(encoded) % 4)
            try:
                payload = json.loads(
                    base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
                )
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("题库写入 operation marker 损坏") from exc
            if not isinstance(payload, dict):
                raise ValueError("题库写入 operation marker 无效")
            found.append(payload)
    if not found:
        return None
    if len(seen_hashes) != 1 or (
        input_hash is not None and seen_hashes != {str(input_hash)}
    ):
        raise ValueError("同一 operation_id 的输入哈希冲突")
    try:
        ordered = sorted(found, key=lambda item: int(item["index"]))
        total = int(ordered[0]["total"])
        indices = [int(item["index"]) for item in ordered]
        if total < 1 or indices != list(range(1, total + 1)):
            raise ValueError
        if any(int(item["total"]) != total for item in ordered):
            raise ValueError
        for item in ordered:
            if (
                not isinstance(item.get("result"), dict)
                or int(item["num"]) < 1
                or not re.fullmatch(r"[0-9a-f]{64}", str(item["content_sha256"]))
            ):
                raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("题库写入 operation marker 不完整") from exc
    return ordered


def _tex_error_detail(output: bytes) -> tuple[str, str, str]:
    text = output.decode("utf-8", errors="replace")
    line = re.search(r"(?m)^l\.(\d+)\s*(.*)$", text)
    line_number = f"（第 {line.group(1)} 行）" if line else ""
    command = None
    if line:
        following = text[line.end():].splitlines()
        pointer_text = ""
        for candidate in following:
            if not candidate:
                continue
            if candidate[:1].isspace():
                pointer_text = candidate.lstrip()
            break
        command_match = re.search(r"\\[A-Za-z@]+", pointer_text)
        if command_match is None:
            command_match = re.search(r"\\[A-Za-z@]+", line.group(2))
        command = command_match.group(0) if command_match else None
    if re.search(r"Undefined control sequence", text, re.IGNORECASE):
        label = f"未定义命令 {command}" if command else "存在未定义的 LaTeX 命令"
        return "TEX_UNDEFINED_COMMAND", label + line_number, "请检查命令拼写或所需宏包"
    missing = re.search(
        r"(?:LaTeX Error:\s*)?File [`']([^`']+)[`'] not found",
        text,
        re.IGNORECASE,
    )
    if missing:
        filename = re.split(r"[/\\]", missing.group(1))[-1][:160]
        return "TEX_MISSING_FILE", f"缺少 TeX 文件 {filename}" + line_number, "请安装所需宏包或移除文件引用"
    if re.search(r"\\begin\{[^}]+\}.*ended by.*\\end\{", text, re.IGNORECASE | re.DOTALL):
        return "TEX_ENVIRONMENT_MISMATCH", "LaTeX 环境的开始与结束不匹配" + line_number, "请核对 begin/end 环境名称和嵌套顺序"
    if re.search(r"Runaway argument|Extra \}|Missing \} inserted|File ended while scanning", text, re.IGNORECASE):
        return "TEX_BRACE_MISMATCH", "LaTeX 括号或参数没有正确闭合" + line_number, "请检查错误行附近的大括号和命令参数"
    return "TEX_COMPILE_FAILED", "XeLaTeX 编译失败" + line_number, "请根据诊断编号查看完整日志"


def _write_compile_log(output_dir: Path, correlation_id: str, content: bytes) -> str:
    diagnostics = output_dir / ".diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    name = f"tex-{correlation_id}.log"
    (diagnostics / name).write_bytes(content or b"No compiler output was captured.\n")
    return name


def _compile_failure(
    output_dir: Path,
    output: bytes,
    *,
    correlation_id: str,
) -> TexCompileError:
    code, message, hint = _tex_error_detail(output)
    log_name = _write_compile_log(output_dir, correlation_id, output)
    return TexCompileError(
        message,
        code=code,
        hint=hint,
        correlation_id=correlation_id,
        log_name=log_name,
    )


def compile_latex_artifact(
    pdf_dir: str,
    command: str,
    stem: str,
    latex: str,
    *,
    timeout: int = 60,
    runner=None,
    publish_source: bool = True,
) -> None:
    """Compile a safe-named artifact in isolation, then atomically publish it."""
    if not isinstance(stem, str) or not _ARTIFACT_STEM.fullmatch(stem):
        raise TexCompileError("PDF 产物名称无效")
    if not isinstance(command, str) or not command.strip():
        raise TexCompileError("未配置 XeLaTeX 命令，请设置 XELATEX_COMMAND")
    try:
        timeout = int(timeout)
    except (TypeError, ValueError) as exc:
        raise TexCompileError("XeLaTeX 超时配置无效") from exc
    if timeout < 1 or timeout > 600:
        raise TexCompileError("XeLaTeX 超时必须在 1–600 秒之间")

    resolved_command = shutil.which(command.strip())
    if resolved_command is None:
        candidate = Path(command.strip())
        if candidate.is_file():
            resolved_command = str(candidate)
        else:
            raise TexCompileError(
                "未找到 XeLaTeX，请安装 TeX Live/MiKTeX 或设置 XELATEX_COMMAND"
            )

    output_dir = Path(pdf_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TexCompileError(f"PDF 输出目录不可用：{exc}") from exc

    target_tex = output_dir / f"{stem}.tex"
    target_pdf = output_dir / f"{stem}.pdf"
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{stem}-", dir=str(output_dir))
    )
    published = []
    compile_runner = runner or _COMPILE_RUNNER.get()
    correlation_id = uuid.uuid4().hex[:12]
    try:
        temporary_tex = temporary_dir / f"{stem}.tex"
        temporary_pdf = temporary_dir / f"{stem}.pdf"
        temporary_tex.write_text(latex, encoding="utf-8")
        args = [
            resolved_command,
            "-no-shell-escape",
            "-interaction=nonstopmode",
            "-halt-on-error",
            temporary_tex.name,
        ]
        for _ in range(2):
            try:
                # Do not use PIPE/capture_output here. On Windows, TeX launchers
                # can leave a child process holding the inherited pipe handles;
                # subprocess.run() then remains blocked after its timeout while
                # waiting for those handles to close. Temporary files preserve
                # diagnostics without allowing descendants to defeat the timeout.
                with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                    run_kwargs = {
                        "cwd": str(temporary_dir),
                        "stdout": stdout_file,
                        "stderr": stderr_file,
                        "timeout": timeout,
                    }
                    if compile_runner is None:
                        raw_result = subprocess.run(args, check=False, **run_kwargs)
                    else:
                        raw_result = compile_runner(args, **run_kwargs)
                    stdout_file.seek(0)
                    stderr_file.seek(0)
                    result = subprocess.CompletedProcess(
                        raw_result.args,
                        raw_result.returncode,
                        raw_result.stdout
                        if raw_result.stdout is not None
                        else stdout_file.read(),
                        raw_result.stderr
                        if raw_result.stderr is not None
                        else stderr_file.read(),
                    )
            except subprocess.TimeoutExpired as exc:
                captured = (exc.stderr or b"") + b"\n" + (exc.stdout or b"")
                log_name = _write_compile_log(output_dir, correlation_id, captured)
                raise TexCompileError(
                    f"XeLaTeX 编译超时（每轮限制 {timeout} 秒）",
                    code="TEX_TIMEOUT",
                    hint="请简化文档或检查是否存在无限展开的命令",
                    correlation_id=correlation_id,
                    log_name=log_name,
                ) from exc
            except OSError as exc:
                log_name = _write_compile_log(
                    output_dir, correlation_id, b"XeLaTeX process could not start.\n"
                )
                raise TexCompileError(
                    "无法启动 XeLaTeX",
                    code="TEX_START_FAILED",
                    hint="请在运行状态中检查 XeLaTeX 配置",
                    correlation_id=correlation_id,
                    log_name=log_name,
                ) from exc
            if result.returncode != 0:
                output = (result.stderr or b"") + b"\n" + (result.stdout or b"")
                raise _compile_failure(
                    output_dir,
                    output,
                    correlation_id=correlation_id,
                )

        if not temporary_pdf.is_file() or temporary_pdf.stat().st_size <= 0:
            log_name = _write_compile_log(
                output_dir,
                correlation_id,
                b"XeLaTeX exited successfully but no non-empty PDF was generated.\n",
            )
            raise TexCompileError(
                "XeLaTeX 未生成有效的 PDF 文件",
                code="TEX_PDF_MISSING",
                hint="请检查文档是否提前终止，并根据诊断编号查看日志",
                correlation_id=correlation_id,
                log_name=log_name,
            )

        os.replace(temporary_pdf, target_pdf)
        published.append(target_pdf)
        if publish_source:
            os.replace(temporary_tex, target_tex)
            published.append(target_tex)
    except Exception:
        for path in published:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


def compile_exam(
    pdf_dir: str,
    command: str,
    exam_id: int,
    latex: str,
    *,
    timeout: int = 60,
    runner=None,
) -> None:
    """Compile an exam while preserving the established exam artifact contract."""

    if not isinstance(exam_id, int) or isinstance(exam_id, bool) or exam_id <= 0:
        raise TexCompileError("试卷 ID 无效")
    compile_latex_artifact(
        pdf_dir,
        command,
        f"exam_{exam_id}",
        latex,
        timeout=timeout,
        runner=runner,
        publish_source=True,
    )
