from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse, urlunparse
from urllib.request import Request

from helper_runtime import (
    ClaimedJob,
    TaskExecutionError,
    application_root,
    cleanup_task_files,
    ensure_chrome,
    instance_lock,
    open_no_redirect,
    prepare_job_files,
    resident_lock,
    run_automation,
    shop_execution_lock,
    shop_profile,
)


# 测试环境：生产请改回 https://www.ebayda.com
API_ORIGIN = "http://101.34.90.101:10112"
CLAIM_TIMEOUT_SECONDS = 10
EVENT_TIMEOUT_SECONDS = 10
BINDING_TIMEOUT_SECONDS = 10
DEVICE_TIMEOUT_SECONDS = 10
MAX_CLAIM_RESPONSE_BYTES = 1024 * 1024
MAX_BINDING_RESPONSE_BYTES = 64 * 1024
MAX_DEVICE_RESPONSE_BYTES = 1024 * 1024
MAX_LAUNCH_URL_LENGTH = 4_096
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
RESIDENT_POLL_SECONDS = 3
TERMINATE_TIMEOUT_SECONDS = 10
FINAL_EVENT_STATUSES = {
    "draft_saved",
    "paused_for_user",
    "needs_confirmation",
    "failed",
    "terminated_unknown",
}


class HelperError(RuntimeError):
    """The helper cannot safely accept or claim a launch request."""


def _configured_api_origin() -> str:
    origin = os.environ.get("EBAYDA_API_ORIGIN", API_ORIGIN).rstrip("/")
    if origin == API_ORIGIN:
        return origin

    if os.environ.get("EBAYDA_ALLOW_STAGING_API") == "1":
        try:
            parsed = urlparse(origin)
            parsed.port
        except ValueError:
            raise HelperError("staging API 地址必须是 https://主机[:端口]") from None
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise HelperError("staging API 地址必须是 https://主机[:端口]")
        return origin

    if origin.casefold().startswith("https://"):
        raise HelperError("staging API 地址必须显式启用 EBAYDA_ALLOW_STAGING_API=1")

    if os.environ.get("EBAYDA_ALLOW_LOCAL_API") != "1":
        raise HelperError("本地 API 地址必须显式启用测试开关")

    try:
        parsed = urlparse(origin)
        port = parsed.port
    except ValueError:
        raise HelperError("本地 API 地址必须是 http://127.0.0.1:<端口>") from None
    if (
        parsed.scheme.casefold() != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise HelperError("本地 API 地址必须是 http://127.0.0.1:<端口>")
    return origin


@dataclass(frozen=True)
class LaunchRequest:
    job_id: str
    ticket: str


@dataclass(frozen=True)
class BindingRequest:
    action: str
    ticket: str


@dataclass(frozen=True)
class DevicePoll:
    termination_item_ids: tuple[str, ...]
    item_payload: Mapping[str, Any] | None


@dataclass
class ActiveExecution:
    job: ClaimedJob
    process: Any
    termination_requested: bool = False


@dataclass(frozen=True)
class PendingFinalEvent:
    item_id: str
    status: str
    message: str


def parse_launch_url(value: str) -> LaunchRequest:
    if not value or len(value) > MAX_LAUNCH_URL_LENGTH:
        raise HelperError("启动地址为空或过长")

    try:
        parsed = urlparse(value)
    except ValueError as error:
        raise HelperError("启动地址格式错误") from error
    if (
        parsed.scheme.casefold() != "ebayda"
        or parsed.netloc.casefold() != "run"
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.fragment
    ):
        raise HelperError("不是受支持的 Ebayda 助手启动地址")

    try:
        parameters = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise HelperError("启动参数格式错误") from error
    if set(parameters) != {"job_id", "ticket"}:
        raise HelperError("启动参数必须且只能包含 job_id 和 ticket")
    if any(len(values) != 1 for values in parameters.values()):
        raise HelperError("启动参数不能重复")

    job_id = parameters["job_id"][0]
    ticket = parameters["ticket"][0]
    if not job_id.startswith("job_") or not JOB_ID_PATTERN.fullmatch(job_id):
        raise HelperError("job_id 格式错误")
    if not 16 <= len(ticket) <= 2_048 or any(character.isspace() for character in ticket):
        raise HelperError("ticket 格式错误")
    if any(ord(character) < 33 or ord(character) > 126 for character in ticket):
        raise HelperError("ticket 格式错误")
    return LaunchRequest(job_id=job_id, ticket=ticket)


def parse_binding_url(value: str) -> BindingRequest:
    if not value or len(value) > MAX_LAUNCH_URL_LENGTH:
        raise HelperError("绑定地址为空或过长")
    try:
        parsed = urlparse(value)
    except ValueError as error:
        raise HelperError("绑定地址格式错误") from error
    if (
        parsed.scheme.casefold() != "ebayda"
        or parsed.netloc.casefold() != "shop-binding"
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.fragment
    ):
        raise HelperError("不是受支持的 Ebayda 店铺绑定地址")
    try:
        parameters = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise HelperError("绑定参数格式错误") from error
    if set(parameters) != {"action", "ticket"} or any(
        len(values) != 1 for values in parameters.values()
    ):
        raise HelperError("绑定参数必须且只能包含 action 和 ticket")
    action = parameters["action"][0].casefold()
    ticket = parameters["ticket"][0]
    if action not in {"bind", "unbind"}:
        raise HelperError("绑定动作无效")
    if not 16 <= len(ticket) <= 2_048 or any(
        character.isspace() or not 33 <= ord(character) <= 126
        for character in ticket
    ):
        raise HelperError("绑定 ticket 格式错误")
    return BindingRequest(action=action, ticket=ticket)


def _bindings_path(app_root: Any) -> Any:
    return app_root / "bindings.json"


def _read_bindings(app_root: Any) -> dict[str, str | None]:
    path = _bindings_path(app_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HelperError("本地店铺绑定配置无法读取") from error
    shops = payload.get("shops") if isinstance(payload, Mapping) else None
    if isinstance(shops, list):
        if any(not isinstance(shop_id, str) or not JOB_ID_PATTERN.fullmatch(shop_id) for shop_id in shops):
            raise HelperError("本地店铺绑定配置格式错误")
        return {shop_id: None for shop_id in shops}
    if not isinstance(shops, Mapping):
        raise HelperError("本地店铺绑定配置格式错误")
    bindings: dict[str, str | None] = {}
    for shop_id, value in shops.items():
        if not isinstance(shop_id, str) or not JOB_ID_PATTERN.fullmatch(shop_id) or not isinstance(value, Mapping):
            raise HelperError("本地店铺绑定配置格式错误")
        token = value.get("device_token")
        if token is not None and (not isinstance(token, str) or not _valid_device_token(token)):
            raise HelperError("本地店铺绑定配置格式错误")
        bindings[shop_id] = token
    return bindings


def _read_bound_shops(app_root: Any) -> set[str]:
    return set(_read_bindings(app_root))


def _write_bindings(app_root: Any, bindings: Mapping[str, str | None]) -> None:
    app_root.mkdir(parents=True, exist_ok=True)
    path = _bindings_path(app_root)
    temporary = path.with_name(f"{path.name}.part")
    payload = {
        "shops": {
            shop_id: ({"device_token": token} if token else {})
            for shop_id, token in sorted(bindings.items())
        }
    }
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise HelperError("本地店铺绑定配置无法保存") from error


def _write_bound_shops(app_root: Any, shops: set[str]) -> None:
    _write_bindings(app_root, {shop_id: None for shop_id in shops})


def _pending_final_events_dir(app_root: Path) -> Path:
    return app_root / "pending-final-events"


def _pending_final_event_path(app_root: Path, item_id: str) -> Path:
    if not JOB_ID_PATTERN.fullmatch(item_id):
        raise HelperError("待回传执行项 ID 格式错误")
    return _pending_final_events_dir(app_root) / f"{item_id}.json"


def _queue_pending_final_event(
    app_root: Path,
    job: ClaimedJob,
    status: str,
    message: str | None = None,
) -> None:
    if not job.job_id.startswith("item_") or status not in FINAL_EVENT_STATUSES:
        return
    path = _pending_final_event_path(app_root, job.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.part")
    payload = {
        "item_id": job.job_id,
        "status": status,
        "message": _safe_event_message(message) if message else "",
    }
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise HelperError("待回传任务状态无法保存") from error


def _read_pending_final_events(app_root: Path) -> list[PendingFinalEvent]:
    directory = _pending_final_events_dir(app_root)
    if not directory.is_dir() or directory.is_symlink():
        return []
    events: list[PendingFinalEvent] = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            item_id = str(payload.get("item_id") or "")
            status = str(payload.get("status") or "")
            message = str(payload.get("message") or "")
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            continue
        if path != _pending_final_event_path(app_root, item_id) or status not in FINAL_EVENT_STATUSES:
            continue
        events.append(PendingFinalEvent(item_id, status, _safe_event_message(message)))
    return events


def _remove_pending_final_event(app_root: Path, item_id: str) -> None:
    try:
        _pending_final_event_path(app_root, item_id).unlink(missing_ok=True)
    except OSError:
        pass


def _execution_marker_path(app_root: Path, item_id: str) -> Path:
    if not JOB_ID_PATTERN.fullmatch(item_id):
        raise HelperError("执行进程 ID 格式错误")
    return app_root / "active-executions" / f"{item_id}.json"


def _write_execution_marker(app_root: Path, item_id: str, process: Any) -> None:
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return
    path = _execution_marker_path(app_root, item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.part")
    payload: dict[str, int] = {"pid": pid}
    if os.name == "nt":
        payload["started_uptime_ms"] = _windows_uptime_ms()
    try:
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise HelperError("执行进程状态无法保存") from error


def _execution_marker_state(app_root: Path, item_id: str) -> tuple[bool, int | None, int | None]:
    path = _execution_marker_path(app_root, item_id)
    if path.is_symlink():
        return True, None, None
    if not path.exists():
        return False, None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = payload.get("pid") if isinstance(payload, Mapping) else None
        started_uptime_ms = payload.get("started_uptime_ms") if isinstance(payload, Mapping) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True, None, None
    return (
        True,
        pid if isinstance(pid, int) and pid > 0 else None,
        started_uptime_ms if type(started_uptime_ms) is int and started_uptime_ms >= 0 else None,
    )


def _execution_marker_pid(app_root: Path, item_id: str) -> tuple[bool, int | None]:
    marker_found, pid, _ = _execution_marker_state(app_root, item_id)
    return marker_found, pid


def _remove_execution_marker(app_root: Path, item_id: str) -> None:
    try:
        _execution_marker_path(app_root, item_id).unlink(missing_ok=True)
    except OSError:
        pass


def _process_is_alive(pid: int) -> bool:
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _marker_process_is_alive(pid: int | None, started_uptime_ms: int | None) -> bool:
    if pid is None:
        return False
    # A smaller uptime proves this marker belongs to a prior Windows boot,
    # even if the operating system has reused the PID.
    if os.name == "nt" and started_uptime_ms is not None and _windows_uptime_ms() < started_uptime_ms:
        return False
    return _process_is_alive(pid)


def _windows_uptime_ms() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetTickCount64.argtypes = ()
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    return int(kernel32.GetTickCount64())


def _windows_process_is_alive(pid: int) -> bool:
    # os.kill(pid, 0) is not a signal-free existence check on Windows.
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # Access denied is conservative: never acknowledge termination while
        # a process could still be running.
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such process
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def is_shop_bound(app_root: Any, shop_id: str) -> bool:
    if not JOB_ID_PATTERN.fullmatch(shop_id):
        raise HelperError("shop_id 格式错误")
    return shop_id in _read_bound_shops(app_root)


def bind_shop(
    shop_id: str,
    *,
    app_root: Any = None,
    ensure: Any = None,
    device_token: str | None = None,
) -> None:
    if not JOB_ID_PATTERN.fullmatch(shop_id):
        raise HelperError("shop_id 格式错误")
    root = app_root or application_root()
    profile = shop_profile(root, shop_id)
    (ensure or ensure_chrome)(profile)
    if device_token is not None and not _valid_device_token(device_token):
        raise HelperError("设备凭据格式错误")
    bindings = _read_bindings(root)
    bindings[shop_id] = device_token if device_token is not None else bindings.get(shop_id)
    _write_bindings(root, bindings)


def unbind_shop(shop_id: str, *, app_root: Any = None) -> None:
    if not JOB_ID_PATTERN.fullmatch(shop_id):
        raise HelperError("shop_id 格式错误")
    root = app_root or application_root()
    bindings = _read_bindings(root)
    bindings.pop(shop_id, None)
    _write_bindings(root, bindings)


def bound_device_tokens(app_root: Any) -> list[str]:
    return sorted({token for token in _read_bindings(app_root).values() if token})


def _valid_device_token(value: str) -> bool:
    return 16 <= len(value) <= 2_048 and not any(character.isspace() or not 33 <= ord(character) <= 126 for character in value)


def _new_device_token() -> str:
    token = secrets.token_urlsafe(32)
    if not _valid_device_token(token):
        raise HelperError("无法生成设备凭据")
    return token


def claim_binding(
    request: BindingRequest,
    *,
    device_token: str | None = None,
    open_url=open_no_redirect,
) -> Mapping[str, Any]:
    """Consume a one-time website ticket and return its safe, flat payload."""
    body = b""
    if request.action == "bind":
        if not isinstance(device_token, str) or not _valid_device_token(device_token):
            raise HelperError("设备凭据格式错误")
        body = json.dumps({"device_token": device_token}, separators=(",", ":")).encode("utf-8")
    elif device_token is not None:
        raise HelperError("解绑请求不应包含设备凭据")
    api_origin = _configured_api_origin()
    claim_request = Request(
        f"{api_origin}/api/automation/shop-bindings/claim",
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"BindingTicket {request.ticket}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "EbaydaHelper/0.3",
        },
        method="POST",
    )
    try:
        with open_url(claim_request, timeout=BINDING_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise HelperError(f"领取店铺绑定票据失败：HTTP {response.status}")
            body = response.read(MAX_BINDING_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise HelperError(f"领取店铺绑定票据失败：HTTP {error.code}") from None
    except URLError as error:
        message = "领取店铺绑定票据失败：请求超时" if isinstance(
            error.reason, TimeoutError
        ) else "领取店铺绑定票据失败：网络错误"
        raise HelperError(message) from None
    except TimeoutError:
        raise HelperError("领取店铺绑定票据失败：请求超时") from None
    except OSError:
        raise HelperError("领取店铺绑定票据失败：网络错误") from None

    if len(body) > MAX_BINDING_RESPONSE_BYTES:
        raise HelperError("领取店铺绑定票据失败：响应过大")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HelperError("领取店铺绑定票据失败：响应格式错误") from None
    if not isinstance(payload, Mapping):
        raise HelperError("领取店铺绑定票据失败：响应格式错误")
    action = str(payload.get("action") or "").casefold()
    shop_id = str(payload.get("shop_id") or "")
    device_id = str(payload.get("device_id") or "")
    status = str(payload.get("status") or "")
    if action != request.action:
        raise HelperError("领取店铺绑定票据失败：动作不匹配")
    if not JOB_ID_PATTERN.fullmatch(shop_id):
        raise HelperError("领取店铺绑定票据失败：shop_id 格式错误")
    if not device_id or len(device_id) > 255 or any(
        character in device_id for character in "\r\n\x00"
    ):
        raise HelperError("领取店铺绑定票据失败：设备标识错误")
    expected_status = "bound" if action == "bind" else "unbound"
    if status != expected_status:
        raise HelperError("领取店铺绑定票据失败：状态不匹配")
    legacy_device_token = payload.get("device_token")
    if legacy_device_token is not None and (
        not isinstance(legacy_device_token, str) or not _valid_device_token(legacy_device_token)
    ):
        raise HelperError("领取店铺绑定票据失败：设备凭据错误")
    return payload


def _localize_claimed_job(job: ClaimedJob, api_origin: str) -> ClaimedJob:
    if api_origin == API_ORIGIN:
        return job

    origin = urlparse(api_origin)

    def localize(url: str) -> str:
        resource = urlparse(url)
        return urlunparse(
            (
                origin.scheme,
                origin.netloc,
                resource.path,
                "",
                resource.query,
                "",
            )
        )

    return replace(
        job,
        product_json_url=localize(job.product_json_url),
        images_zip_url=localize(job.images_zip_url),
        event_url=localize(job.event_url),
    )


def claim_job(request: LaunchRequest, open_url=open_no_redirect) -> Mapping[str, Any]:
    api_origin = _configured_api_origin()
    claim_request = Request(
        f"{api_origin}/api/automation/jobs/{quote(request.job_id, safe='')}/claim",
        data=b"",
        headers={
            "Accept": "application/json",
            "Authorization": f"LaunchTicket {request.ticket}",
            "User-Agent": "EbaydaHelper/0.1",
        },
        method="POST",
    )
    try:
        with open_url(claim_request, timeout=CLAIM_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise HelperError(f"领取任务失败：HTTP {response.status}")
            body = response.read(MAX_CLAIM_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise HelperError(f"领取任务失败：HTTP {error.code}") from None
    except URLError as error:
        message = "领取任务失败：请求超时" if isinstance(
            error.reason, TimeoutError
        ) else "领取任务失败：网络错误"
        raise HelperError(message) from None
    except TimeoutError:
        raise HelperError("领取任务失败：请求超时") from None
    except OSError:
        raise HelperError("领取任务失败：网络错误") from None

    if len(body) > MAX_CLAIM_RESPONSE_BYTES:
        raise HelperError("领取任务失败：响应过大")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HelperError("领取任务失败：响应格式错误") from None
    if not isinstance(payload, Mapping):
        raise HelperError("领取任务失败：响应格式错误")
    if payload.get("job_id") != request.job_id:
        raise HelperError("领取任务失败：job_id 不匹配")
    if payload.get("action") != "save_draft":
        raise HelperError("领取任务失败：任务动作错误")
    shop_id = str(payload.get("shop_id") or "")
    if not JOB_ID_PATTERN.fullmatch(shop_id):
        raise HelperError("领取任务失败：shop_id 格式错误")
    try:
        ClaimedJob.from_payload(payload)
    except TaskExecutionError as error:
        raise HelperError(str(error)) from None
    return payload


def claim_next(device_token: str, *, open_url=open_no_redirect) -> DevicePoll:
    if not _valid_device_token(device_token):
        raise HelperError("设备凭据格式错误")
    api_origin = _configured_api_origin()
    request = Request(
        f"{api_origin}/api/automation/devices/next",
        data=b"",
        headers={
            "Accept": "application/json",
            "Authorization": f"DeviceToken {device_token}",
            "User-Agent": "EbaydaHelper/0.3",
        },
        method="POST",
    )
    try:
        with open_url(request, timeout=DEVICE_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise HelperError(f"领取批次任务失败：HTTP {response.status}")
            body = response.read(MAX_DEVICE_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise HelperError(f"领取批次任务失败：HTTP {error.code}") from None
    except URLError as error:
        message = "领取批次任务失败：请求超时" if isinstance(error.reason, TimeoutError) else "领取批次任务失败：网络错误"
        raise HelperError(message) from None
    except TimeoutError:
        raise HelperError("领取批次任务失败：请求超时") from None
    except OSError:
        raise HelperError("领取批次任务失败：网络错误") from None
    if len(body) > MAX_DEVICE_RESPONSE_BYTES:
        raise HelperError("领取批次任务失败：响应过大")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HelperError("领取批次任务失败：响应格式错误") from None
    if not isinstance(payload, Mapping):
        raise HelperError("领取批次任务失败：响应格式错误")
    commands = payload.get("commands")
    if not isinstance(commands, list):
        raise HelperError("领取批次任务失败：命令格式错误")
    termination_item_ids: list[str] = []
    for command in commands:
        if not isinstance(command, Mapping) or command.get("type") != "terminate":
            raise HelperError("领取批次任务失败：命令格式错误")
        item_id = str(command.get("item_id") or "")
        if not JOB_ID_PATTERN.fullmatch(item_id):
            raise HelperError("领取批次任务失败：命令格式错误")
        if item_id not in termination_item_ids:
            termination_item_ids.append(item_id)
    item_payload = payload.get("item")
    if item_payload is not None:
        if not isinstance(item_payload, Mapping):
            raise HelperError("领取批次任务失败：任务格式错误")
        try:
            ClaimedJob.from_payload(item_payload)
        except TaskExecutionError as error:
            raise HelperError(str(error)) from None
    return DevicePoll(tuple(termination_item_ids), item_payload)


def acknowledge_termination(
    item_id: str,
    device_token: str,
    *,
    open_url=open_no_redirect,
) -> None:
    if not JOB_ID_PATTERN.fullmatch(item_id):
        raise HelperError("终止确认失败：执行项 ID 格式错误")
    if not _valid_device_token(device_token):
        raise HelperError("设备凭据格式错误")
    request = Request(
        f"{_configured_api_origin()}/api/automation/devices/termination-ack",
        data=json.dumps({"item_id": item_id}, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"DeviceToken {device_token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "EbaydaHelper/0.3",
        },
        method="POST",
    )
    try:
        with open_url(request, timeout=DEVICE_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise HelperError(f"终止确认失败：HTTP {response.status}")
            body = response.read(MAX_DEVICE_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise HelperError(f"终止确认失败：HTTP {error.code}") from None
    except URLError as error:
        message = "终止确认失败：请求超时" if isinstance(error.reason, TimeoutError) else "终止确认失败：网络错误"
        raise HelperError(message) from None
    except TimeoutError:
        raise HelperError("终止确认失败：请求超时") from None
    except OSError:
        raise HelperError("终止确认失败：网络错误") from None
    if len(body) > MAX_DEVICE_RESPONSE_BYTES:
        raise HelperError("终止确认失败：响应过大")


def report_final_event(
    item_id: str,
    device_token: str,
    status: str,
    *,
    message: str | None = None,
    open_url=open_no_redirect,
) -> bool:
    if not JOB_ID_PATTERN.fullmatch(item_id):
        raise HelperError("终态回传失败：执行项 ID 格式错误")
    if not _valid_device_token(device_token):
        raise HelperError("设备凭据格式错误")
    if status not in FINAL_EVENT_STATUSES:
        raise HelperError("终态回传失败：状态无效")
    event: dict[str, str] = {"status": status}
    if message:
        event["message"] = _safe_event_message(message)
    request = Request(
        f"{_configured_api_origin()}/api/automation/devices/batch-items/{quote(item_id, safe='')}/final-event",
        data=json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"DeviceToken {device_token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "EbaydaHelper/0.3",
        },
        method="POST",
    )
    try:
        with open_url(request, timeout=DEVICE_TIMEOUT_SECONDS) as response:
            if response.status == 409:
                return False
            if response.status != 200:
                raise HelperError(f"终态回传失败：HTTP {response.status}")
            body = response.read(MAX_DEVICE_RESPONSE_BYTES + 1)
    except HTTPError as error:
        if error.code == 409:
            return False
        raise HelperError(f"终态回传失败：HTTP {error.code}") from None
    except URLError as error:
        message = "终态回传失败：请求超时" if isinstance(error.reason, TimeoutError) else "终态回传失败：网络错误"
        raise HelperError(message) from None
    except TimeoutError:
        raise HelperError("终态回传失败：请求超时") from None
    except OSError:
        raise HelperError("终态回传失败：网络错误") from None
    if len(body) > MAX_DEVICE_RESPONSE_BYTES:
        raise HelperError("终态回传失败：响应过大")
    return True


def post_event(
    job: ClaimedJob,
    status: str,
    *,
    message: str | None = None,
    open_url=open_no_redirect,
) -> None:
    if status not in {
        "preparing",
        "running",
        "draft_saved",
        "paused_for_user",
        "needs_confirmation",
        "failed",
        "terminated_unknown",
    }:
        raise HelperError("不支持的任务状态")
    event: dict[str, str] = {"status": status}
    if message:
        event["message"] = _safe_event_message(message)
    body = json.dumps(
        event,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        job.event_url,
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"JobToken {job.job_token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "EbaydaHelper/0.2",
        },
        method="POST",
    )
    try:
        with open_url(request, timeout=EVENT_TIMEOUT_SECONDS) as response:
            if not 200 <= getattr(response, "status", 0) < 300:
                raise HelperError(f"回传任务状态失败：HTTP {response.status}")
            response.read(4_097)
    except HTTPError as error:
        raise HelperError(f"回传任务状态失败：HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise HelperError("回传任务状态失败：网络错误") from None


def execute_claimed_job(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    try:
        job = ClaimedJob.from_payload(payload)
    except TaskExecutionError as error:
        raise HelperError(str(error)) from None

    app_root = application_root()
    try:
        job = _localize_claimed_job(job, _configured_api_origin())
        with shop_execution_lock(app_root, job.shop_id):
            post_event(job, "preparing")
            files = prepare_job_files(job, app_root)
            profile = shop_profile(app_root, job.shop_id)
            port = ensure_chrome(profile)
            post_event(job, "running")
            automation = run_automation(files, port)
            exit_code = getattr(automation, "exit_code", automation)
            automation_message = getattr(automation, "message", None)
            automation_status = getattr(automation, "status", None)
    except (HelperError, TaskExecutionError) as error:
        _post_final_event(job, "failed", app_root=app_root)
        raise HelperError(str(error)) from None
    except Exception:
        _post_final_event(job, "failed", app_root=app_root)
        raise HelperError("本地自动化执行失败") from None
    finally:
        try:
            cleanup_task_files(app_root, job.job_id)
        except TaskExecutionError as error:
            print(json.dumps({"status": "warning", "error": str(error)}, ensure_ascii=False), file=sys.stderr)

    if exit_code == 0:
        status = "draft_saved"
    elif exit_code == 3 and automation_status == "not_saved":
        status = "needs_confirmation"
    elif exit_code in {2, 3, 4, 130}:
        status = "paused_for_user"
    else:
        status = "failed"
    _post_final_event(job, status, automation_message, app_root=app_root)
    return status, job.job_id, job.shop_id


def _post_final_event(
    job: ClaimedJob,
    status: str,
    message: str | None = None,
    *,
    app_root: Path | None = None,
) -> None:
    try:
        if message:
            post_event(job, status, message=message)
        else:
            post_event(job, status)
    except HelperError:
        try:
            _queue_pending_final_event(app_root or application_root(), job, status, message)
        except HelperError:
            pass


def _safe_event_message(value: object) -> str:
    message = " ".join(str(value).split())
    message = re.sub(
        r"(?i)\b(ticket|job[_-]?token|jwt|password|authorization)\b"
        r"(\s*[:=]\s*)(?:(?:bearer|jobtoken|launchticket)\s+)?"
        r"[A-Za-z0-9._~+/=-]+",
        r"\1=[已打码]",
        message,
    )
    return message[:500]


def _helper_command(argument: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, argument]
    return [sys.executable, str(Path(__file__).resolve()), argument]


def _spawn_claimed_job(payload: Mapping[str, Any]) -> Any:
    try:
        process = subprocess.Popen(
            _helper_command("--run-claimed-job"),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if process.stdin is None:
            raise OSError("missing child stdin")
        process.stdin.write(json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"))
        process.stdin.close()
        return process
    except (OSError, TypeError, ValueError):
        raise HelperError("无法启动批次自动化执行进程") from None


def _run_claimed_job_child() -> int:
    try:
        body = sys.stdin.buffer.read(MAX_CLAIM_RESPONSE_BYTES + 1)
        if len(body) > MAX_CLAIM_RESPONSE_BYTES:
            raise HelperError("任务数据过大")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise HelperError("任务数据格式错误")
        status, job_id, shop_id = execute_claimed_job(payload)
    except (HelperError, UnicodeDecodeError, json.JSONDecodeError):
        return 3
    print(json.dumps({"status": status, "job_id": job_id, "shop_id": shop_id}, ensure_ascii=False), flush=True)
    if status == "draft_saved":
        return 0
    if status == "paused_for_user":
        return 2
    return 3


class ResidentRunner:
    def __init__(
        self,
        root: Path,
        *,
        claim_next_request: Any = claim_next,
        acknowledge_termination_request: Any = acknowledge_termination,
        report_final_event_request: Any = report_final_event,
        spawn: Any = _spawn_claimed_job,
        sleep: Any = time.sleep,
    ) -> None:
        self.root = root
        self._claim_next_request = claim_next_request
        self._acknowledge_termination_request = acknowledge_termination_request
        self._report_final_event_request = report_final_event_request
        self._spawn = spawn
        self._sleep = sleep
        self._active: dict[str, ActiveExecution] = {}

    def run_forever(self, should_stop: Any = lambda: False) -> None:
        while not should_stop():
            self.run_once()
            self._sleep(RESIDENT_POLL_SECONDS)

    def run_once(self) -> None:
        self._reap_finished()
        tokens = bound_device_tokens(self.root)
        if not tokens:
            return
        poll: DevicePoll | None = None
        device_token = ""
        for token in tokens:
            self._flush_pending_final_events(token)
            self._reconcile_orphaned_executions(token)
            try:
                poll = self._claim_next_request(token)
                device_token = token
                break
            except HelperError:
                continue
        if poll is None:
            return
        for item_id in poll.termination_item_ids:
            execution = self._active.get(item_id)
            if execution is not None:
                self._terminate_execution(item_id, execution, device_token)
                continue
            marker_found, pid = _execution_marker_pid(self.root, item_id)
            if marker_found and (pid is None or _process_is_alive(pid)):
                continue
            if marker_found:
                _remove_execution_marker(self.root, item_id)
            try:
                self._acknowledge_termination_request(item_id, device_token)
            except HelperError:
                continue
        if poll.item_payload is not None:
            self._start_item(poll.item_payload)

    def _start_item(self, payload: Mapping[str, Any]) -> None:
        try:
            job = ClaimedJob.from_payload(payload)
        except TaskExecutionError:
            return
        try:
            job = _localize_claimed_job(job, _configured_api_origin())
        except HelperError:
            _post_final_event(job, "failed", "批次任务本地配置无效", app_root=self.root)
            return
        if job.job_id in self._active or any(active.job.shop_id == job.shop_id for active in self._active.values()):
            _post_final_event(job, "failed", "同店铺已有正在执行的批次任务", app_root=self.root)
            return
        try:
            process = self._spawn(payload)
        except HelperError:
            _post_final_event(job, "failed", "无法启动批次自动化执行进程", app_root=self.root)
            return
        try:
            _write_execution_marker(self.root, job.job_id, process)
        except HelperError:
            if self._stop_process(process):
                _post_final_event(job, "terminated_unknown", "无法保存执行进程状态，结果未知", app_root=self.root)
            else:
                self._active[job.job_id] = ActiveExecution(job, process)
            return
        self._active[job.job_id] = ActiveExecution(job, process)

    def _reconcile_orphaned_executions(self, device_token: str) -> None:
        directory = self.root / "active-executions"
        if not directory.is_dir() or directory.is_symlink():
            return
        for marker in directory.glob("*.json"):
            if marker.is_symlink():
                continue
            item_id = marker.stem
            if item_id in self._active or not item_id.startswith("item_") or not JOB_ID_PATTERN.fullmatch(item_id):
                continue
            marker_found, pid, started_uptime_ms = _execution_marker_state(self.root, item_id)
            if not marker_found or _marker_process_is_alive(pid, started_uptime_ms):
                continue
            try:
                self._report_final_event_request(
                    item_id,
                    device_token,
                    "terminated_unknown",
                    message="助手重启后发现自动化进程已结束，结果未知",
                )
            except HelperError:
                continue
            _remove_execution_marker(self.root, item_id)

    def _flush_pending_final_events(self, device_token: str) -> None:
        for event in _read_pending_final_events(self.root):
            try:
                accepted = self._report_final_event_request(
                    event.item_id,
                    device_token,
                    event.status,
                    message=event.message or None,
                )
            except HelperError:
                continue
            if accepted:
                _remove_pending_final_event(self.root, event.item_id)

    def _reap_finished(self) -> None:
        for item_id, execution in list(self._active.items()):
            if execution.process.poll() is None or execution.termination_requested:
                continue
            self._cleanup_execution(item_id)

    def _stop_process(self, process: Any) -> bool:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return process.poll() is not None

    def _terminate_execution(self, item_id: str, execution: ActiveExecution, device_token: str) -> None:
        execution.termination_requested = True
        process = execution.process
        if not self._stop_process(process):
            return
        try:
            self._acknowledge_termination_request(item_id, device_token)
        except HelperError:
            return
        self._cleanup_execution(item_id)

    def _cleanup_execution(self, item_id: str) -> None:
        execution = self._active.pop(item_id, None)
        if execution is None:
            return
        _remove_execution_marker(self.root, item_id)
        try:
            cleanup_task_files(self.root, execution.job.job_id)
        except TaskExecutionError:
            pass


def _run_resident() -> int:
    root = application_root()
    runner = ResidentRunner(root)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        return 0
    return 0


def _start_resident_after_binding() -> None:
    if sys.platform != "win32":
        return
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            _helper_command("--resident"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
    except OSError:
        pass


def _run_binding(request: BindingRequest) -> int:
    root = application_root()
    device_token = _new_device_token() if request.action == "bind" else None
    payload = claim_binding(request, device_token=device_token)
    shop_id = str(payload["shop_id"])
    if request.action == "bind":
        bind_shop(shop_id, app_root=root, device_token=str(payload.get("device_token") or device_token))
        status = "binding_started"
    else:
        unbind_shop(shop_id, app_root=root)
        status = "unbound"
    print(
        json.dumps(
            {"status": status, "shop_id": shop_id}, ensure_ascii=False
        ),
        flush=True,
    )
    return 0


def _run_launch(launch: LaunchRequest) -> int:
    try:
        cleanup_task_files(application_root(), launch.job_id)
        payload = claim_job(launch)
    except HelperError as error:
        print(
            json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )
        return 2

    try:
        status, job_id, shop_id = execute_claimed_job(payload)
    except HelperError as error:
        print(
            json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )
        return 3

    print(
        json.dumps(
            {"status": status, "job_id": job_id, "shop_id": shop_id},
            ensure_ascii=False,
        ),
        flush=True,
    )
    if status == "draft_saved":
        return 0
    if status == "paused_for_user":
        return 2
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ebayda 得物自动上架本地助手")
    parser.add_argument("launch_url", nargs="?", help="网站生成的 ebayda:// 启动地址")
    parser.add_argument("--resident", action="store_true", help="以常驻模式领取批次任务")
    parser.add_argument("--run-claimed-job", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        if args.run_claimed_job:
            if args.resident or args.launch_url:
                raise HelperError("启动参数冲突")
            return _run_claimed_job_child()
        if args.resident:
            if args.launch_url:
                raise HelperError("启动参数冲突")
            with resident_lock(application_root()):
                return _run_resident()
        if not args.launch_url:
            raise HelperError("启动地址为空")
        parsed = urlparse(args.launch_url)
        if parsed.scheme.casefold() == "ebayda" and parsed.netloc.casefold() == "run":
            launch = parse_launch_url(args.launch_url)
            with instance_lock(application_root()):
                return _run_launch(launch)
        binding = parse_binding_url(args.launch_url)
        status = _run_binding(binding)
        if binding.action == "bind" and status == 0:
            _start_resident_after_binding()
        return status
    except ValueError:
        error = HelperError("启动地址格式错误")
        print(
            json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )
        return 2
    except (HelperError, TaskExecutionError) as error:
        print(
            json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
