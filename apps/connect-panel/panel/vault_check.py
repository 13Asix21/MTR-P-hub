"""T/R 与审核清单共用的单遍、只读 Obsidian 扫描引擎。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .settings import ObsidianConfig, PanelError, normalize_local_absolute_path

try:
    from mtrp_hub.adapters.obsidian_markdown import ObsidianMarkdownParser
except (ImportError, ModuleNotFoundError):
    ObsidianMarkdownParser = None  # type: ignore[assignment,misc]


_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_/-]{0,127}$")
_MAX_ISSUE_SAMPLES = 20


@dataclass(frozen=True)
class ScannedFile:
    """单遍引擎为一篇文件产出的事实；raw_bytes 仅供当次回调提取链接。"""

    entry_path: Path
    relative_posix: str
    size_bytes: int
    mtime: str
    content_sha256: str = ""
    tags: tuple[str, ...] = ()
    classification: str = "unknown"
    parse_status: str = "ok"
    error_code: str | None = None
    error_message: str | None = None
    raw_bytes: bytes = field(default=b"", repr=False, compare=False)


@dataclass(frozen=True)
class WalkerSkip:
    """目录遍历跳过或终止事件；路径仅供受控 issue 样本使用。"""

    kind: str
    relative_posix: str = ""
    code: str = ""
    message: str = ""


def _is_link_or_junction(path: Path) -> bool:
    """lstat 同时识别 POSIX symlink 与 Windows reparse point/junction。"""

    try:
        metadata = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _normalise_root(raw_root: str, field: str) -> tuple[str, ...]:
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise PanelError("ROOT_INVALID", f"{field} 中存在空路径。")
    value = raw_root.strip().replace("\\", "/").rstrip("/")
    # '.' 表示库根目录本身，与前端 scanner 的约定一致；其余位置的点仍按穿越拒绝。
    if value == ".":
        return ()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) or Path(value).is_absolute():
        raise PanelError("ROOT_MUST_BE_RELATIVE", f"{field} 必须是 Vault 内的相对路径。")
    if ":" in value:
        raise PanelError("ROOT_DRIVE_RELATIVE_REJECTED", f"{field} 不能包含盘符、冒号或 ADS 路径。")
    pure = PurePosixPath(value)
    if not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise PanelError("ROOT_TRAVERSAL_REJECTED", f"{field} 不能包含父目录跳转。")
    return pure.parts


def _normalise_roots(raw_roots: list[str] | tuple[str, ...], field: str) -> tuple[tuple[str, ...], ...]:
    if len(raw_roots) > 64:
        raise PanelError("TOO_MANY_ROOTS", f"{field} 最多允许 64 项。")
    roots = tuple(_normalise_root(item, field) for item in raw_roots)
    if field == "managed_roots" and not roots:
        raise PanelError("MANAGED_ROOT_REQUIRED", "至少需要一个 managed root。")
    return roots


def _resolve_roots_within_vault(
    root: Path,
    managed_roots: tuple[tuple[str, ...], ...],
    excluded_roots: tuple[tuple[str, ...], ...],
) -> Path:
    """扫描前解析全部候选路径，拒绝任何逃出 Vault 子树的结果。"""

    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PanelError("VAULT_RESOLVE_FAILED", "Vault 根目录无法安全解析。") from exc
    for field, roots in (("managed_roots", managed_roots), ("excluded_roots", excluded_roots)):
        for parts in roots:
            try:
                candidate = root.joinpath(*parts).resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise PanelError("ROOT_RESOLVE_FAILED", f"{field} 无法安全解析，已拒绝。") from exc
            if not candidate.is_relative_to(resolved_root):
                raise PanelError("ROOT_OUTSIDE_VAULT", f"{field} 解析后越出 Vault 根目录，已拒绝。")
    return resolved_root


def _path_key(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(part.casefold() for part in parts)


def _is_excluded(parts: tuple[str, ...], excluded: tuple[tuple[str, ...], ...]) -> bool:
    """按完整路径段前缀排除，A/B 不会误排 A/B2。"""

    key = _path_key(parts)
    return any(key[: len(root)] == _path_key(root) for root in excluded)


def _build_parser(trial_tag: str, rarely_tag: str) -> Any:
    """复用主仓解析器，并仅为面板自定义标签扩展同一套行内标签语义。"""

    if ObsidianMarkdownParser is None:
        return None
    parser = ObsidianMarkdownParser()
    if trial_tag != "mtr/trial" or rarely_tag != "mtr/rarely":
        tag_group = f"(?:{re.escape(trial_tag)}|{re.escape(rarely_tag)})"
        parser._INLINE_TAG = re.compile(  # noqa: SLF001 - 主仓解析器公开的语义扩展点不足，实例级覆盖不改主仓。
            rf"(?<![\w#])#({tag_group})(?![\w/-])",
            re.UNICODE,
        )
    return parser


def _directory_entries(directory: Path) -> list[os.DirEntry[str]]:
    with os.scandir(directory) as iterator:
        return sorted(iterator, key=lambda entry: entry.name.casefold())


def _obsidian_kind(root: Path, markdown_seen: int) -> tuple[str, str]:
    marker = root / ".obsidian"
    if not _is_link_or_junction(marker) and marker.is_dir():
        return "confirmed", "已确认 Obsidian 库"
    if markdown_seen > 0:
        return "probable", "疑似知识库目录（未见 .obsidian）"
    return "not_vault", "不像是知识库"


def _classify_document(document: Any, trial_tag: str, rarely_tag: str) -> str:
    """按既有优先级把解析结果归为 conflict/trial/rarely/unmanaged。"""

    tags = set(document.tags)
    trial = trial_tag in tags
    rarely = rarely_tag in tags
    if trial and rarely:
        return "conflict"
    if trial:
        return "trial"
    if rarely:
        return "rarely"
    return "unmanaged"


def _mtime_iso(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _scan_vault_once(
    root: Path,
    managed_parts: tuple[tuple[str, ...], ...],
    excluded_parts: tuple[tuple[str, ...], ...],
    config: ObsidianConfig,
    trial_tag: str,
    rarely_tag: str,
    *,
    on_file: Callable[[ScannedFile], None],
    on_skip: Callable[[WalkerSkip], None] | None = None,
) -> bool:
    """只遍历并读盘一次；文件解析事实通过 on_file 同步交给收集器。"""

    parser = _build_parser(trial_tag, rarely_tag)
    if parser is None:
        return False

    def emit(kind: str, relative: str = "", code: str = "", message: str = "") -> None:
        if on_skip is not None:
            on_skip(WalkerSkip(kind, relative, code, message))

    pending: list[tuple[Path, tuple[str, ...]]] = []
    for parts in reversed(managed_parts):
        managed_path = root.joinpath(*parts)
        relative = PurePosixPath(*parts).as_posix()
        if _is_link_or_junction(managed_path):
            emit("managed_root_link", relative, "LINK_SKIPPED", "managed root 是链接或 junction，已跳过。")
            continue
        if not managed_path.is_dir():
            emit("managed_root_not_found", relative, "MANAGED_ROOT_NOT_FOUND", "managed root 不存在或不是目录。")
            continue
        pending.append((managed_path, parts))

    scanned_files = 0
    while pending:
        if scanned_files >= config.max_scan_files:
            emit("limit_reached")
            break
        directory, prefix = pending.pop()
        try:
            entries = _directory_entries(directory)
        except OSError:
            emit("directory_read_failed", PurePosixPath(*prefix).as_posix(), "DIRECTORY_READ_FAILED", "目录读取失败。")
            continue

        for entry in entries:
            parts = (*prefix, entry.name)
            relative = PurePosixPath(*parts).as_posix()
            entry_path = Path(entry.path)
            if _is_excluded(parts, excluded_parts):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        emit("excluded_directory")
                    elif entry.name.lower().endswith(".md"):
                        emit("excluded_file")
                except OSError:
                    emit(
                        "excluded_stat_failed",
                        relative,
                        "FILE_STAT_FAILED",
                        "排除项的类型读取失败，仍按排除规则跳过。",
                    )
                continue
            if _is_link_or_junction(entry_path):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        emit("link_directory")
                    else:
                        emit("link_file")
                except OSError:
                    emit("link_file")
                emit("link_issue", relative, "LINK_SKIPPED", "链接或 junction 已跳过，未跟随目标。")
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append((entry_path, parts))
                    continue
                if not entry.is_file(follow_symlinks=False) or not entry.name.lower().endswith(".md"):
                    continue
            except OSError:
                emit("file_stat_failed", relative, "FILE_STAT_FAILED", "文件类型读取失败。")
                continue

            if scanned_files >= config.max_scan_files:
                emit("limit_reached")
                pending.clear()
                break
            scanned_files += 1
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                on_file(
                    ScannedFile(
                        entry_path=entry_path,
                        relative_posix=relative,
                        size_bytes=0,
                        mtime="",
                        parse_status="FILE_READ_FAILED",
                        error_code="FILE_READ_FAILED",
                        error_message="文件读取失败。",
                    )
                )
                continue

            size_bytes = metadata.st_size
            mtime = _mtime_iso(metadata.st_mtime)
            if size_bytes > config.max_file_bytes:
                on_file(
                    ScannedFile(
                        entry_path=entry_path,
                        relative_posix=relative,
                        size_bytes=size_bytes,
                        mtime=mtime,
                        parse_status="FILE_TOO_LARGE",
                        error_code="FILE_TOO_LARGE",
                        error_message="文件超过单文件读取上限，已跳过。",
                    )
                )
                continue
            try:
                raw_bytes = entry_path.read_bytes()
            except OSError:
                on_file(
                    ScannedFile(
                        entry_path=entry_path,
                        relative_posix=relative,
                        size_bytes=size_bytes,
                        mtime=mtime,
                        parse_status="FILE_READ_FAILED",
                        error_code="FILE_READ_FAILED",
                        error_message="文件读取失败。",
                    )
                )
                continue

            content_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            try:
                document = parser.parse(raw_bytes, relative)
            except ValueError as exc:
                code = str(getattr(exc, "error_code", "MARKDOWN_PARSE_FAILED"))
                on_file(
                    ScannedFile(
                        entry_path=entry_path,
                        relative_posix=relative,
                        size_bytes=size_bytes,
                        mtime=mtime,
                        content_sha256=content_sha256,
                        parse_status=code,
                        error_code=code,
                        error_message="Markdown 编码或 frontmatter 无法解析。",
                        raw_bytes=raw_bytes,
                    )
                )
                continue
            except Exception:
                on_file(
                    ScannedFile(
                        entry_path=entry_path,
                        relative_posix=relative,
                        size_bytes=size_bytes,
                        mtime=mtime,
                        content_sha256=content_sha256,
                        parse_status="MARKDOWN_PARSE_FAILED",
                        error_code="MARKDOWN_PARSE_FAILED",
                        error_message="文件解析失败，已跳过。",
                        raw_bytes=raw_bytes,
                    )
                )
                continue

            on_file(
                ScannedFile(
                    entry_path=entry_path,
                    relative_posix=relative,
                    size_bytes=size_bytes,
                    mtime=mtime,
                    content_sha256=document.content_sha256,
                    tags=tuple(document.tags),
                    classification=_classify_document(document, trial_tag, rarely_tag),
                    parse_status="ok",
                    raw_bytes=raw_bytes,
                )
            )
        if scanned_files >= config.max_scan_files and not pending:
            # 与 v1 一致：只有还存在未处理项时才在下一轮/下一文件标记截断。
            continue
    return True


def _new_statistics() -> dict[str, int | bool]:
    return {
        "scanned_files": 0,
        "parsed_files": 0,
        "trial_notes": 0,
        "rarely_notes": 0,
        "unmanaged_notes": 0,
        "conflict_notes": 0,
        "skipped_files": 0,
        "skipped_directories": 0,
        "excluded_directories": 0,
        "error_count": 0,
        "limit_reached": False,
    }


def _collect_file_statistics(
    statistics: dict[str, int | bool],
    scanned: ScannedFile,
    add_issue: Callable[[str, str, str], None] | None = None,
) -> None:
    """两个收集器共享同一份 v1 文件级计数逻辑。"""

    statistics["scanned_files"] = int(statistics["scanned_files"]) + 1
    if scanned.parse_status == "FILE_TOO_LARGE":
        statistics["skipped_files"] = int(statistics["skipped_files"]) + 1
    elif scanned.parse_status != "ok":
        statistics["error_count"] = int(statistics["error_count"]) + 1
    else:
        statistics["parsed_files"] = int(statistics["parsed_files"]) + 1
        key = f"{scanned.classification}_notes"
        statistics[key] = int(statistics[key]) + 1
    if add_issue is not None and scanned.error_code:
        add_issue(scanned.relative_posix, scanned.error_code, scanned.error_message or "文件处理失败。")


def _collect_skip_statistics(
    statistics: dict[str, int | bool],
    event: WalkerSkip,
    add_issue: Callable[[str, str, str], None] | None = None,
) -> None:
    """两个收集器共享同一份 v1 目录/排除/链接计数逻辑。"""

    kind = event.kind
    if kind in {"managed_root_link", "link_directory"}:
        statistics["skipped_directories"] = int(statistics["skipped_directories"]) + 1
    elif kind == "managed_root_not_found":
        statistics["error_count"] = int(statistics["error_count"]) + 1
    elif kind == "limit_reached":
        statistics["limit_reached"] = True
    elif kind in {"directory_read_failed", "excluded_stat_failed", "file_stat_failed"}:
        statistics["error_count"] = int(statistics["error_count"]) + 1
    elif kind == "excluded_directory":
        statistics["excluded_directories"] = int(statistics["excluded_directories"]) + 1
    elif kind in {"excluded_file", "link_file"}:
        statistics["skipped_files"] = int(statistics["skipped_files"]) + 1
    if add_issue is not None and event.code:
        add_issue(event.relative_posix, event.code, event.message)


def check_vault(
    raw_path: str,
    config: ObsidianConfig,
    *,
    managed_roots: list[str] | None = None,
    excluded_roots: list[str] | None = None,
    trial_tag: str | None = None,
    rarely_tag: str | None = None,
) -> dict[str, Any]:
    """以纯聚合收集器驱动单遍引擎，保持 v1 对外行为。"""

    root = normalize_local_absolute_path(raw_path)
    selected_managed = _normalise_roots(
        list(config.managed_roots) if managed_roots is None else managed_roots,
        "managed_roots",
    )
    selected_excluded = _normalise_roots(
        list(config.excluded_roots) if excluded_roots is None else excluded_roots,
        "excluded_roots",
    )
    selected_trial = (trial_tag or config.trial_tag).strip().lstrip("#")
    selected_rarely = (rarely_tag or config.rarely_tag).strip().lstrip("#")
    if not _TAG_PATTERN.fullmatch(selected_trial) or not _TAG_PATTERN.fullmatch(selected_rarely):
        raise PanelError("TAG_INVALID", "标签只能包含字母、数字、下划线、斜杠和连字符。")
    if selected_trial == selected_rarely:
        raise PanelError("TAGS_MUST_DIFFER", "trial 与 rarely 标签不能相同。")

    result: dict[str, Any] = {
        "status": "pending",
        "path": str(root),
        "managed_roots": [PurePosixPath(*parts).as_posix() for parts in selected_managed],
        "excluded_roots": [PurePosixPath(*parts).as_posix() for parts in selected_excluded],
        "trial_tag": selected_trial,
        "rarely_tag": selected_rarely,
        "read_only": True,
        "follow_symlinks": False,
        "scan_limit": config.max_scan_files,
    }
    if _is_link_or_junction(root):
        raise PanelError("VAULT_LINK_REJECTED", "Vault 根目录不能是符号链接或 junction。")
    if not root.exists():
        result.update(status="error", error={"code": "DIRECTORY_NOT_FOUND", "message": "目录不存在或当前无法访问。"})
        return result
    if not root.is_dir():
        result.update(status="error", error={"code": "NOT_A_DIRECTORY", "message": "输入路径不是目录。"})
        return result

    root = _resolve_roots_within_vault(root, selected_managed, selected_excluded)
    statistics = _new_statistics()
    issues: list[dict[str, str]] = []

    def add_issue(relative_path: str, code: str, message: str) -> None:
        if len(issues) < _MAX_ISSUE_SAMPLES:
            issues.append({"path": relative_path, "code": code, "message": message})

    def on_file(scanned: ScannedFile) -> None:
        _collect_file_statistics(statistics, scanned, add_issue)

    def on_skip(event: WalkerSkip) -> None:
        _collect_skip_statistics(statistics, event, add_issue)

    parser_available = _scan_vault_once(
        root,
        selected_managed,
        selected_excluded,
        config,
        selected_trial,
        selected_rarely,
        on_file=on_file,
        on_skip=on_skip,
    )
    if not parser_available:
        result.update(
            obsidian={"kind": "unknown", "label": "无法判定", "obsidian_directory": False},
            status="error",
            parser_available=False,
            error={
                "code": "PARSER_UNAVAILABLE",
                "message": "主仓 mtrp_hub Markdown 解析器导入失败；请使用项目 .venv 启动面板。未执行扫描。",
            },
        )
        return result

    kind, label = _obsidian_kind(root, int(statistics["scanned_files"]))
    has_errors = int(statistics["error_count"]) > 0
    if int(statistics["parsed_files"]) == 0 and has_errors and int(statistics["scanned_files"]) == 0:
        status = "error"
    else:
        status = "limited" if statistics["limit_reached"] or has_errors else "ok"

    result.update(
        status=status,
        parser_available=True,
        parser_source="mtrp_hub.adapters.obsidian_markdown.ObsidianMarkdownParser",
        obsidian={
            "kind": kind,
            "label": label,
            "obsidian_directory": kind == "confirmed",
        },
        statistics=statistics,
        issues=issues,
        issues_truncated=len(issues) >= _MAX_ISSUE_SAMPLES,
        views={
            "T": {
                "status": status,
                "tag": selected_trial,
                "note_count": statistics["trial_notes"],
                "reserved": False,
            },
            "R": {
                "status": status,
                "tag": selected_rarely,
                "note_count": statistics["rarely_notes"],
                "reserved": True,
                "lifecycle_note": "T↔R 下沉/复活提案属于阶段 3；本面板仅做只读统计。",
            },
        },
    )
    return result


__all__ = ["check_vault"]
