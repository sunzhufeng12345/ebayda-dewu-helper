from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse, urlunparse
from urllib.request import Request

from helper_runtime import (
    ClaimedJob,
    TaskExecutionError,
    application_root,
    cleanup_stale_task_files,
    cleanup_task_files,
    ensure_chrome,
    instance_lock,
    open_no_redirect,
    prepare_job_files,
    run_automation,
    shop_profile,
)


API_ORIGIN = "http://101.34.90.101:10112"
LEGACY_API_ORIGIN = "https://www.ebayda.com"
CLAIM_TIMEOUT_SECONDS = 10
EVENT_TIMEOUT_SECONDS = 10
BINDING_TIMEOUT_SECONDS = 10
MAX_CLAIM_RESPONSE_BYTES = 1024 * 1024
MAX_BINDING_RESPONSE_BYTES = 64 * 1024
MAX_LAUNCH_URL_LENGTH = 4_096
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class HelperError(RuntimeError):
    """The helper cannot safely accept or claim a launch request."""


def _configured_api_origin() -> str:
    origin = os.environ.get("EBAYDA_API_ORIGIN", API_ORIGIN).rstrip("/")
    if origin in {API_ORIGIN, LEGACY_API_ORIGIN}:
        # The Tencent Cloud IP is the current deployment; the HTTPS domain
        # remains accepted for older installations during DNS migration.
        return origin

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
    if not JOB_ID_PATTERN.fullmatch(job_id):
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


def _read_bound_shops(app_root: Any) -> set[str]:
    path = _bindings_path(app_root)
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HelperError("本地店铺绑定配置无法读取") from error
    shops = payload.get("shops") if isinstance(payload, Mapping) else None
    if not isinstance(shops, list) or any(
        not isinstance(shop_id, str) or not JOB_ID_PATTERN.fullmatch(shop_id)
        for shop_id in shops
    ):
        raise HelperError("本地店铺绑定配置格式错误")
    return set(shops)


def _write_bound_shops(app_root: Any, shops: set[str]) -> None:
    app_root.mkdir(parents=True, exist_ok=True)
    path = _bindings_path(app_root)
    temporary = path.with_name(f"{path.name}.part")
    try:
        temporary.write_text(
            json.dumps({"shops": sorted(shops)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise HelperError("本地店铺绑定配置无法保存") from error


def is_shop_bound(app_root: Any, shop_id: str) -> bool:
    if not JOB_ID_PATTERN.fullmatch(shop_id):
        raise HelperError("shop_id 格式错误")
    return shop_id in _read_bound_shops(app_root)


def bind_shop(
    shop_id: str,
    *,
    app_root: Any = None,
    ensure: Any = None,
) -> None:
    if not JOB_ID_PATTERN.fullmatch(shop_id):
        raise HelperError("shop_id 格式错误")
    root = app_root or application_root()
    profile = shop_profile(root, shop_id)
    (ensure or ensure_chrome)(profile)
    shops = _read_bound_shops(root)
    shops.add(shop_id)
    _write_bound_shops(root, shops)


def unbind_shop(shop_id: str, *, app_root: Any = None) -> None:
    if not JOB_ID_PATTERN.fullmatch(shop_id):
        raise HelperError("shop_id 格式错误")
    root = app_root or application_root()
    shops = _read_bound_shops(root)
    shops.discard(shop_id)
    _write_bound_shops(root, shops)


def claim_binding(
    request: BindingRequest,
    *,
    open_url=open_no_redirect,
) -> Mapping[str, Any]:
    """Consume a one-time website ticket and return its safe, flat payload."""
    api_origin = _configured_api_origin()
    claim_request = Request(
        f"{api_origin}/api/automation/shop-bindings/claim",
        data=b"",
        headers={
            "Accept": "application/json",
            "Authorization": f"BindingTicket {request.ticket}",
            "User-Agent": "EbaydaHelper/0.2",
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


def post_event(
    job: ClaimedJob,
    status: str,
    *,
    open_url=open_no_redirect,
) -> None:
    api_origin = _configured_api_origin()
    if status not in {
        "preparing",
        "running",
        "draft_saved",
        "paused_for_user",
        "failed",
    }:
        raise HelperError("不支持的任务状态")
    body = json.dumps(
        {"status": status},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        f"{api_origin}/api/automation/jobs/{quote(job.job_id, safe='')}/events",
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
        post_event(job, "preparing")
        files = prepare_job_files(job, app_root)
        profile = shop_profile(app_root, job.shop_id)
        port = ensure_chrome(profile)
        post_event(job, "running")
        exit_code = run_automation(files, port)
    except (HelperError, TaskExecutionError) as error:
        _post_final_event(job, "failed")
        raise HelperError(str(error)) from None
    except Exception:
        _post_final_event(job, "failed")
        raise HelperError("本地自动化执行失败") from None
    finally:
        try:
            cleanup_task_files(app_root, job.job_id)
        except TaskExecutionError as error:
            print(json.dumps({"status": "warning", "error": str(error)}, ensure_ascii=False), file=sys.stderr)

    if exit_code == 0:
        status = "draft_saved"
    elif exit_code in {2, 130}:
        status = "paused_for_user"
    else:
        status = "failed"
    _post_final_event(job, status)
    return status, job.job_id, job.shop_id


def _post_final_event(job: ClaimedJob, status: str) -> None:
    try:
        post_event(job, status)
    except HelperError:
        pass


def _run_binding(request: BindingRequest) -> int:
    root = application_root()
    cleanup_stale_task_files(root)
    payload = claim_binding(request)
    shop_id = str(payload["shop_id"])
    if request.action == "bind":
        bind_shop(shop_id, app_root=root)
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
        cleanup_stale_task_files(application_root())
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
    parser.add_argument("launch_url", help="网站生成的 ebayda:// 启动地址")
    args = parser.parse_args(argv)

    try:
        parsed = urlparse(args.launch_url)
        if parsed.scheme.casefold() == "ebayda" and parsed.netloc.casefold() == "run":
            launch = parse_launch_url(args.launch_url)
            with instance_lock(application_root()):
                return _run_launch(launch)
        binding = parse_binding_url(args.launch_url)
        with instance_lock(application_root()):
            return _run_binding(binding)
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
