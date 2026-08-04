from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from inspect import signature
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

import helper_runtime


def valid_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": "job_1",
        "shop_id": 101,
        "action": "save_draft",
        "job_token": "abcdefghijklmnop",
        "product_json_url": (
            "https://www.ebayda.com/api/automation/jobs/job_1/product-json?signature=json"
        ),
        "images_zip_url": (
            "https://www.ebayda.com/api/automation/jobs/job_1/images?signature=zip"
        ),
    }
    payload.update(updates)
    return payload


class _DownloadResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_length: int | None = None,
        read_error: BaseException | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = (
            {} if content_length is None else {"Content-Length": str(content_length)}
        )
        self.read_error = read_error
        self.offset = 0

    def __enter__(self) -> "_DownloadResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class _DownloadOpener:
    def __init__(self, responses: dict[str, _DownloadResponse]) -> None:
        self.responses = responses
        self.requests: list[object] = []

    def __call__(self, request: object, *, timeout: int) -> _DownloadResponse:
        self.requests.append((request, timeout))
        return self.responses[request.full_url]


class ClaimedJobTests(unittest.TestCase):
    def test_valid_payload_is_normalized(self) -> None:
        job = helper_runtime.ClaimedJob.from_payload(valid_payload())

        self.assertEqual(job.job_id, "job_1")
        self.assertEqual(job.shop_id, "101")
        self.assertEqual(job.action, "save_draft")

    def test_tencent_cloud_payload_is_accepted(self) -> None:
        job = helper_runtime.ClaimedJob.from_payload(
            valid_payload(
                product_json_url=(
                    "http://101.34.90.101:10112/api/automation/jobs/"
                    "job_1/product-json"
                ),
                images_zip_url=(
                    "http://101.34.90.101:10112/api/automation/jobs/"
                    "job_1/images"
                ),
            )
        )

        self.assertEqual(
            job.product_json_url,
            "http://101.34.90.101:10112/api/automation/jobs/job_1/product-json",
        )
        self.assertEqual(
            job.images_zip_url,
            "http://101.34.90.101:10112/api/automation/jobs/job_1/images",
        )

    def test_urls_must_be_https_ebayda_job_resources(self) -> None:
        invalid_urls = (
            "http://www.ebayda.com/api/automation/jobs/job_1/product-json",
            "https://evil.example/api/automation/jobs/job_1/product-json",
            "https://www.ebayda.com/api/automation/jobs/other/product-json",
            "https://user@www.ebayda.com/api/automation/jobs/job_1/product-json",
            "https://www.ebayda.com:444/api/automation/jobs/job_1/product-json",
            "https://www.ebayda.com/api/automation/jobs/job_1/product-json#fragment",
        )

        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(
                helper_runtime.TaskExecutionError
            ):
                helper_runtime.ClaimedJob.from_payload(
                    valid_payload(product_json_url=url)
                )

    def test_required_tokens_and_fields_are_validated(self) -> None:
        invalid_updates = (
            {"job_token": "short"},
            {"job_token": "valid-token-with-space "},
            {"action": "submit"},
            {"shop_id": "../101"},
            {"images_zip_url": ""},
        )

        for updates in invalid_updates:
            with self.subTest(updates=updates), self.assertRaises(
                helper_runtime.TaskExecutionError
            ):
                helper_runtime.ClaimedJob.from_payload(valid_payload(**updates))


class DownloadTests(unittest.TestCase):
    def test_download_uses_job_token_and_atomic_fixed_filenames(self) -> None:
        job = helper_runtime.ClaimedJob.from_payload(valid_payload())
        opener = _DownloadOpener(
            {
                job.product_json_url: _DownloadResponse(b'{"data": {}}'),
                job.images_zip_url: _DownloadResponse(b"PK\x03\x04zip"),
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            files = helper_runtime.prepare_job_files(
                job,
                Path(directory),
                open_url=opener,
            )

            self.assertEqual(files.json_path.name, "product.json")
            self.assertEqual(files.images_path.name, "images.zip")
            self.assertEqual(files.json_path.read_bytes(), b'{"data": {}}')
            self.assertEqual(files.images_path.read_bytes(), b"PK\x03\x04zip")
            self.assertFalse(files.json_path.with_name("product.json.part").exists())
            self.assertFalse(files.images_path.with_name("images.zip.part").exists())

        for request, timeout in opener.requests:
            headers = {name.casefold(): value for name, value in request.header_items()}
            self.assertEqual(headers["authorization"], "JobToken abcdefghijklmnop")
            self.assertEqual(timeout, helper_runtime.DOWNLOAD_TIMEOUT_SECONDS)

    def test_declared_oversized_download_is_rejected(self) -> None:
        job = helper_runtime.ClaimedJob.from_payload(valid_payload())
        opener = _DownloadOpener(
            {
                job.product_json_url: _DownloadResponse(
                    b"",
                    content_length=helper_runtime.MAX_PRODUCT_JSON_BYTES + 1,
                ),
                job.images_zip_url: _DownloadResponse(b"unused"),
            }
        )

        with tempfile.TemporaryDirectory() as directory, self.assertRaises(
            helper_runtime.TaskExecutionError
        ):
            helper_runtime.prepare_job_files(job, Path(directory), open_url=opener)

    def test_streamed_oversized_download_is_rejected(self) -> None:
        job = helper_runtime.ClaimedJob.from_payload(valid_payload())
        opener = _DownloadOpener(
            {
                job.product_json_url: _DownloadResponse(
                    b"x" * (helper_runtime.MAX_PRODUCT_JSON_BYTES + 1)
                ),
                job.images_zip_url: _DownloadResponse(b"unused"),
            }
        )

        with tempfile.TemporaryDirectory() as directory, self.assertRaises(
            helper_runtime.TaskExecutionError
        ):
            helper_runtime.prepare_job_files(job, Path(directory), open_url=opener)

    def test_failed_download_removes_partial_file_and_hides_token(self) -> None:
        job = helper_runtime.ClaimedJob.from_payload(valid_payload())
        opener = _DownloadOpener(
            {
                job.product_json_url: _DownloadResponse(
                    b"",
                    read_error=URLError(job.job_token),
                ),
                job.images_zip_url: _DownloadResponse(b"unused"),
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(helper_runtime.TaskExecutionError) as caught:
                helper_runtime.prepare_job_files(job, root, open_url=opener)

            self.assertNotIn(job.job_token, str(caught.exception))
            self.assertFalse(
                (root / "jobs" / job.job_id / "product.json.part").exists()
            )


class TaskCleanupTests(unittest.TestCase):
    def test_cleanup_task_files_removes_task_artifacts_but_keeps_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_dir = root / "jobs" / "job_1"
            task_dir.mkdir(parents=True)
            (task_dir / "product.json").write_text("{}", encoding="utf-8")
            (task_dir / "images.zip").write_bytes(b"zip")
            (task_dir / "work" / "PB26XY01LJB-ZB9603").mkdir(parents=True)
            profile = root / "profiles" / "101"
            profile.mkdir(parents=True)
            (profile / "Cookies").write_bytes(b"keep")

            helper_runtime.cleanup_task_files(root, "job_1")

            self.assertFalse(task_dir.exists())
            self.assertEqual((profile / "Cookies").read_bytes(), b"keep")

    def test_cleanup_stale_task_files_removes_only_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "jobs" / "job_1").mkdir(parents=True)
            (root / "jobs" / "job_2").mkdir()
            (root / "profiles" / "101").mkdir(parents=True)

            helper_runtime.cleanup_stale_task_files(root)

            self.assertFalse((root / "jobs").exists())
            self.assertTrue((root / "profiles" / "101").is_dir())


class NetworkPolicyTests(unittest.TestCase):
    def test_no_redirect_opener_rejects_real_redirect(self) -> None:
        requests: list[tuple[str, str | None]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/source":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                else:
                    self.send_response(204)
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/source",
                headers={"Authorization": "JobToken secret-token"},
            )
            with self.assertRaises(HTTPError) as caught:
                helper_runtime.open_no_redirect(request, timeout=1)

            self.assertEqual(caught.exception.code, 302)
            self.assertEqual(requests, [("/source", "JobToken secret-token")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_redirect_handler_never_forwards_job_authorization(self) -> None:
        handler = helper_runtime.NoRedirectHandler()

        self.assertIsNone(
            handler.redirect_request(
                object(),
                None,
                302,
                "Found",
                {},
                "https://evil.example/resource",
            )
        )

    def test_public_http_clients_default_to_no_redirect_opener(self) -> None:
        self.assertIs(
            signature(helper_runtime.prepare_job_files).parameters[
                "open_url"
            ].default,
            helper_runtime.open_no_redirect,
        )


class InstanceLockTests(unittest.TestCase):
    def test_second_instance_is_rejected_until_first_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            with helper_runtime.instance_lock(root):
                with self.assertRaisesRegex(
                    helper_runtime.TaskExecutionError,
                    "^助手正在执行另一个任务，请稍后重试$",
                ):
                    with helper_runtime.instance_lock(root):
                        self.fail("second instance acquired the lock")

            with helper_runtime.instance_lock(root):
                pass

    def test_windows_lock_uses_nonblocking_first_byte(self) -> None:
        calls: list[tuple[int, int, int]] = []
        fake_msvcrt = SimpleNamespace(
            LK_NBLCK=7,
            locking=lambda fd, mode, size: calls.append((fd, mode, size)),
        )

        with tempfile.TemporaryDirectory() as directory, patch.object(
            helper_runtime.sys, "platform", "win32"
        ), patch.dict(sys.modules, {"msvcrt": fake_msvcrt}):
            root = Path(directory)
            with helper_runtime.instance_lock(root):
                pass

            self.assertEqual((root / "instance.lock").read_bytes(), b"\0")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:], (fake_msvcrt.LK_NBLCK, 1))


class ChromeRuntimeTests(unittest.TestCase):
    def test_shop_profile_is_under_application_data(self) -> None:
        root = Path("C:/data/EbaydaHelper")

        self.assertEqual(
            helper_runtime.shop_profile(root, "101"),
            root / "profiles" / "101",
        )

    def test_application_root_uses_local_app_data_on_windows(self) -> None:
        with patch.object(helper_runtime.sys, "platform", "win32"), patch.dict(
            helper_runtime.os.environ,
            {"LOCALAPPDATA": "C:/Users/test/AppData/Local"},
            clear=False,
        ):
            self.assertEqual(
                helper_runtime.application_root(),
                Path("C:/Users/test/AppData/Local") / "EbaydaHelper",
            )

    def test_existing_live_devtools_port_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / "DevToolsActivePort").write_text(
                "17321\n/devtools/browser/id\n", encoding="utf-8"
            )

            port = helper_runtime.ensure_chrome(
                profile,
                chrome_executable=Path("unused-chrome"),
                popen=lambda _command: self.fail("不应重复启动 Chrome"),
                port_is_open=lambda value: value == 17321,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(port, 17321)

    def test_chrome_uses_non_default_profile_and_ephemeral_debug_port(self) -> None:
        commands: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profiles" / "101"

            def popen(command: list[str]) -> object:
                commands.append(command)
                (profile / "DevToolsActivePort").write_text(
                    "17321\n/devtools/browser/id\n", encoding="utf-8"
                )
                return object()

            port = helper_runtime.ensure_chrome(
                profile,
                chrome_executable=Path(
                    "C:/Program Files/Google/Chrome/Application/chrome.exe"
                ),
                popen=popen,
                port_is_open=lambda value: value == 17321,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(port, 17321)
        self.assertEqual(len(commands), 1)
        self.assertIn(f"--user-data-dir={profile}", commands[0])
        self.assertIn("--remote-debugging-port=0", commands[0])
        self.assertIn("--remote-allow-origins=*", commands[0])
        self.assertIn("--no-first-run", commands[0])
        self.assertIn(helper_runtime.dewu_main.START_PAGE_URL, commands[0])

    def test_invalid_active_port_is_replaced_after_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            active_port = profile / "DevToolsActivePort"
            active_port.write_text("not-a-port\n", encoding="utf-8")

            def popen(_command: list[str]) -> object:
                active_port.write_text("17322\n", encoding="utf-8")
                return object()

            port = helper_runtime.ensure_chrome(
                profile,
                chrome_executable=Path("chrome.exe"),
                popen=popen,
                port_is_open=lambda value: value == 17322,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(port, 17322)

    def test_missing_chrome_and_startup_timeout_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(helper_runtime.TaskExecutionError):
                helper_runtime.find_chrome_executable([root / "missing.exe"])

            chrome = root / "chrome.exe"
            chrome.write_bytes(b"")
            with self.assertRaises(helper_runtime.TaskExecutionError):
                helper_runtime.ensure_chrome(
                    root / "profile",
                    chrome_executable=chrome,
                    popen=lambda _command: object(),
                    port_is_open=lambda _value: False,
                    sleep=lambda _seconds: None,
                    timeout=0,
                )


class AutomationRunnerTests(unittest.TestCase):
    def test_main_accepts_explicit_arguments(self) -> None:
        args = helper_runtime.dewu_main.parse_args(["--skip-size-chart"])

        self.assertTrue(args.skip_size_chart)

    def test_runner_passes_downloads_port_and_save_mode(self) -> None:
        files = helper_runtime.TaskFiles(
            json_path=Path("job/product.json"),
            images_path=Path("job/images.zip"),
            work_dir=Path("job/work"),
        )
        calls: list[list[str]] = []

        exit_code = helper_runtime.run_automation(
            files,
            17321,
            runner=lambda argv: calls.append(list(argv)) or 0,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [
                [
                    "--json",
                    str(files.json_path),
                    "--images",
                    str(files.images_path),
                    "--work-dir",
                    str(files.work_dir),
                    "--port",
                    "17321",
                    "--execute",
                ]
            ],
        )
        self.assertNotIn("--no-save", calls[0])

    def test_runner_output_is_suppressed(self) -> None:
        files = helper_runtime.TaskFiles(
            json_path=Path("job/product.json"),
            images_path=Path("job/images.zip"),
            work_dir=Path("job/work"),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        def noisy_runner(_argv: object) -> int:
            print("private product summary")
            print("private local path", file=sys.stderr)
            return 0

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = helper_runtime.run_automation(files, 17321, runner=noisy_runner)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
