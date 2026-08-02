# Ebayda 本地助手协议唤起 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个可由 `ebayda://run?...` 唤起的 Windows 本地助手第一阶段，实现严格解析深链接、通过 HTTPS 一次性领取任务，以及生成注册自定义协议的安装包配置。

**Architecture:** 新增单文件 `ebayda_helper.py`，使用 Python 标准库完成协议解析、HTTPS 请求和命令行入口，不改动当前 `main.py` 自动化核心。网站后端返回扁平任务契约，助手只确认任务已安全领取；JSON/ZIP 下载、店铺 Profile 和 DrissionPage 执行在后端契约联调后作为下一阶段接入。Windows 使用 PyInstaller 生成单文件 EXE，再由 Inno Setup 以当前用户权限注册 `ebayda://`。

**Tech Stack:** Python 3.9 标准库、`unittest`、PyInstaller、Inno Setup 6、Windows HKCU 自定义 URL 协议。

---

### Task 1: Parse and validate the custom-protocol URL

**Files:**
- Create: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_ebayda_helper.py`
- Create: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/ebayda_helper.py`
- Test: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_ebayda_helper.py`

- [ ] **Step 1: Write the failing protocol tests**

```python
from __future__ import annotations

import unittest

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
```

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m unittest -v test_ebayda_helper.LaunchUrlTests
```

Expected: import failure because `ebayda_helper.py` does not exist.

- [ ] **Step 3: Implement the minimal parser**

Create `ebayda_helper.py` with these definitions:

```python
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

    parsed = urlparse(value)
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
```

- [ ] **Step 4: Run the parser tests and verify they pass**

Run:

```bash
.venv/bin/python -m unittest -v test_ebayda_helper.LaunchUrlTests
```

Expected: all `LaunchUrlTests` pass.

- [ ] **Step 5: Commit the protocol parser**

```bash
git add ebayda_helper.py test_ebayda_helper.py
git commit -m "feat: 增加本地助手协议解析"
```

### Task 2: Claim a job through the trusted HTTPS endpoint

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/ebayda_helper.py`
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_ebayda_helper.py`
- Test: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_ebayda_helper.py`

- [ ] **Step 1: Add failing HTTPS claim tests**

Append imports and test doubles:

```python
import json
from urllib.error import HTTPError, URLError


class _FakeResponse:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self.body
```

Add the tests:

```python
class ClaimJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.launch = ebayda_helper.LaunchRequest(
            job_id="job_abc-123",
            ticket="abcdefghijklmnop",
        )

    def test_claim_posts_ticket_in_authorization_header(self) -> None:
        captured: list[object] = []

        def open_url(request: object, *, timeout: float) -> _FakeResponse:
            captured.append((request, timeout))
            return _FakeResponse(
                {"job_id": "job_abc-123", "shop_id": "101", "action": "save_draft"}
            )

        payload = ebayda_helper.claim_job(self.launch, open_url=open_url)

        request, timeout = captured[0]
        self.assertEqual(
            request.full_url,
            "https://www.ebayda.com/api/automation/jobs/job_abc-123/claim",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "LaunchTicket abcdefghijklmnop")
        self.assertEqual(timeout, 10)
        self.assertEqual(payload["shop_id"], "101")

    def test_response_must_match_job_and_supported_action(self) -> None:
        invalid_payloads = (
            {"job_id": "other", "shop_id": "101", "action": "save_draft"},
            {"job_id": "job_abc-123", "shop_id": "101", "action": "submit"},
            ["not", "an", "object"],
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ebayda_helper.HelperError):
                ebayda_helper.claim_job(
                    self.launch,
                    open_url=lambda *_args, **_kwargs: _FakeResponse(payload),
                )

    def test_network_errors_do_not_expose_ticket(self) -> None:
        failures = (
            HTTPError("https://www.ebayda.com", 401, "Unauthorized", {}, None),
            URLError("offline"),
            TimeoutError("slow"),
        )

        for failure in failures:
            def open_url(*_args: object, **_kwargs: object) -> object:
                raise failure

            with self.subTest(failure=failure), self.assertRaises(ebayda_helper.HelperError) as caught:
                ebayda_helper.claim_job(self.launch, open_url=open_url)
            self.assertNotIn(self.launch.ticket, str(caught.exception))
```

- [ ] **Step 2: Run the claim tests and verify they fail because `claim_job` is missing**

Run:

```bash
.venv/bin/python -m unittest -v test_ebayda_helper.ClaimJobTests
```

Expected: `AttributeError` for `claim_job`.

- [ ] **Step 3: Implement the standard-library HTTPS client**

Add these imports and definitions to `ebayda_helper.py`:

```python
import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_ORIGIN = "https://www.ebayda.com"
CLAIM_TIMEOUT_SECONDS = 10
MAX_CLAIM_RESPONSE_BYTES = 1_048_576


def claim_job(
    launch: LaunchRequest,
    *,
    open_url: Callable[..., Any] = urlopen,
) -> Mapping[str, Any]:
    url = (
        f"{API_ORIGIN}/api/automation/jobs/"
        f"{quote(launch.job_id, safe='')}/claim"
    )
    request = Request(
        url,
        data=b"",
        headers={
            "Accept": "application/json",
            "Authorization": f"LaunchTicket {launch.ticket}",
            "User-Agent": "EbaydaHelper/0.1",
        },
        method="POST",
    )
    try:
        with open_url(request, timeout=CLAIM_TIMEOUT_SECONDS) as response:
            if getattr(response, "status", 200) != 200:
                raise HelperError(f"领取任务失败：HTTP {response.status}")
            body = response.read(MAX_CLAIM_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise HelperError(f"领取任务失败：HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise HelperError("领取任务失败：网络不可用或请求超时") from error

    if len(body) > MAX_CLAIM_RESPONSE_BYTES:
        raise HelperError("领取任务失败：响应过大")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HelperError("领取任务失败：响应不是有效 JSON") from error
    if not isinstance(payload, Mapping):
        raise HelperError("领取任务失败：响应必须是对象")
    if payload.get("job_id") != launch.job_id:
        raise HelperError("领取任务失败：任务编号不匹配")
    if payload.get("action") != "save_draft":
        raise HelperError("领取任务失败：不支持的任务类型")
    shop_id = str(payload.get("shop_id") or "")
    if not JOB_ID_PATTERN.fullmatch(shop_id):
        raise HelperError("领取任务失败：店铺编号格式错误")
    return payload
```

- [ ] **Step 4: Run the claim tests and the existing suite**

Run:

```bash
.venv/bin/python -m unittest -v test_ebayda_helper.ClaimJobTests
.venv/bin/python -m unittest -v
```

Expected: the new tests and all existing tests pass.

- [ ] **Step 5: Commit the claim client**

```bash
git add ebayda_helper.py test_ebayda_helper.py
git commit -m "feat: 增加本地助手任务领取"
```

### Task 3: Add the executable entry point without leaking tickets

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/ebayda_helper.py`
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_ebayda_helper.py`
- Test: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_ebayda_helper.py`

- [ ] **Step 1: Add failing command-line tests**

Add these imports:

```python
import io
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
```

Add the tests:

```python
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
```

- [ ] **Step 2: Run the command-line tests and verify `main` is missing**

Run:

```bash
.venv/bin/python -m unittest -v test_ebayda_helper.CommandLineTests
```

Expected: `AttributeError` for `main`.

- [ ] **Step 3: Implement the CLI**

Add `argparse` and `sys` imports, then append:

```python
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
```

Do not log `args.launch_url`, `LaunchRequest.ticket`, request headers, or the response body.

- [ ] **Step 4: Run the focused and full test suites**

Run:

```bash
.venv/bin/python -m unittest -v test_ebayda_helper.CommandLineTests
.venv/bin/python -m unittest -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the executable entry point**

```bash
git add ebayda_helper.py test_ebayda_helper.py
git commit -m "feat: 增加本地助手命令行入口"
```

### Task 4: Package and register `ebayda://` on Windows

**Files:**
- Create: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/installer/EbaydaHelper.iss`
- Create: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/installer/build-helper.ps1`
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_ebayda_helper.py`
- Test: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_ebayda_helper.py`

- [ ] **Step 1: Add a failing installer-contract test**

```python
from pathlib import Path


class InstallerContractTests(unittest.TestCase):
    def test_inno_setup_registers_current_user_protocol(self) -> None:
        script = (
            Path(__file__).with_name("installer") / "EbaydaHelper.iss"
        ).read_text(encoding="utf-8")

        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("Software\\Classes\\ebayda", script)
        self.assertIn('ValueName: "URL Protocol"', script)
        self.assertIn('ValueData: """{app}\\EbaydaHelper.exe"" ""%1"""', script)
        self.assertIn("Flags: uninsdeletekey", script)
```

- [ ] **Step 2: Run the installer test and verify it fails because the script is absent**

Run:

```bash
.venv/bin/python -m unittest -v test_ebayda_helper.InstallerContractTests
```

Expected: `FileNotFoundError` for `installer/EbaydaHelper.iss`.

- [ ] **Step 3: Create the Inno Setup installer definition**

Create `installer/EbaydaHelper.iss`:

```ini
#define MyAppName "Ebayda Helper"
#define MyAppVersion "0.1.0"
#define MyAppExeName "EbaydaHelper.exe"

[Setup]
AppId={{EAA11134-2364-4B14-A6C8-6F85FC868A61}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\EbaydaHelper
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename=EbaydaHelperSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Registry]
Root: HKCU; Subkey: "Software\Classes\ebayda"; ValueType: string; ValueName: ""; ValueData: "URL:Ebayda Helper Protocol"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\ebayda"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\ebayda\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"
Root: HKCU; Subkey: "Software\Classes\ebayda\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""
```

Uninstall removes only the protocol registration and installed executable. It must not delete future `%LOCALAPPDATA%\EbaydaHelper\profiles` data.

- [ ] **Step 4: Add the Windows build script**

Create `installer/build-helper.ps1`:

```powershell
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

Push-Location $repoRoot
try {
    python -m PyInstaller --noconfirm --clean --onefile --name EbaydaHelper ebayda_helper.py

    $iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        throw "未找到 Inno Setup 6，请先安装后重试。"
    }
    & $iscc "$PSScriptRoot\EbaydaHelper.iss"
}
finally {
    Pop-Location
}
```

The Windows build prerequisites are Python 3.9+, `pip install pyinstaller`, and Inno Setup 6. Running `powershell -ExecutionPolicy Bypass -File installer/build-helper.ps1` must produce `dist-installer/EbaydaHelperSetup-0.1.0.exe`.

- [ ] **Step 5: Run all offline verification**

Run on the current development machine:

```bash
.venv/bin/python -m unittest -v
.venv/bin/python -m py_compile ebayda_helper.py test_ebayda_helper.py
git diff --check
```

Expected: all tests pass, compilation succeeds, and the diff check has no whitespace errors.

Run later on Windows:

```powershell
python -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File installer/build-helper.ps1
Start-Process "ebayda://run?job_id=test_job&ticket=abcdefghijklmnop"
```

Expected: the installer is generated, installation writes only HKCU protocol entries, and Chrome/Windows can launch `EbaydaHelper.exe` with the complete URL argument. The final command will report a safe HTTP claim failure until the website endpoint is implemented.

- [ ] **Step 6: Commit the Windows packaging files**

```bash
git add installer/EbaydaHelper.iss installer/build-helper.ps1 test_ebayda_helper.py
git commit -m "build: 增加本地助手 Windows 安装配置"
```

## Deferred until the website API contract exists

- Downloading product JSON and image ZIP from signed URLs.
- Mapping `shop_id` to `%LOCALAPPDATA%\EbaydaHelper\profiles\<shop_id>`.
- Starting system Chrome with that profile and a remote-debugging port.
- Calling the existing DrissionPage automation and posting progress events.
- Device registration, version enforcement, single-instance forwarding, and automatic updates.

These are deliberately deferred because this repository does not contain the website backend, and implementing them now would require inventing an unverified response contract.
