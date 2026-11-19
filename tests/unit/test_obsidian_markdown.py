"""Real Markdown frontmatter/inline tag, code exclusion, BOM, CRLF, and hash tests."""

from __future__ import annotations

import pytest

from mtrp_hub.adapters.obsidian_markdown import ObsidianMarkdownError, ObsidianMarkdownParser
from mtrp_hub.canonical import sha256_bytes


def parse(text: bytes):  # type: ignore[no-untyped-def]
    return ObsidianMarkdownParser().parse(text, "目录/note.md")


def test_frontmatter_string_list_and_inline_tags_merge() -> None:
    assert parse(b"---\ntags: mtr/trial other\n---\nbody\n").classification == "trial"
    assert parse(b"---\ntags: [mtr/rarely]\n---\nbody\n").classification == "rarely"
    assert parse(b"body with #mtr/trial\n").classification == "trial"
    assert parse(b"body with #mtr/rarely\n").classification == "rarely"


def test_plain_mentions_status_stale_and_code_are_not_tags() -> None:
    raw = (
        b"plain mtr/trial and status/stale\n"
        b"```md\n#mtr/trial\n```\n"
        b"~~~\n#mtr/rarely\n~~~\n"
        b"    #mtr/trial\n"
        b"inline `#mtr/rarely` code\n"
    )
    result = parse(raw)
    assert result.classification == "unmanaged" and result.needs_classification is True


def test_dual_tags_conflict_and_frontmatter_is_not_rescanned() -> None:
    result = parse(b"---\ntags: [mtr/trial]\n---\n#mtr/rarely\n")
    assert result.classification == "conflict" and result.reason_code == "MTR_TAG_CONFLICT"
    frontmatter_only = parse(b"---\ntags: [mtr/trial]\n---\nbody\n")
    assert frontmatter_only.tags == ("mtr/trial",)


def test_bom_crlf_hash_uses_exact_original_bytes() -> None:
    raw = b"\xef\xbb\xbf---\r\ntitle: Synthetic\r\ntags: [mtr/trial]\r\n---\r\nbody\r\n"
    result = parse(raw)
    assert result.title == "Synthetic"
    assert result.content_sha256 == sha256_bytes(raw)
    assert result.content_sha256 != sha256_bytes(raw.replace(b"\r\n", b"\n"))


def test_invalid_yaml_and_non_utf8_are_file_level_errors() -> None:
    with pytest.raises(ObsidianMarkdownError) as caught:
        parse(b"---\ntags: [mtr/trial\n---\n")
    assert caught.value.error_code == "INVALID_YAML"
    with pytest.raises(ObsidianMarkdownError) as caught:
        parse(b"\xff\xfe")
    assert caught.value.error_code == "INVALID_ENCODING"


def test_fence_length_and_inline_backticks_do_not_leak_tags() -> None:
    result = parse(b"````markdown\n```\n#mtr/trial\n```\n````\n``#mtr/rarely``\n")
    assert result.classification == "unmanaged"
