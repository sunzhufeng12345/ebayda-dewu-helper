from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


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
