# Ebayda 本地助手任务执行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让已被 `ebayda://` 唤起的本地助手下载当前任务的商品 JSON 和图片 ZIP，使用对应店铺的专用 Chrome Profile 执行现有 DrissionPage 草稿流程，并向网站回传阶段和结果。

**Architecture:** `ebayda_helper.py` 保留协议、HTTPS API 和顶层编排职责；新增 `helper_runtime.py` 管理受信任务负载、本地路径、流式下载、系统 Chrome 和现有自动化调用。当前 `main.py` 只增加显式 `argv` 参数，避免助手修改全局 `sys.argv`。所有外部 I/O 均可注入测试替身，运行时只使用 Python 标准库和现有 DrissionPage。

**Tech Stack:** Python 3.9、标准库 `urllib/subprocess/socket/pathlib`、DrissionPage 4.1.1.4、`unittest`、PyInstaller、Google Chrome。

---

## Fixed backend contract

`POST /api/automation/jobs/{job_id}/claim` 成功响应固定为：

```json
{
  "job_id": "job_abc123",
  "shop_id": "101",
  "action": "save_draft",
  "job_token": "short-lived-job-token",
  "product_json_url": "https://www.ebayda.com/api/automation/jobs/job_abc123/product-json",
  "images_zip_url": "https://www.ebayda.com/api/automation/jobs/job_abc123/images"
}
```

两个下载 URL 必须是 `https://www.ebayda.com` 当前任务下的固定路径，可以带签名查询参数，不允许用户信息、非 443 端口或 fragment。下载和事件请求使用 `Authorization: JobToken <job_token>`；`job_token` 不写日志、不出现在结果 JSON 中。

### Task 1: Validate claimed jobs and download their files atomically

**Files:**
- Create: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/helper_runtime.py`
- Create: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_helper_runtime.py`
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/ebayda_helper.py`

- [ ] **Step 1: Write failing payload and download tests**

Create tests for:

```python
class ClaimedJobTests(unittest.TestCase):
    def test_valid_payload_is_normalized(self) -> None:
        job = helper_runtime.ClaimedJob.from_payload(valid_payload())
        self.assertEqual(job.shop_id, "101")
        self.assertEqual(job.action, "save_draft")

    def test_urls_must_be_https_ebayda_job_resources(self) -> None:
        for url in (
            "http://www.ebayda.com/api/automation/jobs/job_1/product-json",
            "https://evil.example/api/automation/jobs/job_1/product-json",
            "https://www.ebayda.com/api/automation/jobs/other/product-json",
            "https://user@www.ebayda.com/api/automation/jobs/job_1/product-json",
        ):
            payload = valid_payload()
            payload["product_json_url"] = url
            with self.assertRaises(helper_runtime.TaskExecutionError):
                helper_runtime.ClaimedJob.from_payload(payload)

    def test_download_uses_job_token_and_atomic_fixed_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            files = helper_runtime.prepare_job_files(
                helper_runtime.ClaimedJob.from_payload(valid_payload()),
                Path(directory),
                open_url=fake_download_opener,
            )
            self.assertEqual(files.json_path.name, "product.json")
            self.assertEqual(files.images_path.name, "images.zip")
            self.assertFalse(files.json_path.with_suffix(".json.part").exists())
```

Also cover a declared or streamed response larger than 10 MiB for JSON / 2 GiB for ZIP, a failed download removing `.part`, and ticket-safe transport errors.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
.venv/bin/python -m unittest -v test_helper_runtime.ClaimedJobTests test_helper_runtime.DownloadTests
```

Expected: import failure because `helper_runtime.py` does not exist.

- [ ] **Step 3: Implement the payload and file boundary**

Create the public exception `TaskExecutionError`, immutable records `ClaimedJob` and
`TaskFiles`, the constructor `ClaimedJob.from_payload(payload)`, and
`prepare_job_files(job, app_root, *, open_url=urlopen)`. `ClaimedJob` contains exactly
`job_id`, `shop_id`, `action`, `job_token`, `product_json_url`, and `images_zip_url`.
`TaskFiles` contains exactly `json_path`, `images_path`, and `work_dir`.

`from_payload()` must validate all six fields, the action, ASCII token length 16-2048, and the exact trusted resource paths. `prepare_job_files()` must create `jobs/<job_id>/`, download to fixed `.part` files in 64 KiB chunks, enforce byte limits, call `Path.replace()` only after success, and remove only its own `.part` file after failure.

- [ ] **Step 4: Extend `claim_job()` validation**

After existing job/action/shop checks, call `ClaimedJob.from_payload(payload)` before returning. This rejects an incomplete backend response before any filesystem or browser action.

- [ ] **Step 5: Run tests and commit**

```bash
.venv/bin/python -m unittest -v test_helper_runtime.py test_ebayda_helper.py
.venv/bin/python -m unittest -v
git add helper_runtime.py test_helper_runtime.py ebayda_helper.py test_ebayda_helper.py
git commit -m "feat: 下载本地助手任务文件"
```

### Task 2: Create shop profiles and launch system Chrome

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/helper_runtime.py`
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_helper_runtime.py`

- [ ] **Step 1: Write failing profile and Chrome tests**

Tests must prove:

```python
def test_shop_profile_is_under_application_data(self) -> None:
    self.assertEqual(
        helper_runtime.shop_profile(Path("C:/data/EbaydaHelper"), "101"),
        Path("C:/data/EbaydaHelper/profiles/101"),
    )

def test_chrome_uses_non_default_profile_and_ephemeral_debug_port(self) -> None:
    port = helper_runtime.ensure_chrome(
        profile,
        chrome_executable=Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        popen=fake_popen_that_writes_devtools_port,
        port_is_open=lambda port: port == 17321,
        sleep=lambda _seconds: None,
    )
    self.assertEqual(port, 17321)
    self.assertIn(f"--user-data-dir={profile}", command)
    self.assertIn("--remote-debugging-port=0", command)
```

Also test reuse of a live `DevToolsActivePort`, rejection of an invalid port file, Chrome path lookup failure, and timeout without guessing a port.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
.venv/bin/python -m unittest -v test_helper_runtime.ChromeRuntimeTests
```

Expected: missing profile/Chrome helpers.

- [ ] **Step 3: Implement application paths and Chrome startup**

Add `application_root() -> Path`, `shop_profile(app_root, shop_id) -> Path`,
`find_chrome_executable(candidates=None) -> Path`, and the injectable
`ensure_chrome(profile_dir, *, chrome_executable=None, popen=subprocess.Popen,
port_is_open=_port_is_open, sleep=time.sleep, timeout=20) -> int`.

Windows root is `%LOCALAPPDATA%\EbaydaHelper`; macOS development root is `~/Library/Application Support/EbaydaHelper`. Chrome must receive `--remote-debugging-port=0`, `--user-data-dir=<profile>`, `--no-first-run`, `--no-default-browser-check`, and the existing `START_PAGE_URL`. Reuse a port only when `DevToolsActivePort` contains an integer from 1-65535 and the socket is open.

- [ ] **Step 4: Run tests and commit**

```bash
.venv/bin/python -m unittest -v test_helper_runtime.ChromeRuntimeTests
.venv/bin/python -m unittest -v
git add helper_runtime.py test_helper_runtime.py
git commit -m "feat: 按店铺启动专用 Chrome Profile"
```

### Task 3: Invoke the existing automation without changing global argv

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/main.py`
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/helper_runtime.py`
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_helper_runtime.py`

- [ ] **Step 1: Write failing explicit-argv tests**

```python
class AutomationRunnerTests(unittest.TestCase):
    def test_main_accepts_explicit_arguments(self) -> None:
        args = dewu_main.parse_args(["--skip-size-chart"])
        self.assertTrue(args.skip_size_chart)

    def test_runner_passes_downloads_port_and_save_mode(self) -> None:
        calls = []
        exit_code = helper_runtime.run_automation(files, 17321, runner=lambda argv: calls.append(argv) or 0)
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls[0],
            [
                "--json", str(files.json_path),
                "--images", str(files.images_path),
                "--work-dir", str(files.work_dir),
                "--port", "17321",
                "--execute",
            ],
        )
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
.venv/bin/python -m unittest -v test_helper_runtime.AutomationRunnerTests
```

Expected: `parse_args()` rejects an explicit list and `run_automation()` is absent.

- [ ] **Step 3: Add explicit argv support and the adapter**

Change only the signatures and parser call in `main.py`:

```python
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
```

Add `run_automation()` in `helper_runtime.py`; it builds the exact list in the test and calls the injected runner, which defaults to `main.main`. Do not patch `sys.argv` and do not add `--no-save`.

- [ ] **Step 4: Run tests and commit**

```bash
.venv/bin/python -m unittest -v test_helper_runtime.AutomationRunnerTests
.venv/bin/python -m unittest -v
git add main.py helper_runtime.py test_helper_runtime.py
git commit -m "feat: 接入现有得物自动化执行核心"
```

### Task 4: Orchestrate progress events and final status

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/ebayda_helper.py`
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_ebayda_helper.py`
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/installer/build-helper.ps1`

- [ ] **Step 1: Write failing event and orchestration tests**

Tests must assert that:

- event requests POST UTF-8 JSON to `/api/automation/jobs/{job_id}/events` with `Authorization: JobToken <job_token>`;
- the helper emits `preparing`, then `running`, then one final status;
- automation exit `0` maps to `draft_saved`, `2`/`130` map to `paused_for_user`, other codes map to `failed`;
- final output contains only `status/job_id/shop_id` and no launch URL, launch ticket, job token, or download URL;
- a failure before browser execution reports `failed`; failure to post the final event does not rerun or change a successfully saved draft.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
.venv/bin/python -m unittest -v test_ebayda_helper.CommandLineTests test_ebayda_helper.EventTests
```

Expected: missing event/orchestration behavior and old `claimed` output.

- [ ] **Step 3: Implement event posting and orchestration**

Add `post_event(job, status, *, open_url=urlopen) -> None` and
`execute_claimed_job(payload) -> tuple[str, str, str]`.

`execute_claimed_job()` must call, in order: `ClaimedJob.from_payload()`, `application_root()`, `post_event(preparing)`, `prepare_job_files()`, `shop_profile()`, `ensure_chrome()`, `post_event(running)`, `run_automation()`, and a best-effort final `post_event()`. Convert `TaskExecutionError` to `HelperError` without including tokens or URLs.

Update `main()` to print the final tuple as safe JSON and return `0` for `draft_saved`, `2` for `paused_for_user`, and `3` for `failed`.

- [ ] **Step 4: Keep Windows build prerequisites explicit**

At the start of `installer/build-helper.ps1`, run:

```powershell
python -m PyInstaller --version | Out-Null
```

Then retain the existing one-file build and Inno Setup invocation. No new Python runtime dependency is added.

- [ ] **Step 5: Run full verification and commit**

```bash
.venv/bin/python -m unittest -v
.venv/bin/python -m py_compile ebayda_helper.py helper_runtime.py main.py test_ebayda_helper.py test_helper_runtime.py
git diff --check
git add ebayda_helper.py helper_runtime.py main.py test_ebayda_helper.py test_helper_runtime.py installer/build-helper.ps1
git commit -m "feat: 串联本地助手自动上架任务"
```

On macOS, additionally build the helper into a temporary directory with PyInstaller and verify an invalid deep link returns safe JSON. Actual Chrome execution, Windows EXE generation, protocol registration and website API integration remain Windows/end-to-end verification steps because those external systems are unavailable in this repository.
