"""审核工作台的单遍双口径收集、单篇现算与确定性 wiki 反链。"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ruamel.yaml import YAML

from .settings import ObsidianConfig, PanelError, normalize_local_absolute_path
from .vault_check import (
    ScannedFile,
    WalkerSkip,
    _build_parser,
    _classify_document,
    _collect_file_statistics,
    _collect_skip_statistics,
    _is_link_or_junction,
    _new_statistics,
    _normalise_roots,
    _resolve_roots_within_vault,
    _scan_vault_once,
)


_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_/-]{0,127}$")
_INLINE_CODE = re.compile(r"(`+)(.*?)\1")
_WIKI_LINK = re.compile(r"\[\[([^\[\]\r\n]+)\]\]")


@dataclass(frozen=True)
class CurrentFile:
    """一次单文件安全读取的原始字节与元信息。"""

    raw_bytes: bytes = field(repr=False)
    content_sha256: str
    size_bytes: int
    mtime: str


def _sha256_bytes(raw_bytes: bytes) -> str:
    """计算包含 BOM、CRLF 等全部原始字节的 SHA-256。"""

    return hashlib.sha256(raw_bytes).hexdigest()


def _nfc(path: str) -> str:
    """生成审核域统一使用的 NFC POSIX 路径键。"""

    return unicodedata.normalize("NFC", path)


def _extract_wiki_links(lines: list[str]) -> list[str]:
    """提取代码围栏、缩进代码与行内代码之外的 wiki 链接目标。"""

    targets: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in lines:
        without_newline = line.rstrip("\r\n")
        if fence_character is not None:
            closing = re.match(
                rf"^ {{0,3}}{re.escape(fence_character)}{{{fence_length},}}\s*$",
                without_newline,
            )
            if closing:
                fence_character = None
                fence_length = 0
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3})", without_newline)
        if opening:
            marker = opening.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            continue
        if without_newline.startswith(("    ", "\t")):
            continue
        visible = _INLINE_CODE.sub("", without_newline)
        for match in _WIKI_LINK.finditer(visible):
            target = match.group(1).split("|", 1)[0].strip()
            if target:
                targets.append(_nfc(target))
    return targets


def _link_variants(path: str) -> tuple[str, ...]:
    normalized = _nfc(path).casefold()
    if normalized.endswith(".md"):
        return (normalized, normalized[:-3])
    return (normalized, normalized + ".md")


def _resolve_links(items: list[dict[str, Any]]) -> None:
    """以全路径优先、basename 全局唯一次之的方式计算反链。"""

    full_index: dict[str, dict[str, Any] | None] = {}
    basename_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        path = item["vault_relative_path"]
        for variant in _link_variants(path):
            if variant in full_index:
                full_index[variant] = None
            else:
                full_index[variant] = item
        basename = PurePosixPath(path).name
        for variant in _link_variants(basename):
            basename_candidates[variant].append(item)

    for item in items:
        for raw_target in item.get("_link_targets", []):
            target = raw_target.split("#", 1)[0].strip()
            if not target:
                continue
            resolved: dict[str, Any] | None = None
            for variant in _link_variants(target):
                candidate = full_index.get(variant)
                if candidate is not None:
                    resolved = candidate
                    break
            if resolved is None:
                basename = PurePosixPath(target).name
                for variant in _link_variants(basename):
                    candidates = basename_candidates.get(variant, [])
                    unique = {id(candidate): candidate for candidate in candidates}
                    if len(unique) == 1:
                        resolved = next(iter(unique.values()))
                        break
            if resolved is not None:
                resolved["links_in"] = int(resolved["links_in"]) + 1
        item.pop("_link_targets", None)


def _empty_counts() -> dict[str, Any]:
    return {
        "parsed_ok": 0,
        "invalid": 0,
        "file_too_large": 0,
        "mtr": {"trial": 0, "rarely": 0, "unmanaged": 0, "conflict": 0},
    }


def _item_from_scanned(scanned: ScannedFile) -> dict[str, Any]:
    item: dict[str, Any] = {
        "vault_relative_path": _nfc(scanned.relative_posix),
        "sha256": scanned.content_sha256,
        "size_bytes": scanned.size_bytes,
        "mtime": scanned.mtime,
        "tags": list(scanned.tags),
        "mtr_state": scanned.classification if scanned.parse_status == "ok" else "unknown",
        "parse_status": scanned.parse_status,
        "links_out": 0,
        "links_in": 0,
    }
    if scanned.raw_bytes and scanned.parse_status != "INVALID_ENCODING":
        try:
            text = scanned.raw_bytes.decode("utf-8-sig", errors="strict")
        except UnicodeError:
            text = ""
        if text:
            targets = _extract_wiki_links(text.splitlines(keepends=True))
            item["links_out"] = len(targets)
            item["_link_targets"] = targets
    return item


def scan_review_listing(
    raw_path: str,
    config: ObsidianConfig,
    managed_roots: list[str] | None = None,
    excluded_roots: list[str] | None = None,
) -> dict[str, Any]:
    """同一次 on_file 回调同时累加 v1 statistics 与审核 items。"""

    root = normalize_local_absolute_path(raw_path)
    selected_managed = _normalise_roots(
        list(config.managed_roots) if managed_roots is None else managed_roots,
        "managed_roots",
    )
    selected_excluded = _normalise_roots(
        list(config.excluded_roots) if excluded_roots is None else excluded_roots,
        "excluded_roots",
    )
    selected_trial = config.trial_tag.strip().lstrip("#")
    selected_rarely = config.rarely_tag.strip().lstrip("#")
    if not _TAG_PATTERN.fullmatch(selected_trial) or not _TAG_PATTERN.fullmatch(selected_rarely):
        raise PanelError("TAG_INVALID", "标签只能包含字母、数字、下划线、斜杠和连字符。")
    if selected_trial == selected_rarely:
        raise PanelError("TAGS_MUST_DIFFER", "trial 与 rarely 标签不能相同。")

    statistics = _new_statistics()
    result: dict[str, Any] = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "path": str(root),
        "managed_roots": [PurePosixPath(*parts).as_posix() for parts in selected_managed],
        "excluded_roots": [PurePosixPath(*parts).as_posix() for parts in selected_excluded],
        "status": "pending",
        "limit_reached": False,
        "statistics": statistics,
        "total": 0,
        "counts": _empty_counts(),
        "items": [],
        "warnings": [],
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
    items: list[dict[str, Any]] = []
    source_paths: dict[str, str] = {}

    def on_file(scanned: ScannedFile) -> None:
        _collect_file_statistics(statistics, scanned)
        item = _item_from_scanned(scanned)
        items.append(item)
        source_paths[item["vault_relative_path"]] = scanned.relative_posix

    def on_skip(event: WalkerSkip) -> None:
        _collect_skip_statistics(statistics, event)

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
            scanned_at=datetime.now(timezone.utc).isoformat(),
            status="error",
            error={
                "code": "PARSER_UNAVAILABLE",
                "message": "主仓 mtrp_hub Markdown 解析器导入失败；请使用项目 .venv 启动面板。未执行扫描。",
            },
        )
        return result

    _resolve_links(items)
    items.sort(key=lambda entry: entry["vault_relative_path"].casefold())
    normalized_paths = [entry["vault_relative_path"] for entry in items]
    if len(set(normalized_paths)) != len(normalized_paths):
        result["warnings"].append("NFC 归一后存在同键路径；清单保留全部条目，请人工核对。")

    counts = _empty_counts()
    for item in items:
        parse_status = item["parse_status"]
        if parse_status == "ok":
            counts["parsed_ok"] += 1
            counts["mtr"][item["mtr_state"]] += 1
        elif parse_status == "FILE_TOO_LARGE":
            counts["file_too_large"] += 1
        else:
            counts["invalid"] += 1

    has_errors = int(statistics["error_count"]) > 0
    if int(statistics["parsed_files"]) == 0 and has_errors and int(statistics["scanned_files"]) == 0:
        status = "error"
    else:
        status = (
            "limited"
            if statistics["limit_reached"] or has_errors or counts["file_too_large"] > 0
            else "ok"
        )
    result.update(
        scanned_at=datetime.now(timezone.utc).isoformat(),
        status=status,
        limit_reached=bool(statistics["limit_reached"]),
        total=len(items),
        counts=counts,
        items=items,
        _source_paths=source_paths,
    )
    return result


def _format_mtime(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def read_current_file(root: Path, item: Mapping[str, Any], config: ObsidianConfig) -> CurrentFile:
    """对清单成员做单文件安全复查并现算 SHA；绝不跟随链接或越根。"""

    relative = str(item.get("_source_relative_path", item["vault_relative_path"]))
    parts = relative.split("/")
    if _is_link_or_junction(root):
        raise PanelError("REVIEW_PATH_INVALID", "Vault 根目录已变为链接，已拒绝读取。", 400)
    current = root
    for part in parts[:-1]:
        current = current / part
        if _is_link_or_junction(current):
            raise PanelError("REVIEW_PATH_INVALID", "目标路径包含链接或 junction，已拒绝读取。", 400)
    candidate = root.joinpath(*parts)
    if _is_link_or_junction(candidate):
        raise PanelError("REVIEW_PATH_INVALID", "目标文件已变为链接或 junction，已拒绝读取。", 400)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise PanelError("REVIEW_FILE_MISSING", "清单中的文件已消失或当前无法读取。", 404) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise PanelError("REVIEW_FILE_MISSING", "清单中的文件已消失或不再是普通文件。", 404)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PanelError("REVIEW_FILE_MISSING", "目标文件无法安全解析。", 404) from exc
    if not resolved_candidate.is_relative_to(resolved_root):
        raise PanelError("REVIEW_PATH_INVALID", "目标文件解析后越出 Vault，已拒绝读取。", 400)
    if metadata.st_size > config.max_file_bytes:
        raise PanelError("FILE_TOO_LARGE", "文件超过单文件读取上限，未读取正文。", 413)
    try:
        raw_bytes = candidate.read_bytes()
    except OSError as exc:
        raise PanelError("REVIEW_FILE_MISSING", "清单中的文件已消失或当前无法读取。", 404) from exc
    if len(raw_bytes) > config.max_file_bytes:
        raise PanelError("FILE_TOO_LARGE", "文件读取期间超过单文件上限，已拒绝处理。", 413)
    return CurrentFile(
        raw_bytes=raw_bytes,
        content_sha256=_sha256_bytes(raw_bytes),
        size_bytes=len(raw_bytes),
        mtime=_format_mtime(metadata.st_mtime),
    )


def _frontmatter_value(value: Any) -> tuple[str, str]:
    if value is None:
        return "", "null"
    if isinstance(value, bool):
        return ("true" if value else "false"), "boolean"
    if isinstance(value, str):
        return value, "string"
    if isinstance(value, int):
        return str(value), "integer"
    if isinstance(value, float):
        return str(value), "number"
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, default=str), "array"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str), "object"
    return str(value), type(value).__name__


def _split_frontmatter(text: str, parse_status: str) -> tuple[dict[str, Any], str]:
    frontmatter: dict[str, Any] = {"raw": "", "entries": [], "parse_status": parse_status}
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return frontmatter, text
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        frontmatter["raw"] = "".join(lines[1:])
        return frontmatter, text
    raw = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])
    frontmatter["raw"] = raw
    if parse_status == "ok":
        yaml = YAML(typ="safe")
        yaml.allow_duplicate_keys = False
        data = yaml.load(raw)
        if isinstance(data, dict):
            entries = []
            for key, value in data.items():
                rendered, value_type = _frontmatter_value(value)
                entries.append({"key": str(key), "value": rendered, "value_type": value_type})
            frontmatter["entries"] = entries
    return frontmatter, body


def load_file_detail(root: Path, item: Mapping[str, Any], config: ObsidianConfig) -> dict[str, Any]:
    """按需重读单篇，现算元数据、frontmatter、正文与出链。"""

    current = read_current_file(root, item, config)
    detail_item = dict(item)
    detail_item.pop("_source_relative_path", None)
    detail_item.update(
        sha256=current.content_sha256,
        size_bytes=current.size_bytes,
        mtime=current.mtime,
        tags=[],
        mtr_state="unknown",
        parse_status="ok",
        links_out=0,
    )
    parser = _build_parser(config.trial_tag, config.rarely_tag)
    if parser is None:
        raise PanelError("PARSER_UNAVAILABLE", "Markdown 解析器当前不可用。", 503)
    try:
        document = parser.parse(current.raw_bytes, str(item["vault_relative_path"]))
    except ValueError as exc:
        detail_item["parse_status"] = str(getattr(exc, "error_code", "MARKDOWN_PARSE_FAILED"))
    except Exception:
        detail_item["parse_status"] = "MARKDOWN_PARSE_FAILED"
    else:
        detail_item.update(
            tags=list(document.tags),
            mtr_state=_classify_document(document, config.trial_tag, config.rarely_tag),
            parse_status="ok",
            sha256=document.content_sha256,
        )

    try:
        text = current.raw_bytes.decode("utf-8-sig", errors="strict")
    except UnicodeError:
        text = ""
        detail_item["parse_status"] = "INVALID_ENCODING"
        detail_item["mtr_state"] = "unknown"
    if text and detail_item["parse_status"] != "INVALID_ENCODING":
        targets = _extract_wiki_links(text.splitlines(keepends=True))
        detail_item["links_out"] = len(targets)
    frontmatter, body = _split_frontmatter(text, str(detail_item["parse_status"]))
    return {
        "item": detail_item,
        "frontmatter": frontmatter,
        "body": {"text": body, "raw_only": detail_item["parse_status"] != "ok"},
        "links": {"out": detail_item["links_out"], "in": detail_item.get("links_in", 0)},
    }


__all__ = ["load_file_detail", "read_current_file", "scan_review_listing"]
