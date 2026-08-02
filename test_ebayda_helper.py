from __future__ import annotations

import json
import io
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_type_hints
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import ebayda_helper


class LaunchUrlTests(unittest.TestCase):
    def test_valid_launch_url_is_parsed(self) -> None:
        request = ebayda_helper.parse_launch_url(
            "ebayda://run?job_id=job_abc-123&ticket=abcdefghijklmnop"
        )

        self.assertEqual(request.job_id, "job_abc-123")
        self.assertEqual(request.ticket, "abcdefghijklmnop")

    def test_invalid_protocol_shape_is_rejected(self) -> None:
        invalid_urls = (
            "https://run?job_id=job_1&ticket=abcdefghijklmnop",
            "ebayda://bind?job_id=job_1&ticket=abcdefghijklmnop",
            "ebayda://run/path?job_id=job_1&ticket=abcdefghijklmnop",
            "ebayda://run?job_id=job_1&ticket=abcdefghijklmnop#fragment",
        )

        for value in invalid_urls:
            with self.subTest(value=value), self.assertRaises(ebayda_helper.HelperError):
                ebayda_helper.parse_launch_url(value)

    def test_malformed_url_is_rejected_with_helper_error(self) -> None:
        with self.assertRaises(ebayda_helper.HelperError):
            ebayda_helper.parse_launch_url(
                "ebayda://run／evil?job_id=job_1&ticket=abcdefghijklmnop"
            )

    def test_missing_duplicate_unknown_or_unsafe_parameters_are_rejected(self) -> None:
        invalid_urls = (
            "ebayda://run?job_id=job_1",
            "ebayda://run?ticket=abcdefghijklmnop",
            "ebayda://run?job_id=job_1&job_id=job_2&ticket=abcdefghijklmnop",
            "ebayda://run?job_id=job_1&ticket=abcdefghijklmnop&extra=1",
            "ebayda://run?job_id=../job&ticket=abcdefghijklmnop",
            "ebayda://run?job_id=job_1&ticket=short",
        )

        for value in invalid_urls:
            with self.subTest(value=value), self.assertRaises(ebayda_helper.HelperError):
                ebayda_helper.parse_launch_url(value)


class ClaimJobTests(unittest.TestCase):
    class Response:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self.body = body
            self.status = status
            self.read_limit: int | None = None
            self.closed = False

        def __enter__(self) -> ClaimJobTests.Response:
            return self

        def __exit__(self, *args: object) -> None:
            self.closed = True

        def read(self, limit: int) -> bytes:
            self.read_limit = limit
            return self.body[:limit]

    def setUp(self) -> None:
        self.request = ebayda_helper.LaunchRequest(
            job_id="job_abc-123", ticket="super-secret-ticket"
        )

    def complete_payload(self, **updates: object) -> dict[str, object]:
        job_id = str(updates.get("job_id") or self.request.job_id)
        payload: dict[str, object] = {
            "job_id": job_id,
            "action": "save_draft",
            "shop_id": "shop_123",
            "job_token": "abcdefghijklmnop",
            "product_json_url": (
                f"https://www.ebayda.com/api/automation/jobs/{job_id}/product-json"
            ),
            "images_zip_url": (
                f"https://www.ebayda.com/api/automation/jobs/{job_id}/images"
            ),
        }
        payload.update(updates)
        return payload

    def claim_payload(self, payload: object) -> object:
        response = self.Response(json.dumps(payload).encode("utf-8"))
        return ebayda_helper.claim_job(self.request, lambda *args, **kwargs: response)

    def test_posts_claim_request_and_returns_valid_payload(self) -> None:
        request = self.request
        payload = self.complete_payload()
        response = self.Response(json.dumps(payload).encode("utf-8"))
        call: dict[str, object] = {}

        def open_url(http_request: object, timeout: int) -> ClaimJobTests.Response:
            call["request"] = http_request
            call["timeout"] = timeout
            return response

        result = ebayda_helper.claim_job(request, open_url)

        http_request = call["request"]
        headers = {
            name.casefold(): value for name, value in http_request.header_items()
        }
        self.assertEqual(
            http_request.full_url,
            "https://www.ebayda.com/api/automation/jobs/job_abc-123/claim",
        )
        self.assertEqual(http_request.get_method(), "POST")
        self.assertEqual(http_request.data, b"")
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(
            headers["authorization"], "LaunchTicket super-secret-ticket"
        )
        self.assertEqual(headers["user-agent"], "EbaydaHelper/0.1")
        self.assertEqual(ebayda_helper.CLAIM_TIMEOUT_SECONDS, 10)
        self.assertEqual(call["timeout"], 10)
        self.assertEqual(ebayda_helper.MAX_CLAIM_RESPONSE_BYTES, 1024 * 1024)
        self.assertEqual(response.read_limit, 1024 * 1024 + 1)
        self.assertTrue(response.closed)
        self.assertEqual(result, payload)

    def test_rejects_non_200_response_before_reading(self) -> None:
        payload = self.complete_payload()
        response = self.Response(json.dumps(payload).encode("utf-8"), status=503)

        with self.assertRaisesRegex(
            ebayda_helper.HelperError, "^领取任务失败：HTTP 503$"
        ):
            ebayda_helper.claim_job(
                self.request, lambda *args, **kwargs: response
            )

        self.assertIsNone(response.read_limit)
        self.assertTrue(response.closed)

    def test_return_annotation_is_mapping(self) -> None:
        self.assertEqual(
            get_type_hints(ebayda_helper.claim_job)["return"], Mapping[str, Any]
        )

    def test_accepts_mapping_payload(self) -> None:
        payload = MappingProxyType(self.complete_payload())
        response = self.Response(b"{}")

        with patch.object(ebayda_helper.json, "loads", return_value=payload):
            result = ebayda_helper.claim_job(
                self.request, lambda *args, **kwargs: response
            )

        self.assertIs(result, payload)

    def test_rejects_mismatched_job_action_and_non_object_payloads(self) -> None:
        invalid_payloads = (
            self.complete_payload(job_id="another_job"),
            self.complete_payload(action="submit"),
            [self.request.job_id, "save_draft", "shop_123"],
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(
                ebayda_helper.HelperError
            ):
                self.claim_payload(payload)

    def test_rejects_invalid_shop_id(self) -> None:
        with self.assertRaises(ebayda_helper.HelperError):
            self.claim_payload(self.complete_payload(shop_id="../shop"))

    def test_accepts_numeric_shop_id_without_changing_payload(self) -> None:
        result = self.claim_payload(self.complete_payload(shop_id=101))

        self.assertEqual(result["shop_id"], 101)

    def test_rejects_incomplete_execution_payload(self) -> None:
        payload = self.complete_payload()
        del payload["job_token"]

        with self.assertRaises(ebayda_helper.HelperError):
            self.claim_payload(payload)

    def test_rejects_oversized_or_malformed_responses(self) -> None:
        invalid_bodies = (
            b"x" * (ebayda_helper.MAX_CLAIM_RESPONSE_BYTES + 1),
            b"\xff",
            b"{",
        )

        for body in invalid_bodies:
            with self.subTest(size=len(body)), self.assertRaises(
                ebayda_helper.HelperError
            ):
                ebayda_helper.claim_job(
                    self.request, lambda *args, body=body, **kwargs: self.Response(body)
                )

    def test_transport_errors_are_safe_and_do_not_expose_ticket(self) -> None:
        ticket = self.request.ticket
        errors = (
            (
                HTTPError(
                    f"https://www.ebayda.com/?ticket={ticket}",
                    403,
                    ticket,
                    None,
                    None,
                ),
                "领取任务失败：HTTP 403",
            ),
            (URLError(ticket), "领取任务失败：网络错误"),
            (URLError(TimeoutError(ticket)), "领取任务失败：请求超时"),
            (TimeoutError(ticket), "领取任务失败：请求超时"),
            (OSError(ticket), "领取任务失败：网络错误"),
        )

        for error, expected_message in errors:
            def open_url(*args: object, error: BaseException = error, **kwargs: object) -> None:
                raise error

            with self.subTest(error=type(error).__name__), self.assertRaisesRegex(
                ebayda_helper.HelperError, f"^{expected_message}$"
            ) as caught:
                ebayda_helper.claim_job(self.request, open_url)
            self.assertNotIn(ticket, str(caught.exception))


class CommandLineTests(unittest.TestCase):
    def test_success_prints_only_safe_claim_summary(self) -> None:
        output = io.StringIO()
        with patch.object(
            ebayda_helper,
            "claim_job",
            return_value={"job_id": "job_1", "shop_id": "101", "action": "save_draft"},
        ), redirect_stdout(output):
            exit_code = ebayda_helper.main(
                ["ebayda://run?job_id=job_1&ticket=abcdefghijklmnop"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"status": "claimed", "job_id": "job_1", "shop_id": "101"},
        )
        self.assertNotIn("abcdefghijklmnop", output.getvalue())

    def test_failure_is_safe_and_does_not_echo_launch_url(self) -> None:
        error_output = io.StringIO()
        with patch.object(
            ebayda_helper,
            "claim_job",
            side_effect=ebayda_helper.HelperError("领取任务失败"),
        ), redirect_stderr(error_output):
            exit_code = ebayda_helper.main(
                ["ebayda://run?job_id=job_1&ticket=abcdefghijklmnop"]
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            json.loads(error_output.getvalue()),
            {"status": "failed", "error": "领取任务失败"},
        )
        self.assertNotIn("abcdefghijklmnop", error_output.getvalue())


class InstallerContractTests(unittest.TestCase):
    def test_inno_setup_registers_current_user_protocol(self) -> None:
        script = (
            Path(__file__).with_name("installer") / "EbaydaHelper.iss"
        ).read_text(encoding="utf-8")

        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("Software\\Classes\\ebayda", script)
        self.assertIn('ValueName: "URL Protocol"', script)
        self.assertIn('ValueData: """{app}\\{#MyAppExeName}"" ""%1"""', script)
        self.assertIn("Flags: uninsdeletekey", script)
