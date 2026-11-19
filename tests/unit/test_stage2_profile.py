"""Closed tool IDs, exact arguments, and real/synthetic raw response contracts."""

from __future__ import annotations

import pytest

from mtrp_hub.errors import MtrpError
from mtrp_hub.execution.envelope import RawResponse
from mtrp_hub.execution.models import ExecutionMode, ManifestStep, Stage2Operation
from mtrp_hub.execution.profile import (
    GITHUB_GET_FILE_TOOL,
    LEXIANG_OUTER_TOOL,
    STAGE2_PROFILE,
    ResponseContractError,
)


def remote_step(operation: Stage2Operation, arguments: dict) -> ManifestStep:
    return ManifestStep(
        step_id="s01-m-01-test",
        route_step_id="s01-m",
        executor="workbuddy",
        adapter="lexiang-m" if operation.value.startswith("lexiang") else "github-p",
        operation=operation,
        arguments=arguments,
        on_empty="continue",
        on_unavailable="continue",
    )


def test_outer_inner_tool_names_and_actual_arguments_are_profile_constants() -> None:
    step = remote_step(
        Stage2Operation.LEXIANG_SEARCH_VECTOR,
        {
            "space_id": "0" * 32,
            "filters": {"keyword": "synthetic"},
            "limit": 5,
            "threshold": 0.3,
        },
    )
    STAGE2_PROFILE.validate_arguments(step)
    call = STAGE2_PROFILE.materialize_calls(step, step.arguments)[0]
    assert call.tool_id == LEXIANG_OUTER_TOOL
    assert call.inner_tool_name == "search_kb_embedding_search"
    assert call.arguments == {
        "tool_name": "search_kb_embedding_search",
        "arguments": {"filters": {"keyword": "synthetic"}, "space_id": "0" * 32, "limit": 5, "threshold": 0.3},
    }


def test_extra_argument_is_rejected_before_transport() -> None:
    step = remote_step(
        Stage2Operation.LEXIANG_SEARCH_TEXT,
        {"space_id": "0" * 32, "keyword": "x", "limit": 5, "url": "https://invalid.example"},
    )
    with pytest.raises(MtrpError) as caught:
        STAGE2_PROFILE.validate_arguments(step)
    assert caught.value.error_code == "ARGUMENT_SCHEMA_INVALID"


def test_real_empty_search_is_legal_but_unknown_nonempty_fails_closed() -> None:
    empty = RawResponse(kind="json", data={"code": 0, "message": "success", "data": {}, "request_id": "synthetic"})
    assert STAGE2_PROFILE.parse_search(empty, Stage2Operation.LEXIANG_SEARCH_TEXT, ExecutionMode.WORKBUDDY_MANIFEST, limit=5)[0] == ()
    nonempty = RawResponse(
        kind="json",
        data={"code": 0, "message": "success", "data": {"entries": []}, "request_id": "synthetic"},
    )
    with pytest.raises(ResponseContractError) as caught:
        STAGE2_PROFILE.parse_search(nonempty, Stage2Operation.LEXIANG_SEARCH_TEXT, ExecutionMode.WORKBUDDY_MANIFEST, limit=5)
    assert caught.value.error_code == "M_NONEMPTY_CONTRACT_UNCONFIRMED"


def test_real_github_text_header_is_strict_and_hash_scope_is_honest() -> None:
    response = RawResponse(
        kind="text",
        text="successfully downloaded text file (SHA: " + "a" * 40 + ")\nREADME synthetic\n",
    )
    parsed = STAGE2_PROFILE.parse_file(response, ExecutionMode.WORKBUDDY_MANIFEST)
    assert parsed.content == b"README synthetic\n"
    assert parsed.content_hash_scope == "response_utf8"
    assert len(parsed.content_sha256) == 64
    malformed = RawResponse(kind="text", text="successfully downloaded text file\nREADME synthetic\n")
    with pytest.raises(ResponseContractError):
        STAGE2_PROFILE.parse_file(malformed, ExecutionMode.WORKBUDDY_MANIFEST)


def test_multiple_download_headers_cannot_be_accepted_as_content() -> None:
    header = "successfully downloaded text file (SHA: " + "f" * 40 + ")\n"
    with pytest.raises(ResponseContractError) as caught:
        STAGE2_PROFILE.parse_file(
            RawResponse(kind="text", text=header + header + "synthetic body\n"),
            ExecutionMode.WORKBUDDY_MANIFEST,
        )
    assert caught.value.error_code == "RESPONSE_CONTRACT_INVALID"


def test_nonadjacent_and_crlf_download_header_injection_is_rejected() -> None:
    header = "successfully downloaded text file (SHA: " + "f" * 40 + ")\n"
    crlf_header = "successfully downloaded text file (SHA: " + "f" * 40 + ")\r\n"
    for payload in (
        header + "ordinary body\n" + header + "trailing body\n",
        crlf_header + "ordinary body\r\n" + crlf_header + "trailing body\r\n",
        header + "first body\n" + header + "second body\n" + header + "trailing body\n",
    ):
        with pytest.raises(ResponseContractError) as caught:
            STAGE2_PROFILE.parse_file(
                RawResponse(kind="text", text=payload),
                ExecutionMode.WORKBUDDY_MANIFEST,
            )
        assert caught.value.error_code == "RESPONSE_CONTRACT_INVALID"


def test_normal_similar_body_text_and_bad_headers_are_distinguished() -> None:
    header = "successfully downloaded text file (SHA: " + "a" * 40 + ")\n"
    body = (
        "prefix successfully downloaded text file (SHA: " + "f" * 40 + ")\n"
        "successfully downloaded text file (SHA: " + "f" * 40 + ") suffix\n"
        "successfully downloaded text file (SHA: not-a-full-sha)\nordinary text\n"
    )
    parsed = STAGE2_PROFILE.parse_file(
        RawResponse(kind="text", text=header + body),
        ExecutionMode.WORKBUDDY_MANIFEST,
    )
    assert parsed.content == body.encode("utf-8")
    for invalid in ("ordinary body without header", "prefix\n" + header, header.replace("a" * 40, "aaaa")):
        with pytest.raises(ResponseContractError):
            STAGE2_PROFILE.parse_file(
                RawResponse(kind="text", text=invalid),
                ExecutionMode.WORKBUDDY_MANIFEST,
            )


def test_profile_hash_changes_never_accept_another_profile() -> None:
    assert STAGE2_PROFILE.name == "stage2-readonly-v1"
    assert len(STAGE2_PROFILE.profile_hash()) == 64
    assert STAGE2_PROFILE.spec_for(Stage2Operation.GITHUB_READ_FILE).tool_id == GITHUB_GET_FILE_TOOL
    assert STAGE2_PROFILE.spec_for(Stage2Operation.LEXIANG_READ_ENTRIES).real_response_verified is False
