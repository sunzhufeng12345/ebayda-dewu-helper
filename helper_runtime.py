from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import main as dewu_main


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


def application_root() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise TaskExecutionError("无法确定 Windows 本地应用数据目录")
        return Path(local_app_data) / "EbaydaHelper"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "EbaydaHelper"
    data_home = os.environ.get("XDG_DATA_HOME")
    return Path(data_home) / "EbaydaHelper" if data_home else Path.home() / ".local" / "share" / "EbaydaHelper"


def shop_profile(app_root: Path, shop_id: str) -> Path:
    if not SAFE_ID_PATTERN.fullmatch(shop_id):
        raise TaskExecutionError("无法创建店铺 Profile：shop_id 格式错误")
    return app_root / "profiles" / shop_id


def find_chrome_executable(candidates: Sequence[Path] | None = None) -> Path:
    if candidates is None:
        if sys.platform == "win32":
            roots = (
                os.environ.get("PROGRAMFILES"),
                os.environ.get("PROGRAMFILES(X86)"),
                os.environ.get("LOCALAPPDATA"),
            )
            candidates = tuple(
                Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
                for root in roots
                if root
            )
        elif sys.platform == "darwin":
            candidates = (
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            )
        else:
            candidates = (
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/google-chrome-stable"),
                Path("/usr/bin/chromium"),
            )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise TaskExecutionError("未找到 Google Chrome，请先安装后重试")


def ensure_chrome(
    profile_dir: Path,
    *,
    chrome_executable: Path | None = None,
    popen: Callable[[list[str]], Any] = subprocess.Popen,
    port_is_open: Callable[[int], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 20,
) -> int:
    profile_dir.mkdir(parents=True, exist_ok=True)
    active_port_file = profile_dir / "DevToolsActivePort"
    is_open = port_is_open or _port_is_open
    existing_port = _read_devtools_port(active_port_file)
    if existing_port is not None and is_open(existing_port):
        return existing_port

    active_port_file.unlink(missing_ok=True)
    chrome = chrome_executable or find_chrome_executable()
    command = [
        str(chrome),
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        dewu_main.START_PAGE_URL,
    ]
    try:
        popen(command)
    except OSError:
        raise TaskExecutionError("无法启动 Google Chrome") from None

    deadline = time.monotonic() + timeout
    while True:
        port = _read_devtools_port(active_port_file)
        if port is not None and is_open(port):
            return port
        if time.monotonic() >= deadline:
            break
        sleep(0.1)
    raise TaskExecutionError("Chrome 启动超时，请关闭该店铺的旧自动化窗口后重试")


def run_automation(
    files: TaskFiles,
    port: int,
    *,
    runner: Callable[[Sequence[str]], int] = dewu_main.main,
) -> int:
    arguments = [
        "--json",
        str(files.json_path),
        "--images",
        str(files.images_path),
        "--work-dir",
        str(files.work_dir),
        "--port",
        str(port),
        "--execute",
    ]
    return runner(arguments)


def _read_devtools_port(path: Path) -> int | None:
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        port = int(first_line)
    except (FileNotFoundError, IndexError, OSError, UnicodeError, ValueError):
        return None
    return port if 1 <= port <= 65_535 else None


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


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
