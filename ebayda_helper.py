from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from helper_runtime import ClaimedJob, TaskExecutionError


API_ORIGIN = "https://www.ebayda.com"
CLAIM_TIMEOUT_SECONDS = 10
MAX_CLAIM_RESPONSE_BYTES = 1024 * 1024
MAX_LAUNCH_URL_LENGTH = 4_096
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class HelperError(RuntimeError):
    """The helper cannot safely accept or claim a launch request."""


@dataclass(frozen=True)
class LaunchRequest:
    job_id: str
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


def claim_job(request: LaunchRequest, open_url=urlopen) -> Mapping[str, Any]:
    claim_request = Request(
        f"{API_ORIGIN}/api/automation/jobs/{quote(request.job_id, safe='')}/claim",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ebayda 得物自动上架本地助手")
    parser.add_argument("launch_url", help="网站生成的 ebayda:// 启动地址")
    args = parser.parse_args(argv)

    try:
        launch = parse_launch_url(args.launch_url)
        payload = claim_job(launch)
        print(
            json.dumps(
                {
                    "status": "claimed",
                    "job_id": launch.job_id,
                    "shop_id": str(payload["shop_id"]),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    except HelperError as error:
        print(
            json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
