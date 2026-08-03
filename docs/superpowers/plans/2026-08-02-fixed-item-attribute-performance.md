# 固定包装信息与属性填写优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (recommended) to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用 JSON 的 `itemNumber`，固定填写包装信息，并修正属性映射与等待逻辑，减少属性填写耗时。

**Architecture:** 保持 `models.py` 负责来源数据归一化，`main.py` 负责页面配置和交互。把来源 `商品类型与品牌` 的 `subValueText` 映射到得物页面的 `设计元素`，保留直接来源属性 `图案` 的可能性；将属性控件查询统一限制在短超时内，避免缺失选项触发浏览器默认长等待。

**Tech Stack:** Python 3.9、DrissionPage、`unittest`。

---

### Task 1: Lock the requested data mappings with failing tests

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_main.py`
- Test: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_main.py`

- [x] **Step 1: Add tests for `itemNumber`, fixed package defaults, and design-element mapping**

Add tests that load the checked-in sample and assert the requested source mapping:

```python
class ProductMappingTests(unittest.TestCase):
    def test_item_number_and_source_pattern_mapping(self) -> None:
        product = load_product(Path(__file__).with_name("1632.json"))

        self.assertEqual(product.item_no, "PB26XY01LJB-ZB9603")
        self.assertEqual(product.attributes["设计元素"], ("印花",))

    def test_package_defaults_are_fixed(self) -> None:
        self.assertEqual(
            main.PACKAGE_DEFAULTS,
            {"length_cm": "42", "width_cm": "38", "height_cm": "5", "weight_kg": "0.8"},
        )
```

Import `load_product` from `models` with the existing test imports.

- [x] **Step 2: Add a failing test for the short optional-attribute wait**

Record the timeout passed to `_wait_for_option` when an optional value is absent:

```python
    def test_optional_attribute_missing_option_uses_short_wait(self) -> None:
        input_element = _FakeReadonlyInput(readonly=False)
        timeouts: list[float] = []
        automation = object.__new__(main.DewuAutomation)
        automation.result = SimpleNamespace(warnings=[])
        automation._click = lambda _element: None
        automation._wait_for_option = lambda _value, timeout: timeouts.append(timeout) or None

        self.assertFalse(
            automation._select_from_input(input_element, "不存在的属性", required=False)
        )
        self.assertEqual(timeouts, [1])
```

- [x] **Step 3: Run the focused tests and confirm they fail for the expected reasons**

Run:

```bash
.venv/bin/python -m unittest -v test_main.ProductMappingTests test_main.AttributeSelectionTests.test_optional_attribute_missing_option_uses_short_wait
```

Expected: the item number is still `ZB9603`, `设计元素` is absent, package defaults are `None`, and the optional wait is still `5` seconds.

### Task 2: Implement the mappings and fixed values

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/models.py:101-176,455-475`
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/main.py:45-51,1106-1205,1207-1250,1088-1095,1127-1131`
- Test: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_main.py`

- [x] **Step 1: Use `itemNumber` and map source design values to `设计元素`**

Read `itemNumber` with the existing required-text helper and set `ProductData.item_no` to it. Keep `code` as the product/SKU code. Change the `leixing-pinpai`/`商品类型与品牌` sub-value target from `图案` to `设计元素`, and add `设计元素` to the ordered detail-page attribute labels. Leave a directly supplied `图案` attribute untouched.

- [x] **Step 2: Set the fixed package values**

Replace the four `None` values in `PACKAGE_DEFAULTS` with the string values `"42"`, `"38"`, `"5"`, and `"0.8"`, and update the comment/warning logic so the empty-package warning no longer appears.

- [x] **Step 3: Run the focused tests and confirm they pass**

Run the focused command from Task 1. Expected: all mapping and timeout tests pass.

### Task 3: Remove avoidable attribute waits

**Files:**
- Modify: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/main.py:1088-1249`
- Test: `/Users/sunzhufeng/project/wiabao/dianshang/python_dp/dewu_dp/test_main.py`

- [x] **Step 1: Use the existing visible-element helper for scoped attribute queries**

Replace direct `form_item.eles(...)` and `select.eles(...)` calls in attribute filling and value verification with `_visible_elements(..., scope=...)`, so every lookup uses the existing one-second query timeout. Preserve the current visible-element and selected-tag checks.

- [x] **Step 2: Use a one-second option timeout for optional attributes**

Keep the five-second option wait for required fields, but pass one second to `_wait_for_option` when `required=False`. This makes an unavailable optional value such as the old `图案=印花` mapping fail fast and record a warning.

- [x] **Step 3: Run the full unit suite and the offline preflight**

Run:

```bash
.venv/bin/python -m unittest -v test_main.py
.venv/bin/python main.py
```

Expected: all tests pass, the preflight item number is `PB26XY01LJB-ZB9603`, package values are no longer warned as empty, and `设计元素` contains `印花`.

### Task 4: Verify the live page behavior without submitting

**Files:**
- No additional files.

- [ ] **Step 1: Run the browser flow with `--execute --no-save` only after the user has a blank start page**

Check that important attributes no longer pause on absent tags, `设计元素` receives `印花`, the `图案` field is not cleared by later attributes, and the SKU packaging inputs receive the four fixed values.

- [ ] **Step 2: Stop before save or review submission and report the result**

Do not click `保存草稿` or `提交审核` during this verification.
