from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DOWNLOAD_TIMEOUT_SECONDS = 120
DOWNLOAD_CHUNK_BYTES = 64 * 1024
MAX_PRODUCT_JSON_BYTES = 10 * 1024 * 1024
MAX_IMAGES_ZIP_BYTES = 2 * 1024 * 1024 * 1024
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
TRUSTED_DOWNLOAD_HOST = "www.ebayda.com"


class TaskExecutionError(RuntimeError):
    """A claimed task cannot be prepared or executed safely."""


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    shop_id: str
    action: str
    job_token: str
    product_json_url: str
    images_zip_url: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ClaimedJob":
        job_id = str(payload.get("job_id") or "")
        shop_id = str(payload.get("shop_id") or "")
        action = str(payload.get("action") or "")
        job_token = str(payload.get("job_token") or "")
        if not SAFE_ID_PATTERN.fullmatch(job_id):
            raise TaskExecutionError("任务数据错误：job_id 格式错误")
        if not SAFE_ID_PATTERN.fullmatch(shop_id):
            raise TaskExecutionError("任务数据错误：shop_id 格式错误")
        if action != "save_draft":
            raise TaskExecutionError("任务数据错误：不支持的任务动作")
        if not _valid_token(job_token):
            raise TaskExecutionError("任务数据错误：job_token 格式错误")

        product_json_url = _trusted_resource_url(
            payload.get("product_json_url"), job_id, "product-json"
        )
        images_zip_url = _trusted_resource_url(
            payload.get("images_zip_url"), job_id, "images"
        )
        return cls(
            job_id=job_id,
            shop_id=shop_id,
            action=action,
            job_token=job_token,
            product_json_url=product_json_url,
            images_zip_url=images_zip_url,
        )


@dataclass(frozen=True)
class TaskFiles:
    json_path: Path
    images_path: Path
    work_dir: Path


def prepare_job_files(
    job: ClaimedJob,
    app_root: Path,
    *,
    open_url: Callable[..., Any] = urlopen,
) -> TaskFiles:
    job_dir = app_root / "jobs" / job.job_id
    work_dir = job_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    json_path = job_dir / "product.json"
    images_path = job_dir / "images.zip"
    _download(
        job.product_json_url,
        json_path,
        job.job_token,
        MAX_PRODUCT_JSON_BYTES,
        open_url,
    )
    _download(
        job.images_zip_url,
        images_path,
        job.job_token,
        MAX_IMAGES_ZIP_BYTES,
        open_url,
    )
    return TaskFiles(json_path=json_path, images_path=images_path, work_dir=work_dir)


def _valid_token(value: str) -> bool:
    return (
        16 <= len(value) <= 2_048
        and not any(character.isspace() for character in value)
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _trusted_resource_url(value: object, job_id: str, resource: str) -> str:
    url = str(value or "")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as error:
        raise TaskExecutionError("任务数据错误：下载地址格式错误") from error
    expected_path = f"/api/automation/jobs/{job_id}/{resource}"
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname != TRUSTED_DOWNLOAD_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path != expected_path
        or parsed.params
        or parsed.fragment
    ):
        raise TaskExecutionError("任务数据错误：下载地址不受信任")
    return url


def _download(
    url: str,
    destination: Path,
    job_token: str,
    maximum_bytes: int,
    open_url: Callable[..., Any],
) -> None:
    partial = destination.with_name(f"{destination.name}.part")
    request = Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "Authorization": f"JobToken {job_token}",
            "User-Agent": "EbaydaHelper/0.2",
        },
        method="GET",
    )
    try:
        with open_url(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            if getattr(response, "status", 200) != 200:
                raise TaskExecutionError(f"下载任务文件失败：HTTP {response.status}")
            _validate_content_length(response.headers.get("Content-Length"), maximum_bytes)
            total = 0
            with partial.open("wb") as output:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > maximum_bytes:
                        raise TaskExecutionError("下载任务文件失败：文件过大")
                    output.write(chunk)
        partial.replace(destination)
    except TaskExecutionError:
        partial.unlink(missing_ok=True)
        raise
    except HTTPError as error:
        partial.unlink(missing_ok=True)
        raise TaskExecutionError(f"下载任务文件失败：HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        partial.unlink(missing_ok=True)
        raise TaskExecutionError("下载任务文件失败：网络或本地文件错误") from None


def _validate_content_length(value: object, maximum_bytes: int) -> None:
    if value in (None, ""):
        return
    try:
        length = int(str(value))
    except ValueError as error:
        raise TaskExecutionError("下载任务文件失败：Content-Length 格式错误") from error
    if length < 0 or length > maximum_bytes:
        raise TaskExecutionError("下载任务文件失败：文件过大")
