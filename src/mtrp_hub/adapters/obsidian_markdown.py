"""Real Obsidian Markdown tag parsing with exact raw-byte hashing."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from mtrp_hub.canonical import sha256_bytes
from mtrp_hub.domain.models import FrozenModel


class ObsidianMarkdownError(ValueError):
    def __init__(self, code: str) -> None:
        self.error_code = code
        super().__init__(code)


class ParsedObsidianDocument(FrozenModel):
    tags: tuple[str, ...]
    title: str
    content_sha256: str
    classification: Literal["trial", "rarely", "conflict", "unmanaged"]
    needs_classification: bool
    reason_code: Literal["MTR_TAG_CONFLICT"] | None = None


class ObsidianMarkdownParser:
    """Parse frontmatter and real inline tags while excluding every code form."""

    _FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(?:[^`]*)$")
    _INLINE_TAG = re.compile(r"(?<![\w#])#(mtr/(?:trial|rarely))(?![\w/-])", re.UNICODE)
    _INLINE_CODE = re.compile(r"(`+)(.*?)\1")

    def parse(self, raw_bytes: bytes, relative_path: str) -> ParsedObsidianDocument:
        content_sha256 = sha256_bytes(raw_bytes)
        try:
            text = raw_bytes.decode("utf-8-sig", errors="strict")
        except UnicodeError as exc:
            raise ObsidianMarkdownError("INVALID_ENCODING") from exc
        lines = text.splitlines(keepends=True)
        frontmatter_end = self._frontmatter_end(lines)
        frontmatter_tags: tuple[str, ...] = ()
        fallback_title = PurePosixPath(relative_path).stem or "Note"
        title = fallback_title
        if frontmatter_end is not None:
            frontmatter_text = "".join(lines[1:frontmatter_end])
            try:
                yaml = YAML(typ="safe")
                yaml.allow_duplicate_keys = False
                data = yaml.load(frontmatter_text)
            except (YAMLError, ValueError) as exc:
                raise ObsidianMarkdownError("INVALID_YAML") from exc
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise ObsidianMarkdownError("INVALID_YAML")
            raw_tags = data.get("tags", [])
            if isinstance(raw_tags, str):
                frontmatter_tags = tuple(part.lstrip("#") for part in raw_tags.split() if part.strip())
            elif isinstance(raw_tags, (list, tuple)) and all(isinstance(item, str) for item in raw_tags):
                frontmatter_tags = tuple(item.lstrip("#") for item in raw_tags)
            else:
                raise ObsidianMarkdownError("INVALID_YAML")
            raw_title = data.get("title", fallback_title)
            if isinstance(raw_title, str) and raw_title.strip():
                title = raw_title.strip()
            body_lines = lines[frontmatter_end + 1 :]
        else:
            body_lines = lines
        inline_tags = self.inline_tags_outside_code(body_lines)
        tags = tuple(sorted(set(frontmatter_tags + inline_tags)))
        trial = "mtr/trial" in tags
        rarely = "mtr/rarely" in tags
        if trial and rarely:
            return ParsedObsidianDocument(
                tags=tags,
                title=title,
                content_sha256=content_sha256,
                classification="conflict",
                needs_classification=False,
                reason_code="MTR_TAG_CONFLICT",
            )
        classification = "trial" if trial else "rarely" if rarely else "unmanaged"
        return ParsedObsidianDocument(
            tags=tags,
            title=title,
            content_sha256=content_sha256,
            classification=classification,
            needs_classification=not (trial or rarely),
        )

    @staticmethod
    def _frontmatter_end(lines: list[str]) -> int | None:
        if not lines or lines[0].strip() != "---":
            return None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return index
        raise ObsidianMarkdownError("INVALID_YAML")

    def frontmatter_tags(self, lines: list[str]) -> tuple[str, ...]:
        """Compatibility helper for focused parser tests; full parse remains preferred."""
        end = self._frontmatter_end(lines)
        if end is None:
            return ()
        parsed = self.parse(("".join(lines)).encode("utf-8"), "fixture.md")
        return parsed.tags

    def inline_tags_outside_code(self, lines: list[str]) -> tuple[str, ...]:
        tags: set[str] = set()
        fence_character: str | None = None
        fence_length = 0
        for line in lines:
            without_newline = line.rstrip("\r\n")
            if fence_character is not None:
                closing = re.match(rf"^ {{0,3}}{re.escape(fence_character)}{{{fence_length},}}\s*$", without_newline)
                if closing:
                    fence_character = None
                    fence_length = 0
                continue
            opening = re.match(r"^ {0,3}(`{3,}|~{3,})", without_newline)
            if opening:
                marker = opening.group(1)
                fence_character = marker[0]
                fence_length = len(marker)
                continue
            if without_newline.startswith(("    ", "\t")):
                continue
            visible = self._INLINE_CODE.sub("", without_newline)
            tags.update(match.group(1) for match in self._INLINE_TAG.finditer(visible))
        return tuple(sorted(tags))


__all__ = ["ObsidianMarkdownError", "ObsidianMarkdownParser", "ParsedObsidianDocument"]
