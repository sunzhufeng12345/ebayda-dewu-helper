from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

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


if __name__ == "__main__":
    unittest.main()
