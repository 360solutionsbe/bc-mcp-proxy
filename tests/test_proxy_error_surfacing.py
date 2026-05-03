"""Tests for masked-error detection in proxy.py (Fix #3)."""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from bc_mcp_proxy.proxy import _detect_masked_error, _flag_as_error


def _result(text: str, *, is_error: bool = False) -> CallToolResult:
  return CallToolResult(
      content=[TextContent(type="text", text=text)],
      isError=is_error,
  )


def test_clean_success_returns_none() -> None:
  assert _detect_masked_error(_result("Found 12 customers.")) is None


def test_explicit_error_is_left_alone() -> None:
  # Already flagged — no second-guessing.
  assert _detect_masked_error(_result("anything", is_error=True)) is None


def test_semantic_search_not_enabled_is_detected() -> None:
  text = "Semantic search is not enabled for this environment."
  detected = _detect_masked_error(_result(text))
  assert detected == text


def test_authentication_invalid_credentials_is_detected() -> None:
  text = (
      "The remote server returned an error: (401) Unauthorized."
      " Inner: Authentication_InvalidCredentials"
  )
  assert _detect_masked_error(_result(text)) == text


def test_match_is_case_insensitive() -> None:
  text = "FEATURE IS NOT ENABLED on this tenant"
  assert _detect_masked_error(_result(text)) == text


def test_internal_server_error_is_detected() -> None:
  assert _detect_masked_error(_result("Internal Server Error: try again")) is not None


def test_unrelated_text_is_not_flagged() -> None:
  # Must not over-trigger on benign words.
  assert _detect_masked_error(_result("All systems nominal.")) is None
  assert _detect_masked_error(_result("error log written")) is None


def test_multiple_content_items_only_one_with_pattern() -> None:
  result = CallToolResult(
      content=[
          TextContent(type="text", text="Step 1: ok"),
          TextContent(type="text", text="Authentication_InvalidCredentials at step 2"),
          TextContent(type="text", text="Step 3: skipped"),
      ],
      isError=False,
  )
  assert "Authentication_InvalidCredentials" in (_detect_masked_error(result) or "")


def test_empty_content_returns_none() -> None:
  assert _detect_masked_error(CallToolResult(content=[], isError=False)) is None


def test_flag_as_error_preserves_content() -> None:
  original = _result("Semantic search is not enabled.")
  flagged = _flag_as_error(original)
  assert flagged.isError is True
  assert flagged.content == original.content
  # Original must remain untouched (model_copy is non-mutating).
  assert original.isError is False
