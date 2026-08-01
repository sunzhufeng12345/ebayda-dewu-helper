# 得物申请新品起始页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从空白的得物“申请新品”起始页选择品牌、类目和适用人群，上传第一张方图，创建申请并进入现有详情页自动化。

**Architecture:** 在 `main.py` 中增加独立的 `DewuStartPage` 页面对象和起始页标签页连接函数。它只负责起始页的空白校验、Element UI 交互、上传及跳转；跳转成功后复用现有 `DewuAutomation`。起始页和详情页共享商品媒体与运行结果，但不共享表单定位逻辑。

**Tech Stack:** Python 3.9、DrissionPage、`unittest`、Chrome DevTools 远程调试。

---

### Task 1: Add failing start-page safety tests

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_main.py`
- Test: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_main.py`

- [ ] **Step 1: Write the failing tests**

Add tests for the pure start-page boundaries before adding production code:

```python
class StartPageTests(unittest.TestCase):
    def test_start_page_url_is_recognized(self) -> None:
        self.assertTrue(
            main._is_start_page_url(
                "https://stark.dewu.com/vueProduct/newProductApply/start?noLayout=1"
            )
        )
        self.assertFalse(
            main._is_start_page_url(
                "https://stark.dewu.com/vueProduct/newProductApply/spuEdit/operation/106"
            )
        )

    def test_non_empty_start_page_state_is_rejected(self) -> None:
        with self.assertRaises(main.AutomationError):
            main._validate_start_page_state(
                {"商品品牌": "pcgocollection", "商品类目": "", "适用人群": "", "商品链接": ""},
                image_count=0,
            )

    def test_start_category_value_matches_cascader_path(self) -> None:
        self.assertTrue(main._start_category_value_matches("服装>>上衣>>卫衣"))
        self.assertFalse(main._start_category_value_matches("服装>>上衣>>夹克"))
```

- [ ] **Step 2: Run the focused tests and verify the failure is meaningful**

Run:

```bash
.venv/bin/python -m unittest -v test_main.StartPageTests
```

Expected: `AttributeError` for the missing start-page helpers, before any browser operation.

- [ ] **Step 3: Commit the red tests**

```bash
git add test_main.py
git commit -m "test: 增加申请新品起始页边界测试"
```

### Task 2: Implement start-page constants, state checks, and selectors

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/main.py`
- Test: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_main.py`

- [ ] **Step 1: Add constants and pure helpers**

Define the fixed route and choices near the existing detail-page constants:

```python
START_PATH_FRAGMENT = "/vueProduct/newProductApply/start"
START_PAGE_URL = f"https://{TARGET_HOST}{START_PATH_FRAGMENT}?noLayout=1"
START_CATEGORY_PATH = ("服装", "上衣", "卫衣")
START_AUDIENCE = "通用"
```

Implement `_is_start_page_url()`, `_validate_start_page_state()` and `_start_category_value_matches()` using `urlparse`, stripped values, and `AutomationError`. The state validator must reject any non-empty brand/category/audience/link or any image count other than zero.

- [ ] **Step 2: Implement `DewuStartPage` form-item lookup**

Add a page object that receives `browser`, `tab`, `media`, `settings`, and `result`. Use the start-page `<form>` and visible `el-form-item` labels to locate brand, category, audience, image, and link controls. Do not use the detail-page `//main` locator.

- [ ] **Step 3: Implement brand, category, and audience selection**

Within `DewuStartPage.run()`:

1. Read all four text inputs and upload-list rows, then call `_validate_start_page_state()`.
2. Click the brand select input, wait for layout-visible `li.el-select-dropdown__item` elements, click index zero, and wait for a non-empty value.
3. Click the cascader input and, for each value in `START_CATEGORY_PATH`, wait for an exact visible `li.el-cascader-node`, click it, and after the final click verify `_start_category_value_matches()`.
4. Click the audience select input, choose exact visible `通用`, and verify the input value.

All waits must poll the current DOM and use the existing timeout setting; hidden dropdown templates must be excluded by both display state and non-zero rectangle dimensions.

- [ ] **Step 4: Run the focused tests and the existing suite**

Run:

```bash
.venv/bin/python -m unittest -v test_main.StartPageTests
.venv/bin/python -m unittest -v test_main.py
```

Expected: all start-page tests and the existing size/SKU tests pass.

- [ ] **Step 5: Commit the start-page selector implementation**

```bash
git add main.py test_main.py
git commit -m "feat: 增加申请新品起始页选择流程"
```

### Task 3: Implement first-square upload and create-to-detail transition

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/main.py`
- Test: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_main.py`

- [ ] **Step 1: Add a failing media-source test**

Add a fake file input test that calls `DewuStartPage._upload_first_square()` with `MediaFiles.first_square=(Path("first-square.jpg"),)` and asserts the file input receives exactly that path, not `main`, `first_long`, or a color image.

- [ ] **Step 2: Run the new test and confirm it fails before implementation**

Run:

```bash
.venv/bin/python -m unittest -v test_main.StartPageTests.test_first_square_upload_uses_first_file
```

Expected: failure because `DewuStartPage` does not yet implement the upload method.

- [ ] **Step 3: Implement upload and create transition**

Require `media.first_square[0]`; call the scoped `input[type=file]` with its absolute path; wait for one successful preview row and image source. Before clicking create, re-read and verify the product-link input is still empty. Click the exact `创建新品发布申请` button and wait for one newly appearing URL containing `TARGET_PATH_FRAGMENT`.

Record `new_product_start` in `RunResult.completed_sections`, update `page_url`, activate the new/current detail tab, and return it. Existing detail tabs present before the click must not be selected as the result.

- [ ] **Step 4: Validate the first-square preflight input**

In `_validate_media_for_page()`, reject an empty `media.first_square` collection and reject unsupported first-square suffixes before connecting to Chrome. Keep the existing ZIP extraction and JSON data unchanged.

- [ ] **Step 5: Run the full unit suite and commit**

```bash
.venv/bin/python -m unittest -v test_main.py
git add main.py test_main.py
git commit -m "feat: 上传首张方图并进入新品详情页"
```

### Task 4: Wire the new flow into `main()` and verify with Chrome

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/main.py`

- [ ] **Step 1: Refactor Chrome attachment into start-page and detail-page selection**

Keep the existing authenticated-only connection behavior, but add `attach_to_dewu_start_tab()`. It must use exactly one existing start tab, or open `START_PAGE_URL` in the authenticated browser when no start tab exists; multiple start tabs are an error. The old detail-page attachment helper remains available for compatibility.

- [ ] **Step 2: Call the start page before `DewuAutomation`**

Replace the direct detail-page attachment in `main()` with:

```python
browser, start_tab = attach_to_dewu_start_tab(args.port)
detail_tab = DewuStartPage(browser, start_tab, media, settings, result).run()
result = DewuAutomation(detail_tab, product, media, settings, result).run()
```

The start-page object must use the same `RunResult`, and no save or submit call may be added to the start-page branch.

- [ ] **Step 3: Run offline preflight and full unit tests**

```bash
.venv/bin/python -m unittest -v test_main.py
.venv/bin/python main.py
```

Expected: tests pass and offline preflight reports `first_square` as available without connecting to Chrome.

- [ ] **Step 4: Reset Chrome to one blank start page and run the first integration checkpoint**

Close only the known exploration-residue start tab, open/refresh one blank `START_PAGE_URL`, and run:

```bash
.venv/bin/python main.py --execute --no-save
```

Verify in order: one brand selected, category input reads `服装>>上衣>>卫衣`, audience reads `通用`, exactly one first-square preview exists, link remains empty, and the URL changes to a new `/spuEdit/operation/` page. Stop at the first real detail-page error and report it before changing later logic.

- [ ] **Step 5: Commit only after the integration checkpoint**

```bash
git add main.py
git commit -m "feat: 串联申请新品起始页与详情页"
```
