"""Pure parsing of explicitly supplied Markdown Fixture text; never file I/O."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from mtrp_hub.canonical import sha256_bytes
from mtrp_hub.domain.models import FrozenModel


class FixtureMarkdownError(ValueError):
    """A Fake document contains malformed frontmatter/YAML."""


class ParsedFixtureMarkdown(FrozenModel):
    """Metadata derived from Fixture text; the body is never retained in results."""

    tags: tuple[str, ...]
    title: str
    content_sha256: str
    classification: Literal["managed", "unmanaged"]
    needs_classification: bool


def parse_fixture_markdown(text: str, *, logical_path: str = "fixture.md") -> ParsedFixtureMarkdown:
    """Parse only the supplied string and classify exact MTR frontmatter tags.

    No path is opened, no directory is discovered, and no input text is modified.
    Documents without frontmatter are always unmanaged/needs_classification.
    """
    content_sha256 = sha256_bytes(text.encode("utf-8"))
    fallback_title = PurePosixPath(logical_path).stem or "Fixture"
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return ParsedFixtureMarkdown(
            tags=(),
            title=fallback_title,
            content_sha256=content_sha256,
            classification="unmanaged",
            needs_classification=True,
        )
    lines = text.splitlines(keepends=True)
    closing_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        raise FixtureMarkdownError("Unclosed Fixture frontmatter")
    frontmatter_text = "".join(lines[1:closing_index])
    try:
        data = YAML(typ="safe").load(frontmatter_text)
    except YAMLError as exc:
        raise FixtureMarkdownError("Invalid Fixture frontmatter YAML") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FixtureMarkdownError("Fixture frontmatter must be a mapping")
    raw_tags = data.get("tags", [])
    if isinstance(raw_tags, str):
        tags = tuple(part.lstrip("#") for part in raw_tags.split() if part.strip())
    elif isinstance(raw_tags, (list, tuple)) and all(isinstance(item, str) for item in raw_tags):
        tags = tuple(item.lstrip("#") for item in raw_tags)
    else:
        raise FixtureMarkdownError("Fixture tags must be a string or string list")
    normalized_tags = tuple(sorted(set(tags)))
    raw_title = data.get("title", fallback_title)
    title = raw_title if isinstance(raw_title, str) and raw_title.strip() else fallback_title
    managed = "mtr/trial" in normalized_tags or "mtr/rarely" in normalized_tags
    return ParsedFixtureMarkdown(
        tags=normalized_tags,
        title=title,
        content_sha256=content_sha256,
        classification="managed" if managed else "unmanaged",
        needs_classification=not managed,
    )
