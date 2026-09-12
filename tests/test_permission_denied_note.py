"""Tests for the permission-denial detection and note (permissions.py).

The payload shapes come from measurements against Business Central 28.0
(tests/fixtures/bc_permission_errors.json, docs/security-model.md): BC
answers a refused call with isError=true and a generic Internal_ServerError
whose message names the object and the missing permission in brackets.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from bc_mcp_proxy.permissions import (
    NOTE_MARKER,
    PermissionDenial,
    annotate_permission_denied,
    detect_permission_denied,
    parse_bc_error,
    permission_note,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bc_permission_errors.json"

TABLE_DENIAL = json.dumps({"error": {
    "code": "Internal_ServerError",
    "message": "Sorry, the current permissions prevented the action. "
               "(TableData 5200 Employee Read: _Exclude_APIV2_)"}}, indent=2)
PAGE_DENIAL = json.dumps({"error": {
    "code": "Internal_ServerError",
    "message": "Sorry, the current permissions prevented the action. "
               "(Page 30009 APIV2 - Customers Execute: _Exclude_APIV2_)"}})
INDIRECT_DENIAL = json.dumps({"error": {
    "code": "Internal_ServerError",
    "message": "Sorry, the current permissions prevented the action. "
               "(TableData 21 Cust. Ledger Entry IndirectRead: _Exclude_APIV2_)"}})
STRUCTURAL = json.dumps({"error": {
    "code": "BadRequest_NotFound", "message": "Bad Request - Error in query syntax."}})
LIST_RESULT = (
    'Returned all 2 records.\n'
    '{"@odata.count":2,"value":[{"code":"READ","displayName":"Read Permission Set"},'
    '{"code":"PCS","displayName":"Permission piece"}]}')


def _result(*texts: str, is_error: bool = False) -> CallToolResult:
  return CallToolResult(
      content=[TextContent(type="text", text=t) for t in texts], isError=is_error)


# -- parse_bc_error ------------------------------------------------------------


def test_parse_lower_and_upper_case_keys() -> None:
  assert parse_bc_error(TABLE_DENIAL)[0] == "Internal_ServerError"
  assert parse_bc_error('{"Error":{"Code":"X","Message":"m"}}') == ("X", "m")


def test_parse_ignores_record_fields_called_code() -> None:
  # A list payload prefixed by BC's sentence is not a JSON error object, and
  # even its JSON part has "code" only inside records.
  assert parse_bc_error(LIST_RESULT) is None
  assert parse_bc_error('{"value":[{"code":"READ"}]}') is None


def test_parse_regex_fallback_on_truncated_json() -> None:
  truncated = TABLE_DENIAL[:-4]
  assert parse_bc_error(truncated) == (
      "Internal_ServerError",
      "Sorry, the current permissions prevented the action. (TableData 5200 Employee Read: _Exclude_APIV2_)")


# -- detect_permission_denied --------------------------------------------------


def test_unrelated_results_are_not_denials() -> None:
  assert detect_permission_denied(_result(LIST_RESULT)) is None
  assert detect_permission_denied(_result(STRUCTURAL, is_error=True)) is None
  assert detect_permission_denied(CallToolResult(content=[], isError=True)) is None


def test_table_denial_is_parsed() -> None:
  denial = detect_permission_denied(_result(TABLE_DENIAL, is_error=True))
  assert denial == PermissionDenial(
      code="Internal_ServerError", message=json.loads(TABLE_DENIAL)["error"]["message"],
      kind="TableData", object_id=5200, object_name="Employee", permission="Read",
      app="_Exclude_APIV2_")
  assert denial.object_label == "TableData 5200 Employee"


def test_page_execute_denial_is_parsed() -> None:
  denial = detect_permission_denied(_result(PAGE_DENIAL, is_error=True))
  assert (denial.kind, denial.object_id, denial.permission) == ("Page", 30009, "Execute")
  assert denial.object_name == "APIV2 - Customers"


def test_indirect_read_is_kept_as_is() -> None:
  denial = detect_permission_denied(_result(INDIRECT_DENIAL, is_error=True))
  assert denial.permission == "IndirectRead"
  assert "indirect read permission" in permission_note(denial, "List_X_PAG1")


def test_detected_even_when_is_error_is_false() -> None:
  assert detect_permission_denied(_result(TABLE_DENIAL)) is not None


def test_denial_without_object_reference_still_detected() -> None:
  text = json.dumps({"error": {"code": "Internal_ServerError",
                                "message": "Sorry, the current permissions prevented the action."}})
  denial = detect_permission_denied(_result(text, is_error=True))
  assert denial is not None and denial.object_label is None


def test_authorization_code_counts_as_denial() -> None:
  text = json.dumps({"error": {"code": "Authorization_IdentityNotFound", "message": "No."}})
  assert detect_permission_denied(_result(text, is_error=True)) is not None


def test_plain_text_sentence_is_detected_but_keywords_are_not() -> None:
  plain = "You do not have the following permissions on TableData Employee: Read"
  assert detect_permission_denied(_result(plain, is_error=True)) is not None
  # Words alone never count: a page listing permission sets says "Read" and
  # "Permission" in every row.
  assert detect_permission_denied(_result("Read Permission: Yes; Execute Permission: Yes")) is None


# -- annotate_permission_denied ------------------------------------------------


def test_note_is_appended_and_flagged() -> None:
  annotated = annotate_permission_denied(_result(TABLE_DENIAL, is_error=True), "List_Employees_PAG30017")
  assert annotated.isError is True
  assert len(annotated.content) == 2
  assert annotated.content[0].text == TABLE_DENIAL  # original payload survives, first
  note = annotated.content[1].text
  assert note.strip().startswith(NOTE_MARKER)
  assert "read permission on TableData 5200 Employee" in note
  assert "List_Employees_PAG30017" in note
  assert "not by this proxy" in note


def test_unrelated_result_returned_as_same_object() -> None:
  result = _result(LIST_RESULT)
  assert annotate_permission_denied(result, "List_X_PAG1") is result


def test_already_error_result_gets_note() -> None:
  annotated = annotate_permission_denied(_result(PAGE_DENIAL, is_error=True), "List_Customers_PAG30009")
  assert annotated.isError is True and len(annotated.content) == 2


def test_note_without_object_when_unparseable() -> None:
  text = json.dumps({"error": {"code": "Internal_ServerError",
                                "message": "Sorry, the current permissions prevented the action."}})
  note = annotate_permission_denied(_result(text, is_error=True), "List_X_PAG1").content[1].text
  assert "lacks a permission it needs" in note


def test_annotation_is_idempotent() -> None:
  once = annotate_permission_denied(_result(TABLE_DENIAL, is_error=True), "List_Employees_PAG30017")
  twice = annotate_permission_denied(once, "List_Employees_PAG30017")
  assert twice is once
  # The note itself must not read as a denial on a later pass.
  assert detect_permission_denied(_result(once.content[1].text)) is None


def test_dynamic_invoke_note_names_the_action() -> None:
  note = annotate_permission_denied(
      _result(TABLE_DENIAL, is_error=True), "bc_actions_invoke",
      {"ActionName": "List_Employees_PAG30017", "RequestParameters": "{}"}).content[1].text
  assert "action List_Employees_PAG30017 (via bc_actions_invoke)" in note


# -- measured corpus -----------------------------------------------------------


def _fixture() -> dict:
  if not FIXTURE.is_file():
    pytest.skip("fixture not present")
  return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _corpus(kind: str) -> list:
  try:
    return _fixture()[kind]
  except Exception:  # noqa: BLE001 - skip cleanly when the fixture is absent
    return []


@pytest.mark.parametrize("record", _corpus("denials"), ids=lambda r: r["tool"][:40])
def test_every_measured_denial_is_detected(record: dict) -> None:
  result = CallToolResult(
      content=[TextContent(type="text", text=t) for t in record["text"]],
      isError=record.get("isError", False))
  denial = detect_permission_denied(result)
  assert denial is not None
  assert denial.kind in {"TableData", "Page"}
  assert denial.permission in {"Read", "IndirectRead", "Execute"}


@pytest.mark.parametrize("record", _corpus("nonDenials"), ids=lambda r: r["tool"][:40])
def test_measured_non_denials_are_left_alone(record: dict) -> None:
  result = CallToolResult(
      content=[TextContent(type="text", text=t) for t in record["text"]],
      isError=record.get("isError", False))
  assert detect_permission_denied(result) is None
  assert annotate_permission_denied(result, record["tool"]) is result
