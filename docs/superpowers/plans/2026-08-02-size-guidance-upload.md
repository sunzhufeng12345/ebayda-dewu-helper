# Size Guidance Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select size recommendation and try-on report workbooks from the product size range, clear existing modal data when the page exposes “一键清空”, and upload the selected workbook before confirming each modal.

**Architecture:** Resolve both workbook paths before browser attachment from normalized `product.sizes` range keys. Add one shared `DewuAutomation` modal workflow for the two buttons: open the labeled modal, clear when possible, upload through its file input, wait for the file name, and confirm. Missing clear controls produce a warning and continue with the page's overwrite/append behavior.

**Tech Stack:** Python 3, `pathlib`, DrissionPage element helpers, `unittest`.

---

### Task 1: Configuration resolution and modal behavior tests

**Files:**
- Modify: `test_main.py`
- Test: `test_main.py`

- [ ] Add tests for trailing-hyphen/`5X` filename normalization, missing workbook errors, clear-before-upload ordering, and upload fallback when “一键清空” is absent.
- [ ] Run the focused tests and verify they fail because the resolver and upload workflow do not exist yet.

### Task 2: Implement workbook resolution and browser upload workflow

**Files:**
- Modify: `main.py:38-105, 484-527, 1933-1986`

- [ ] Add normalized size-range lookup under `配置文件/尺码推荐` and `配置文件/试穿报告`.
- [ ] Pass resolved paths through `RunSettings` and invoke the two uploads after the size table is filled.
- [ ] Implement shared modal discovery, “一键清空” handling, file upload verification, confirmation, and result counts.
- [ ] Run focused tests and then the complete `unittest` suite.

### Task 3: Verification

**Files:**
- Verify: `main.py`, `models.py`, `test_main.py`

- [ ] Run the full test suite and Python compilation check.
- [ ] Inspect the final diff and confirm no browser session or review submission was performed.
