"""Tests for the Internal_CompanyNotFound annotation in proxy.py.

BC's own message for this code is on-premises advice ("edit the service
configuration file") and points the reader at the configured company name,
which measurement showed is not the cause. The proxy appends a note saying
so; these tests pin the behaviour that note depends on.
"""

from __future__ import annotations

import json

from mcp.types import CallToolResult, TextContent

from bc_mcp_proxy.config import ProxyConfig
from bc_mcp_proxy.proxy import _annotate_company_not_found

BC_ERROR = json.dumps({
    "Error": {
        "Code": "Internal_CompanyNotFound",
        "Message": (
            "Cannot process the request because the default company cannot be "
            "found. You can specify a default company in the service "
            "configuration file, or specify one for each tenant, or you can "
            "add a query string in the form of \"company=[name]\"."
        ),
    }
})


def _config() -> ProxyConfig:
  return ProxyConfig(
      environment="DemoEssential",
      company="Vangelder Solutions BV",
      configuration_name="MCP-VangelderSolutions",
  )


def _result(text: str, *, is_error: bool = False) -> CallToolResult:
  return CallToolResult(
      content=[TextContent(type="text", text=text)],
      isError=is_error,
  )


def test_unrelated_result_is_returned_unchanged() -> None:
  result = _result("Returned all 7 records.")
  assert _annotate_company_not_found(result, _config()) is result


def test_note_is_appended_and_flagged() -> None:
  annotated = _annotate_company_not_found(_result(BC_ERROR, is_error=True), _config())
  assert annotated.isError is True
  assert len(annotated.content) == 2
  # The original payload survives ahead of the note — callers parse it.
  assert annotated.content[0].text == BC_ERROR


def test_note_echoes_the_effective_config() -> None:
  note = _annotate_company_not_found(
      _result(BC_ERROR, is_error=True), _config()).content[1].text
  assert "DemoEssential" in note
  assert "Vangelder Solutions BV" in note
  assert "MCP-VangelderSolutions" in note


def test_note_contradicts_the_two_misleading_readings() -> None:
  note = _annotate_company_not_found(
      _result(BC_ERROR, is_error=True), _config()).content[1].text
  # ...that the header was not sent, and that a file needs editing.
  assert "sent on every request" in note
  assert "on-premises" in note


def test_unset_configuration_name_is_rendered_not_omitted() -> None:
  config = ProxyConfig(environment="DemoEssential", company="X",
                       configuration_name=None)
  note = _annotate_company_not_found(
      _result(BC_ERROR, is_error=True), config).content[1].text
  assert "<not set>" in note


def test_match_is_case_insensitive() -> None:
  result = _result('{"Error":{"Code":"internal_companynotfound"}}', is_error=True)
  assert _annotate_company_not_found(result, _config()) is not result


def test_annotation_is_idempotent_on_a_clean_result() -> None:
  # Guards against the note itself re-triggering the match on a second pass:
  # the note mentions the situation but must not carry the error code.
  annotated = _annotate_company_not_found(_result(BC_ERROR, is_error=True), _config())
  assert "Internal_CompanyNotFound" not in annotated.content[1].text
