from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from inspect import signature
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_type_hints
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import ebayda_helper
import helper_runtime


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
            "ebayda://run?job_id=item_1&ticket=abcdefghijklmnop",
            "ebayda://run?job_id=../job&ticket=abcdefghijklmnop",
            "ebayda://run?job_id=job_1&ticket=short",
        )

        for value in invalid_urls:
            with self.subTest(value=value), self.assertRaises(ebayda_helper.HelperError):
                ebayda_helper.parse_launch_url(value)


class BindingUrlTests(unittest.TestCase):
    def test_bind_and_unbind_urls_are_parsed(self) -> None:
        self.assertEqual(
            ebayda_helper.parse_binding_url(
                "ebayda://shop-binding?action=bind&ticket=abcdefghijklmnop"
            ),
            ebayda_helper.BindingRequest(action="bind", ticket="abcdefghijklmnop"),
        )
        self.assertEqual(
            ebayda_helper.parse_binding_url(
                "ebayda://shop-binding?action=unbind&ticket=abcdefghijklmnop"
            ),
            ebayda_helper.BindingRequest(action="unbind", ticket="abcdefghijklmnop"),
        )

    def test_binding_urls_reject_extra_or_unsafe_parameters(self) -> None:
        invalid_urls = (
            "ebayda://bind?shop_id=101",
            "ebayda://shop-binding?action=bind",
            "ebayda://shop-binding?action=bind&ticket=short",
            "ebayda://shop-binding?action=invalid&ticket=abcdefghijklmnop",
            "ebayda://shop-binding?action=bind&ticket=abcdefghijklmnop&extra=1",
            "ebayda://shop-binding?action=bind&ticket=abcdefghijklmnop&ticket=other",
            "ebayda://run?shop_id=101",
        )
        for value in invalid_urls:
            with self.subTest(value=value), self.assertRaises(
                ebayda_helper.HelperError
            ):
                ebayda_helper.parse_binding_url(value)


class BindingStateTests(unittest.TestCase):
    def test_bind_opens_profile_and_unbind_keeps_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            opened: list[Path] = []

            ebayda_helper.bind_shop(
                "101",
                app_root=root,
                ensure=lambda profile: opened.append(profile),
            )
            profile = root / "profiles" / "101"
            self.assertEqual(opened, [profile])
            self.assertTrue(ebayda_helper.is_shop_bound(root, "101"))

            ebayda_helper.unbind_shop("101", app_root=root)

            self.assertFalse(ebayda_helper.is_shop_bound(root, "101"))
            self.assertFalse(profile.exists())

    def test_bind_persists_the_device_token_in_local_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = "device-token-abcdefghijklmnop"

            ebayda_helper.bind_shop(
                "101",
                app_root=root,
                ensure=lambda _profile: None,
                device_token=token,
            )

            self.assertEqual(ebayda_helper._read_bindings(root), {"101": token})
            payload = json.loads((root / "bindings.json").read_text(encoding="utf-8"))
            self.assertEqual(payload, {"shops": {"101": {"device_token": token}}})


class BindingClaimTests(unittest.TestCase):
    class Response:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self.body = body
            self.status = status
            self.read_limit: int | None = None
            self.closed = False

        def __enter__(self) -> "BindingClaimTests.Response":
            return self

        def __exit__(self, *args: object) -> None:
            self.closed = True

        def read(self, limit: int) -> bytes:
            self.read_limit = limit
            return self.body[:limit]

    def setUp(self) -> None:
        self.request = ebayda_helper.BindingRequest(
            action="bind", ticket="abcdefghijklmnop"
        )

    def test_posts_binding_ticket_and_validates_flat_payload(self) -> None:
        payload = {
            "ticket_id": "binding_1",
            "user_id": 42,
            "shop_id": 101,
            "device_id": "device-mac-1",
            "action": "bind",
            "status": "bound",
            "claimed_at": "2026-08-04T01:00:00Z",
        }
        device_token = "device-token-abcdefghijklmnop"
        response = self.Response(json.dumps(payload).encode("utf-8"))
        call: dict[str, object] = {}

        def open_url(http_request: object, timeout: int) -> BindingClaimTests.Response:
            call["request"] = http_request
            call["timeout"] = timeout
            return response

        result = ebayda_helper.claim_binding(self.request, device_token=device_token, open_url=open_url)
        http_request = call["request"]
        headers = {
            name.casefold(): value for name, value in http_request.header_items()
        }
        self.assertEqual(
            http_request.full_url,
			"https://www.ebayda.com/api/automation/shop-bindings/claim",
        )
        self.assertEqual(http_request.get_method(), "POST")
        self.assertEqual(json.loads(http_request.data), {"device_token": device_token})
        self.assertEqual(headers["authorization"], "BindingTicket abcdefghijklmnop")
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(result, payload)
        self.assertNotIn("device_token", result)
        self.assertEqual(call["timeout"], ebayda_helper.BINDING_TIMEOUT_SECONDS)
        self.assertTrue(response.closed)

    def test_rejects_action_status_and_shop_mismatches(self) -> None:
        payloads = (
            {"action": "unbind", "shop_id": 101, "device_id": "d", "status": "bound"},
            {"action": "bind", "shop_id": "../101", "device_id": "d", "status": "bound"},
            {"action": "bind", "shop_id": 101, "device_id": "d", "status": "unbound"},
        )
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(
                ebayda_helper.HelperError
            ):
                response = self.Response(json.dumps(payload).encode("utf-8"))
                ebayda_helper.claim_binding(
                    self.request,
                    device_token="device-token-abcdefghijklmnop",
                    open_url=lambda *args, response=response, **kwargs: response,
                )

    def test_accepts_a_legacy_server_token_during_rollout(self) -> None:
        payload = {
            "shop_id": 101,
            "device_id": "device-mac-1",
            "action": "bind",
            "status": "bound",
            "device_token": "legacy-device-token-abcdefghijklmnop",
        }
        response = self.Response(json.dumps(payload).encode("utf-8"))

        result = ebayda_helper.claim_binding(
            self.request,
            device_token="device-token-abcdefghijklmnop",
            open_url=lambda *_args, **_kwargs: response,
        )

        self.assertEqual(result["device_token"], "legacy-device-token-abcdefghijklmnop")


class TaskExecutionCleanupTests(unittest.TestCase):
    def test_execute_claimed_job_cleans_files_after_success(self) -> None:
        payload = {
            "job_id": "job_1",
            "shop_id": "101",
            "action": "save_draft",
            "job_token": "abcdefghijklmnop",
            "product_json_url": "https://www.ebayda.com/api/automation/jobs/job_1/product-json",
            "images_zip_url": "https://www.ebayda.com/api/automation/jobs/job_1/images",
            "size_chart_url": "https://www.ebayda.com/api/automation/jobs/job_1/size-chart",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def prepare(_job: object, app_root: Path) -> helper_runtime.TaskFiles:
                task_dir = app_root / "jobs" / "job_1"
                task_dir.mkdir(parents=True)
                (task_dir / "product.json").write_text("{}", encoding="utf-8")
                return helper_runtime.TaskFiles(
                    task_dir / "product.json",
                    task_dir / "images.zip",
                    task_dir / "work",
                )

            with patch.object(ebayda_helper, "application_root", return_value=root), patch.object(
                ebayda_helper, "prepare_job_files", side_effect=prepare
            ), patch.object(ebayda_helper, "shop_profile", return_value=root / "profiles" / "101"), patch.object(
                ebayda_helper, "ensure_chrome", return_value=9222
            ), patch.object(ebayda_helper, "run_automation", return_value=0), patch.object(
                ebayda_helper, "post_event"
            ):
                result = ebayda_helper.execute_claimed_job(payload)

            self.assertEqual(result[0], "draft_saved")
            self.assertFalse((root / "jobs" / "job_1").exists())

    def test_legacy_launch_cleans_only_its_own_task_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            own_cache = root / "jobs" / "job_1"
            active_batch_cache = root / "jobs" / "item_1"
            own_cache.mkdir(parents=True)
            active_batch_cache.mkdir()
            launch = ebayda_helper.LaunchRequest("job_1", "abcdefghijklmnop")

            with patch.object(ebayda_helper, "application_root", return_value=root), patch.object(
                ebayda_helper, "claim_job", return_value={}
            ), patch.object(
                ebayda_helper, "execute_claimed_job", return_value=("draft_saved", "job_1", "101")
            ), redirect_stdout(io.StringIO()):
                exit_code = ebayda_helper._run_launch(launch)

            self.assertEqual(exit_code, 0)
            self.assertFalse(own_cache.exists())
            self.assertTrue(active_batch_cache.exists())


class ApiOriginTests(unittest.TestCase):
    def test_api_origin_defaults_to_https_public_origin(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                ebayda_helper._configured_api_origin(),
                "https://www.ebayda.com",
            )

    def test_local_api_origin_requires_explicit_test_flag(self) -> None:
        with patch.dict(
            os.environ,
            {"EBAYDA_API_ORIGIN": "http://127.0.0.1:18080"},
            clear=True,
        ), self.assertRaisesRegex(
            ebayda_helper.HelperError,
            "^本地 API 地址必须显式启用测试开关$",
        ):
            ebayda_helper._configured_api_origin()

    def test_plain_http_remote_origin_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {
                "EBAYDA_API_ORIGIN": "http://101.34.90.101:10112",
                "EBAYDA_ALLOW_REMOTE_API": "1",
            },
            clear=True,
        ), self.assertRaises(ebayda_helper.HelperError):
            ebayda_helper._configured_api_origin()

    def test_local_api_origin_rewrites_claimed_resource_paths_only(self) -> None:
        payload = {
            "job_id": "job_1",
            "shop_id": "101",
            "action": "save_draft",
            "job_token": "abcdefghijklmnop",
            "product_json_url": (
                "https://www.ebayda.com/api/automation/jobs/job_1/product-json"
            ),
            "images_zip_url": (
                "https://www.ebayda.com/api/automation/jobs/job_1/images"
            ),
            "size_chart_url": (
                "https://www.ebayda.com/api/automation/jobs/job_1/size-chart"
            ),
        }
        job = helper_runtime.ClaimedJob.from_payload(payload)

        with patch.dict(
            os.environ,
            {
                "EBAYDA_API_ORIGIN": "http://127.0.0.1:18080",
                "EBAYDA_ALLOW_LOCAL_API": "1",
            },
            clear=True,
        ):
            localized = ebayda_helper._localize_claimed_job(
                job,
                ebayda_helper._configured_api_origin(),
            )

        self.assertEqual(
            localized.product_json_url,
            "http://127.0.0.1:18080/api/automation/jobs/job_1/product-json",
        )
        self.assertEqual(
            localized.images_zip_url,
            "http://127.0.0.1:18080/api/automation/jobs/job_1/images",
        )
        self.assertEqual(
            localized.size_chart_url,
            "http://127.0.0.1:18080/api/automation/jobs/job_1/size-chart",
        )
        self.assertEqual(localized.job_token, job.job_token)

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
            "size_chart_url": (
                f"https://www.ebayda.com/api/automation/jobs/{job_id}/size-chart"
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

    def test_api_clients_default_to_no_redirect_opener(self) -> None:
        self.assertIs(
            signature(ebayda_helper.claim_job).parameters["open_url"].default,
            helper_runtime.open_no_redirect,
        )
        self.assertIs(
            signature(ebayda_helper.post_event).parameters["open_url"].default,
            helper_runtime.open_no_redirect,
        )


class DevicePollingTests(unittest.TestCase):
    class Response:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self.body = body
            self.status = status

        def __enter__(self) -> "DevicePollingTests.Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return self.body[:limit]

    @staticmethod
    def item_payload(item_id: str = "item_1", shop_id: str = "101") -> dict[str, str]:
        return {
            "job_id": item_id,
            "shop_id": shop_id,
            "action": "save_draft",
            "job_token": "abcdefghijklmnop",
            "product_json_url": (
                f"https://www.ebayda.com/api/automation/batch-items/{item_id}/product-json"
            ),
            "images_zip_url": (
                f"https://www.ebayda.com/api/automation/batch-items/{item_id}/images"
            ),
            "size_chart_url": (
                f"https://www.ebayda.com/api/automation/batch-items/{item_id}/size-chart"
            ),
        }

    def test_claim_next_posts_device_credential_and_validates_item(self) -> None:
        response = self.Response(
            json.dumps(
                {
                    "commands": [{"type": "terminate", "item_id": "item_old"}],
                    "item": self.item_payload(),
                }
            ).encode("utf-8")
        )
        call: dict[str, object] = {}

        def open_url(http_request: object, timeout: int) -> DevicePollingTests.Response:
            call["request"] = http_request
            call["timeout"] = timeout
            return response

        result = ebayda_helper.claim_next("device-token-abcdefghijklmnop", open_url=open_url)

        request = call["request"]
        headers = {name.casefold(): value for name, value in request.header_items()}
        self.assertEqual(request.full_url, "https://www.ebayda.com/api/automation/devices/next")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(headers["authorization"], "DeviceToken device-token-abcdefghijklmnop")
        self.assertEqual(call["timeout"], ebayda_helper.DEVICE_TIMEOUT_SECONDS)
        self.assertEqual(result.termination_item_ids, ("item_old",))
        self.assertEqual(result.item_payload, self.item_payload())

    def test_acknowledge_termination_uses_the_bound_device_credential(self) -> None:
        response = self.Response(b'{"status":"ok"}')
        call: dict[str, object] = {}

        def open_url(http_request: object, timeout: int) -> DevicePollingTests.Response:
            call["request"] = http_request
            call["timeout"] = timeout
            return response

        ebayda_helper.acknowledge_termination(
            "item_1", "device-token-abcdefghijklmnop", open_url=open_url
        )

        request = call["request"]
        headers = {name.casefold(): value for name, value in request.header_items()}
        self.assertEqual(
            request.full_url,
            "https://www.ebayda.com/api/automation/devices/termination-ack",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(headers["authorization"], "DeviceToken device-token-abcdefghijklmnop")
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(request.data, b'{"item_id":"item_1"}')
        self.assertEqual(call["timeout"], ebayda_helper.DEVICE_TIMEOUT_SECONDS)

    def test_report_final_event_uses_device_credential_without_job_token(self) -> None:
        response = self.Response(b'{"status":"ok"}')
        call: dict[str, object] = {}

        def open_url(http_request: object, timeout: int) -> DevicePollingTests.Response:
            call["request"] = http_request
            call["timeout"] = timeout
            return response

        reported = ebayda_helper.report_final_event(
            "item_1",
            "device-token-abcdefghijklmnop",
            "draft_saved",
            message="草稿已保存",
            open_url=open_url,
        )

        request = call["request"]
        headers = {name.casefold(): value for name, value in request.header_items()}
        self.assertTrue(reported)
        self.assertEqual(
            request.full_url,
            "https://www.ebayda.com/api/automation/devices/batch-items/item_1/final-event",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(headers["authorization"], "DeviceToken device-token-abcdefghijklmnop")
        self.assertNotIn("jobtoken", headers["authorization"].casefold())
        self.assertEqual(json.loads(request.data), {"status": "draft_saved", "message": "草稿已保存"})
        self.assertEqual(call["timeout"], ebayda_helper.DEVICE_TIMEOUT_SECONDS)


class ResidentRunnerTests(unittest.TestCase):
    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminated = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: int | None = None) -> int:
            if self.returncode is None:
                raise AssertionError(f"unexpected wait({timeout})")
            return self.returncode

    @staticmethod
    def payload(item_id: str, shop_id: str) -> dict[str, str]:
        return DevicePollingTests.item_payload(item_id, shop_id)

    def test_uses_another_bound_token_when_the_first_one_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ebayda_helper._write_bindings(
                root,
                {"101": "aaaaaaaaaaaaaaaa", "102": "bbbbbbbbbbbbbbbb"},
            )
            calls: list[str] = []

            def claim(token: str) -> ebayda_helper.DevicePoll:
                calls.append(token)
                if token.startswith("a"):
                    raise ebayda_helper.HelperError("expired")
                return ebayda_helper.DevicePoll((), None)

            ebayda_helper.ResidentRunner(root, claim_next_request=claim).run_once()

            self.assertEqual(calls, ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"])

    def test_different_shops_run_in_parallel_but_a_shop_stays_serial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processes: list[ResidentRunnerTests.Process] = []
            runner = ebayda_helper.ResidentRunner(
                root,
                spawn=lambda _payload: processes.append(self.Process()) or processes[-1],
            )

            runner._start_item(self.payload("item_1", "101"))
            runner._start_item(self.payload("item_2", "101"))
            runner._start_item(self.payload("item_3", "102"))

            self.assertEqual(set(runner._active), {"item_1", "item_3"})
            self.assertEqual(len(processes), 2)

    def test_start_failure_reports_failed_instead_of_leaving_the_item_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events: list[tuple[str, str, str | None, Path | None]] = []

            def report(job: helper_runtime.ClaimedJob, status: str, message: str | None = None, *, app_root: Path | None = None) -> None:
                events.append((job.job_id, status, message, app_root))

            def fail_to_spawn(_payload: Mapping[str, Any]) -> None:
                raise ebayda_helper.HelperError("spawn failed")

            runner = ebayda_helper.ResidentRunner(root, spawn=fail_to_spawn)
            with patch.object(ebayda_helper, "_post_final_event", side_effect=report):
                runner._start_item(self.payload("item_1", "101"))

            self.assertEqual(
                events,
                [("item_1", "failed", "无法启动批次自动化执行进程", root)],
            )
            self.assertEqual(runner._active, {})

    def test_marker_failure_reports_unknown_only_after_the_child_is_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = self.Process()
            events: list[tuple[str, str, str | None, Path | None]] = []

            def report(job: helper_runtime.ClaimedJob, status: str, message: str | None = None, *, app_root: Path | None = None) -> None:
                events.append((job.job_id, status, message, app_root))

            runner = ebayda_helper.ResidentRunner(root, spawn=lambda _payload: process)
            with patch.object(ebayda_helper, "_write_execution_marker", side_effect=ebayda_helper.HelperError("disk full")), patch.object(
                ebayda_helper, "_post_final_event", side_effect=report
            ):
                runner._start_item(self.payload("item_1", "101"))

            self.assertTrue(process.terminated)
            self.assertEqual(
                events,
                [("item_1", "terminated_unknown", "无法保存执行进程状态，结果未知", root)],
            )
            self.assertEqual(runner._active, {})

    def test_termination_ends_only_the_child_and_reports_unknown_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = self.Process()
            runner = ebayda_helper.ResidentRunner(root, spawn=lambda _payload: process)
            runner._start_item(self.payload("item_1", "101"))
            acknowledged: list[tuple[str, str]] = []
            runner._acknowledge_termination_request = lambda item_id, token: acknowledged.append((item_id, token))
            runner._terminate_execution(
                "item_1", runner._active["item_1"], "device-token-abcdefghijklmnop"
            )

            self.assertTrue(process.terminated)
            self.assertNotIn("item_1", runner._active)
            self.assertEqual(acknowledged, [("item_1", "device-token-abcdefghijklmnop")])

    def test_restart_acknowledges_a_delivered_termination_command_without_a_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ebayda_helper._write_bindings(root, {"101": "aaaaaaaaaaaaaaaa"})
            acknowledged: list[tuple[str, str]] = []
            runner = ebayda_helper.ResidentRunner(
                root,
                claim_next_request=lambda _token: ebayda_helper.DevicePoll(("item_1",), None),
                acknowledge_termination_request=lambda item_id, token: acknowledged.append((item_id, token)),
            )

            runner.run_once()

            self.assertEqual(acknowledged, [("item_1", "aaaaaaaaaaaaaaaa")])

    def test_restart_keeps_termination_pending_when_the_recorded_child_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ebayda_helper._write_bindings(root, {"101": "aaaaaaaaaaaaaaaa"})
            marker = root / "active-executions" / "item_1.json"
            marker.parent.mkdir(parents=True)
            marker.write_text('{"pid":12345}', encoding="utf-8")
            acknowledged: list[tuple[str, str]] = []
            runner = ebayda_helper.ResidentRunner(
                root,
                claim_next_request=lambda _token: ebayda_helper.DevicePoll(("item_1",), None),
                acknowledge_termination_request=lambda item_id, token: acknowledged.append((item_id, token)),
            )

            with patch.object(ebayda_helper, "_process_is_alive", return_value=True):
                runner.run_once()

            self.assertEqual(acknowledged, [])
            self.assertTrue(marker.exists())

    def test_restart_reconciles_a_dead_child_before_claiming_the_next_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ebayda_helper._write_bindings(root, {"101": "aaaaaaaaaaaaaaaa"})
            marker = root / "active-executions" / "item_1.json"
            marker.parent.mkdir(parents=True)
            marker.write_text('{"pid":12345}', encoding="utf-8")
            sequence: list[str] = []
            reports: list[tuple[str, str, str, str | None]] = []
            runner = ebayda_helper.ResidentRunner(
                root,
                claim_next_request=lambda _token: sequence.append("claim") or ebayda_helper.DevicePoll(
                    (), self.payload("item_2", "101")
                ),
                report_final_event_request=lambda item_id, token, status, *, message=None: sequence.append("report") or reports.append(
                    (item_id, token, status, message)
                ) or True,
                spawn=lambda _payload: self.Process(),
            )

            with patch.object(ebayda_helper, "_process_is_alive", return_value=False):
                runner.run_once()

            self.assertEqual(sequence, ["report", "claim"])
            self.assertEqual(
                reports,
                [("item_1", "aaaaaaaaaaaaaaaa", "terminated_unknown", "助手重启后发现自动化进程已结束，结果未知")],
            )
            self.assertFalse(marker.exists())
            self.assertIn("item_2", runner._active)

    def test_restart_keeps_a_dead_child_marker_when_the_unknown_result_cannot_be_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ebayda_helper._write_bindings(root, {"101": "aaaaaaaaaaaaaaaa"})
            marker = root / "active-executions" / "item_1.json"
            marker.parent.mkdir(parents=True)
            marker.write_text('{"pid":12345}', encoding="utf-8")
            runner = ebayda_helper.ResidentRunner(
                root,
                claim_next_request=lambda _token: ebayda_helper.DevicePoll((), None),
                report_final_event_request=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    ebayda_helper.HelperError("network")
                ),
            )

            with patch.object(ebayda_helper, "_process_is_alive", return_value=False):
                runner.run_once()

            self.assertTrue(marker.exists())

    def test_windows_reboot_reconciles_a_marker_even_when_the_pid_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ebayda_helper._write_bindings(root, {"101": "aaaaaaaaaaaaaaaa"})
            marker = root / "active-executions" / "item_1.json"
            marker.parent.mkdir(parents=True)
            marker.write_text('{"pid":12345,"started_uptime_ms":1000}', encoding="utf-8")
            reports: list[tuple[str, str, str, str | None]] = []
            runner = ebayda_helper.ResidentRunner(
                root,
                claim_next_request=lambda _token: ebayda_helper.DevicePoll((), None),
                report_final_event_request=lambda item_id, token, status, *, message=None: reports.append(
                    (item_id, token, status, message)
                ) or True,
            )

            with patch.object(ebayda_helper.os, "name", "nt"), patch.object(
                ebayda_helper, "_windows_uptime_ms", return_value=100
            ), patch.object(ebayda_helper, "_process_is_alive", return_value=True) as alive:
                runner.run_once()

            alive.assert_not_called()
            self.assertEqual(reports[0][:3], ("item_1", "aaaaaaaaaaaaaaaa", "terminated_unknown"))
            self.assertFalse(marker.exists())

    def test_windows_process_check_uses_the_read_only_probe(self) -> None:
        with patch.object(ebayda_helper.os, "name", "nt"), patch.object(
            ebayda_helper, "_windows_process_is_alive", return_value=True
        ) as probe:
            self.assertTrue(ebayda_helper._process_is_alive(12345))

        probe.assert_called_once_with(12345)

    def test_resident_retries_persisted_batch_final_event_before_claiming(self) -> None:
        for status, message in (
            ("draft_saved", "草稿已保存"),
            ("terminated_unknown", "执行进程已结束，结果未知"),
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ebayda_helper._write_bindings(root, {"101": "aaaaaaaaaaaaaaaa"})
                job = helper_runtime.ClaimedJob.from_payload(self.payload("item_1", "101"))
                ebayda_helper._queue_pending_final_event(root, job, status, message)
                reports: list[tuple[str, str, str, str | None]] = []
                runner = ebayda_helper.ResidentRunner(
                    root,
                    claim_next_request=lambda _token: ebayda_helper.DevicePoll((), None),
                    report_final_event_request=lambda item_id, token, event_status, *, message=None: reports.append(
                        (item_id, token, event_status, message)
                    ) or True,
                )

                runner.run_once()

                self.assertEqual(
                    reports,
                    [("item_1", "aaaaaaaaaaaaaaaa", status, message)],
                )
                self.assertEqual(ebayda_helper._read_pending_final_events(root), [])

    def test_resident_keeps_final_event_when_the_server_reports_a_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ebayda_helper._write_bindings(root, {"101": "aaaaaaaaaaaaaaaa"})
            job = helper_runtime.ClaimedJob.from_payload(self.payload("item_1", "101"))
            ebayda_helper._queue_pending_final_event(root, job, "draft_saved")
            runner = ebayda_helper.ResidentRunner(
                root,
                claim_next_request=lambda _token: ebayda_helper.DevicePoll((), None),
                report_final_event_request=lambda *_args, **_kwargs: False,
            )

            runner.run_once()

            self.assertEqual(
                ebayda_helper._read_pending_final_events(root),
                [ebayda_helper.PendingFinalEvent("item_1", "draft_saved", "")],
            )


class CommandLineTests(unittest.TestCase):
    def test_busy_helper_does_not_claim_the_ticket(self) -> None:
        error_output = io.StringIO()
        ticket = "abcdefghijklmnop"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with helper_runtime.instance_lock(root), patch.object(
                ebayda_helper, "application_root", return_value=root
            ), patch.object(ebayda_helper, "claim_job") as claim, redirect_stderr(
                error_output
            ):
                exit_code = ebayda_helper.main(
                    [f"ebayda://run?job_id=job_1&ticket={ticket}"]
                )

        self.assertEqual(exit_code, 2)
        claim.assert_not_called()
        self.assertEqual(
            json.loads(error_output.getvalue()),
            {"status": "failed", "error": "助手正在执行另一个任务，请稍后重试"},
        )
        self.assertNotIn(ticket, error_output.getvalue())

    def test_binding_can_run_while_the_resident_helper_holds_its_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with helper_runtime.resident_lock(root), patch.object(
                ebayda_helper, "application_root", return_value=root
            ), patch.object(ebayda_helper, "_run_binding", return_value=0) as run_binding, patch.object(
                ebayda_helper, "_start_resident_after_binding"
            ) as start_resident:
                exit_code = ebayda_helper.main(
                    ["ebayda://shop-binding?action=bind&ticket=abcdefghijklmnop"]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            run_binding.call_args.args[0],
            ebayda_helper.BindingRequest(action="bind", ticket="abcdefghijklmnop"),
        )
        start_resident.assert_called_once()

    def test_legacy_launch_can_run_while_the_resident_helper_holds_its_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with helper_runtime.resident_lock(root), patch.object(
                ebayda_helper, "application_root", return_value=root
            ), patch.object(ebayda_helper, "_run_launch", return_value=0) as run_launch:
                exit_code = ebayda_helper.main(
                    ["ebayda://run?job_id=job_1&ticket=abcdefghijklmnop"]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            run_launch.call_args.args[0],
            ebayda_helper.LaunchRequest(job_id="job_1", ticket="abcdefghijklmnop"),
        )

    def test_binding_leaves_active_task_files_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_file = root / "jobs" / "item_1" / "product.json"
            task_file.parent.mkdir(parents=True)
            task_file.write_text("{}", encoding="utf-8")
            token = "device-token-abcdefghijklmnop"
            with patch.object(ebayda_helper, "application_root", return_value=root), patch.object(
                ebayda_helper,
                "claim_binding",
                return_value={"shop_id": 101},
            ) as claim, patch.object(ebayda_helper, "_new_device_token", return_value=token), patch.object(
                ebayda_helper, "bind_shop"
            ) as bind, redirect_stdout(io.StringIO()):
                exit_code = ebayda_helper._run_binding(
                    ebayda_helper.BindingRequest(action="bind", ticket="abcdefghijklmnop")
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(task_file.exists())
            claim.assert_called_once_with(
                ebayda_helper.BindingRequest(action="bind", ticket="abcdefghijklmnop"), device_token=token
            )
            bind.assert_called_once_with("101", app_root=root, device_token=token)

    def test_success_prints_only_safe_execution_summary(self) -> None:
        output = io.StringIO()
        with patch.object(
            ebayda_helper,
            "claim_job",
            return_value={"job_id": "job_1", "shop_id": "101", "action": "save_draft"},
        ), patch.object(
            ebayda_helper,
            "execute_claimed_job",
            return_value=("draft_saved", "job_1", "101"),
        ), patch.object(
            ebayda_helper, "instance_lock", return_value=nullcontext()
        ), redirect_stdout(output):
            exit_code = ebayda_helper.main(
                ["ebayda://run?job_id=job_1&ticket=abcdefghijklmnop"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"status": "draft_saved", "job_id": "job_1", "shop_id": "101"},
        )
        self.assertNotIn("abcdefghijklmnop", output.getvalue())

    def test_failure_is_safe_and_does_not_echo_launch_url(self) -> None:
        error_output = io.StringIO()
        with patch.object(
            ebayda_helper,
            "claim_job",
            side_effect=ebayda_helper.HelperError("领取任务失败"),
        ), patch.object(
            ebayda_helper, "instance_lock", return_value=nullcontext()
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

    def test_malformed_url_returns_safe_failure_instead_of_raising(self) -> None:
        error_output = io.StringIO()

        with redirect_stderr(error_output):
            exit_code = ebayda_helper.main(["ebayda://["])

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            json.loads(error_output.getvalue()),
            {"status": "failed", "error": "启动地址格式错误"},
        )

    def test_paused_execution_returns_two(self) -> None:
        output = io.StringIO()
        with patch.object(ebayda_helper, "claim_job", return_value={}), patch.object(
            ebayda_helper,
            "execute_claimed_job",
            return_value=("paused_for_user", "job_1", "101"),
        ), patch.object(
            ebayda_helper, "instance_lock", return_value=nullcontext()
        ), redirect_stdout(output):
            exit_code = ebayda_helper.main(
                ["ebayda://run?job_id=job_1&ticket=abcdefghijklmnop"]
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "paused_for_user")


class _EventResponse:
    status = 204

    def __enter__(self) -> "_EventResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return b""


class EventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload: dict[str, object] = {
            "job_id": "job_1",
            "shop_id": "101",
            "action": "save_draft",
            "job_token": "abcdefghijklmnop",
            "product_json_url": (
                "https://www.ebayda.com/api/automation/jobs/job_1/product-json"
            ),
            "images_zip_url": (
                "https://www.ebayda.com/api/automation/jobs/job_1/images"
            ),
            "size_chart_url": (
                "https://www.ebayda.com/api/automation/jobs/job_1/size-chart"
            ),
        }
        self.job = helper_runtime.ClaimedJob.from_payload(self.payload)
        self.shop_execution_lock = patch.object(
            ebayda_helper, "shop_execution_lock", return_value=nullcontext()
        )
        self.shop_execution_lock_mock = self.shop_execution_lock.start()
        self.addCleanup(self.shop_execution_lock.stop)

    def test_event_posts_safe_json_with_job_authorization(self) -> None:
        captured: list[tuple[object, int]] = []

        def open_url(request: object, *, timeout: int) -> _EventResponse:
            captured.append((request, timeout))
            return _EventResponse()

        ebayda_helper.post_event(self.job, "running", open_url=open_url)

        request, timeout = captured[0]
        headers = {name.casefold(): value for name, value in request.header_items()}
        self.assertEqual(
            request.full_url,
			"https://www.ebayda.com/api/automation/jobs/job_1/events",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"status": "running"})
        self.assertEqual(headers["authorization"], "JobToken abcdefghijklmnop")
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(timeout, ebayda_helper.EVENT_TIMEOUT_SECONDS)

    def test_event_includes_safe_reason_when_provided(self) -> None:
        captured: list[object] = []

        def open_url(request: object, *, timeout: int) -> _EventResponse:
            captured.append(request)
            return _EventResponse()

        ebayda_helper.post_event(
            self.job,
            "paused_for_user",
            message="请先登录（job_token=secret-job-token）",
            open_url=open_url,
        )

        request = captured[0]
        self.assertEqual(
            json.loads(request.data),
            {"status": "paused_for_user", "message": "请先登录（job_token=[已打码]）"},
        )

    def test_execution_emits_stages_in_order(self) -> None:
        calls: list[str] = []
        files = helper_runtime.TaskFiles(Path("product.json"), Path("images.zip"), Path("work"))

        with patch.object(
            ebayda_helper, "application_root", return_value=Path("app")
        ), patch.object(
            ebayda_helper,
            "post_event",
            side_effect=lambda _job, status: calls.append(status),
        ), patch.object(
            ebayda_helper,
            "prepare_job_files",
            side_effect=lambda *_args: calls.append("download") or files,
        ), patch.object(
            ebayda_helper,
            "shop_profile",
            side_effect=lambda *_args: calls.append("profile") or Path("profile"),
        ), patch.object(
            ebayda_helper,
            "ensure_chrome",
            side_effect=lambda *_args: calls.append("chrome") or 17321,
        ), patch.object(
            ebayda_helper,
            "run_automation",
            side_effect=lambda *_args: calls.append("automation") or 0,
        ):
            result = ebayda_helper.execute_claimed_job(self.payload)

        self.assertEqual(result, ("draft_saved", "job_1", "101"))
        self.assertEqual(
            calls,
            [
                "preparing",
                "download",
                "profile",
                "chrome",
                "running",
                "automation",
                "draft_saved",
            ],
        )
        self.shop_execution_lock_mock.assert_called_once_with(Path("app"), "101")

    def test_automation_exit_codes_map_to_public_statuses(self) -> None:
        expected = {
            0: "draft_saved",
            2: "paused_for_user",
            3: "paused_for_user",
            130: "paused_for_user",
            4: "paused_for_user",
        }
        files = helper_runtime.TaskFiles(Path("product.json"), Path("images.zip"), Path("work"))

        for exit_code, status in expected.items():
            with self.subTest(exit_code=exit_code), patch.object(
                ebayda_helper, "application_root", return_value=Path("app")
            ), patch.object(ebayda_helper, "post_event"), patch.object(
                ebayda_helper, "prepare_job_files", return_value=files
            ), patch.object(
                ebayda_helper, "shop_profile", return_value=Path("profile")
            ), patch.object(
                ebayda_helper, "ensure_chrome", return_value=17321
            ), patch.object(
                ebayda_helper, "run_automation", return_value=exit_code
            ):
                result = ebayda_helper.execute_claimed_job(self.payload)

            self.assertEqual(result[0], status)

    def test_unconfirmed_draft_save_waits_for_review(self) -> None:
        files = helper_runtime.TaskFiles(Path("product.json"), Path("images.zip"), Path("work"))
        events: list[tuple[str, str | None]] = []

        def post_event(_job: object, status: str, **kwargs: object) -> None:
            events.append((status, kwargs.get("message")))

        with patch.object(
            ebayda_helper, "application_root", return_value=Path("app")
        ), patch.object(ebayda_helper, "post_event", side_effect=post_event), patch.object(
            ebayda_helper, "prepare_job_files", return_value=files
        ), patch.object(
            ebayda_helper, "shop_profile", return_value=Path("profile")
        ), patch.object(
            ebayda_helper, "ensure_chrome", return_value=17321
        ), patch.object(
            ebayda_helper,
            "run_automation",
            return_value=helper_runtime.AutomationRun(
                exit_code=3,
                status="not_saved",
                message="没有捕获到保存草稿成功提示",
            ),
        ):
            result = ebayda_helper.execute_claimed_job(self.payload)

        self.assertEqual(result[0], "needs_confirmation")
        self.assertEqual(events[-1], ("needs_confirmation", "没有捕获到保存草稿成功提示"))

    def test_automation_reason_is_sent_with_terminal_event(self) -> None:
        files = helper_runtime.TaskFiles(Path("product.json"), Path("images.zip"), Path("work"))
        events: list[tuple[str, str | None]] = []

        def post_event(_job: object, status: str, **kwargs: object) -> None:
            events.append((status, kwargs.get("message")))

        with patch.object(
            ebayda_helper, "application_root", return_value=Path("app")
        ), patch.object(ebayda_helper, "post_event", side_effect=post_event), patch.object(
            ebayda_helper, "prepare_job_files", return_value=files
        ), patch.object(
            ebayda_helper, "shop_profile", return_value=Path("profile")
        ), patch.object(
            ebayda_helper, "ensure_chrome", return_value=17321
        ), patch.object(
            ebayda_helper,
            "run_automation",
            return_value=helper_runtime.AutomationRun(
                exit_code=2,
                message="请先登录得物商家后台",
            ),
        ):
            result = ebayda_helper.execute_claimed_job(self.payload)

        self.assertEqual(result[0], "paused_for_user")
        self.assertEqual(events[-1], ("paused_for_user", "请先登录得物商家后台"))

    def test_pre_browser_failure_reports_failed(self) -> None:
        events: list[str] = []
        with patch.object(
            ebayda_helper, "application_root", return_value=Path("app")
        ), patch.object(
            ebayda_helper,
            "post_event",
            side_effect=lambda _job, status: events.append(status),
        ), patch.object(
            ebayda_helper,
            "prepare_job_files",
            side_effect=helper_runtime.TaskExecutionError("下载失败"),
        ), patch.object(ebayda_helper, "ensure_chrome") as chrome:
            with self.assertRaises(ebayda_helper.HelperError):
                ebayda_helper.execute_claimed_job(self.payload)

        chrome.assert_not_called()
        self.assertEqual(events, ["preparing", "failed"])

    def test_final_event_failure_does_not_change_saved_result(self) -> None:
        files = helper_runtime.TaskFiles(Path("product.json"), Path("images.zip"), Path("work"))

        def post_event(_job: object, status: str) -> None:
            if status == "draft_saved":
                raise ebayda_helper.HelperError("回传失败")

        with patch.object(
            ebayda_helper, "application_root", return_value=Path("app")
        ), patch.object(ebayda_helper, "post_event", side_effect=post_event), patch.object(
            ebayda_helper, "prepare_job_files", return_value=files
        ), patch.object(
            ebayda_helper, "shop_profile", return_value=Path("profile")
        ), patch.object(
            ebayda_helper, "ensure_chrome", return_value=17321
        ), patch.object(ebayda_helper, "run_automation", return_value=0) as runner:
            result = ebayda_helper.execute_claimed_job(self.payload)

        self.assertEqual(result[0], "draft_saved")
        runner.assert_called_once()

    def test_batch_final_event_network_failure_is_persisted_without_a_job_token(self) -> None:
        payload = {
            **self.payload,
            "job_id": "item_1",
            "product_json_url": "https://www.ebayda.com/api/automation/batch-items/item_1/product-json",
            "images_zip_url": "https://www.ebayda.com/api/automation/batch-items/item_1/images",
            "size_chart_url": "https://www.ebayda.com/api/automation/batch-items/item_1/size-chart",
        }
        files = helper_runtime.TaskFiles(Path("product.json"), Path("images.zip"), Path("work"))

        def post_event(_job: object, status: str) -> None:
            if status == "draft_saved":
                raise ebayda_helper.HelperError("回传失败")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(ebayda_helper, "application_root", return_value=root), patch.object(
                ebayda_helper, "post_event", side_effect=post_event
            ), patch.object(ebayda_helper, "prepare_job_files", return_value=files), patch.object(
                ebayda_helper, "shop_profile", return_value=Path("profile")
            ), patch.object(ebayda_helper, "ensure_chrome", return_value=17321), patch.object(
                ebayda_helper, "run_automation", return_value=0
            ):
                result = ebayda_helper.execute_claimed_job(payload)

            events = ebayda_helper._read_pending_final_events(root)
            saved = (root / "pending-final-events" / "item_1.json").read_text(encoding="utf-8")

        self.assertEqual(result[0], "draft_saved")
        self.assertEqual(events, [ebayda_helper.PendingFinalEvent("item_1", "draft_saved", "")])
        self.assertNotIn("abcdefghijklmnop", saved)


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
        self.assertIn("Software\\Microsoft\\Windows\\CurrentVersion\\Run", script)
        self.assertIn('Parameters: "--resident"', script)

        build_script = (
            Path(__file__).with_name("installer") / "build-helper.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("python -m PyInstaller --version | Out-Null", build_script)

        workflow = (
            Path(__file__).parent / ".github" / "workflows" / "windows-installer.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn("python -m unittest discover", workflow)
        self.assertIn("choco install innosetup", workflow)
        self.assertIn("installer/build-helper.ps1", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
