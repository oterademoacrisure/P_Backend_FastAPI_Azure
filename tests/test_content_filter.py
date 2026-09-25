"""
Unit tests for Azure OpenAI's own content-filter rejection path -- a second,
independent safety layer from Prompt Shields (content_safety_service.py).
Covers is_content_filter_error()'s detection logic and app/graph.py's
routing/node behavior when it fires. See CONTENT_SAFETY.md section 1 for
where this sits relative to Prompt Shields and Groundedness Detection.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from openai import BadRequestError

from app.graph import content_blocked_node, route_after_generate
from app.services.openai_service import is_content_filter_error


def _bad_request_error(body) -> BadRequestError:
    response = MagicMock(spec=httpx.Response)
    response.request = MagicMock()
    response.headers = {}
    response.status_code = 400
    return BadRequestError("Error code: 400", response=response, body=body)


class TestIsContentFilterError:
    def test_true_for_azures_nested_error_shape(self):
        # The actual shape Azure OpenAI returns -- "code" one level down
        # inside "error", not at the top level of the body.
        body = {
            "error": {
                "message": "The response was filtered due to the prompt "
                "triggering Azure OpenAI's content management policy.",
                "type": None,
                "param": "prompt",
                "code": "content_filter",
                "status": 400,
                "innererror": {
                    "code": "ResponsibleAIPolicyViolation",
                    "content_filter_result": {
                        "jailbreak": {"detected": True, "filtered": True},
                    },
                },
            }
        }
        assert is_content_filter_error(_bad_request_error(body)) is True

    def test_true_for_a_flatter_shape_too(self):
        assert is_content_filter_error(_bad_request_error({"code": "content_filter"})) is True

    def test_false_for_an_unrelated_bad_request(self):
        body = {"error": {"code": "InvalidRequestBody", "message": "bad input"}}
        assert is_content_filter_error(_bad_request_error(body)) is False

    def test_false_for_a_non_bad_request_exception(self):
        assert is_content_filter_error(ValueError("content_filter")) is False

    def test_false_for_none_body(self):
        assert is_content_filter_error(_bad_request_error(None)) is False


class TestRouteAfterGenerate:
    def test_routes_to_content_blocked_when_flagged(self):
        assert route_after_generate({"content_filtered": True}) == "content_blocked"

    def test_routes_to_groundedness_check_otherwise(self):
        assert route_after_generate({"content_filtered": False}) == "groundedness_check"

    def test_defaults_to_groundedness_check_when_field_absent(self):
        assert route_after_generate({}) == "groundedness_check"


class TestContentBlockedNode:
    def test_sets_rejected_status_with_a_clear_message(self):
        result = content_blocked_node({})
        assert result["status"] == "rejected"
        assert "content safety filter" in result["feedback"]
