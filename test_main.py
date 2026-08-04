from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main


class SkipSizeChartTests(unittest.TestCase):
    def test_parse_args_accepts_skip_size_chart(self) -> None:
        with patch.object(sys, "argv", ["main.py", "--skip-size-chart"]):
            args = main.parse_args()

        self.assertTrue(args.skip_size_chart)


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

    def test_brand_selection_clicks_first_visible_option(self) -> None:
        brand_input = _FocusRequiredInput()
        options = [
            SimpleNamespace(
                text="第一个品牌",
                states=SimpleNamespace(is_displayed=True),
                rect=_FakeRect(120, 34),
            ),
            SimpleNamespace(
                text="第二个品牌",
                states=SimpleNamespace(is_displayed=True),
                rect=_FakeRect(120, 34),
            ),
        ]
        clicked: list[object] = []
        automation = object.__new__(main.DewuStartPage)
        automation.settings = SimpleNamespace(timeout=1)
        automation._start_input = lambda _label: brand_input
        automation._visible_elements = lambda _xpath, **_kwargs: options

        def click(element: object) -> None:
            clicked.append(element)
            if element is options[0]:
                brand_input.value = options[0].text

        automation._click = click
        automation._wait_until = lambda predicate, **_kwargs: predicate()

        automation._select_brand()

        self.assertEqual(clicked, [brand_input, options[0]])
        self.assertEqual(brand_input.value, "第一个品牌")

    def test_category_selection_clicks_each_level_in_order(self) -> None:
        category_input = _FocusRequiredInput()
        options = {
            value: SimpleNamespace(
                text=value,
                states=SimpleNamespace(is_displayed=True),
                rect=_FakeRect(120, 34),
            )
            for value in main.START_CATEGORY_PATH
        }
        clicked: list[object] = []
        automation = object.__new__(main.DewuStartPage)
        automation.settings = SimpleNamespace(timeout=1)
        automation._start_input = lambda _label, **_kwargs: category_input
        automation._wait_for_cascader_option = lambda value: options[value]
        automation._wait_until = lambda predicate, **_kwargs: predicate()

        def click(element: object) -> None:
            clicked.append(element)
            if element in options.values():
                selected = element.text
                index = main.START_CATEGORY_PATH.index(selected) + 1
                category_input.value = ">>".join(main.START_CATEGORY_PATH[:index])

        automation._click = click

        automation._select_category()

        self.assertEqual(
            clicked[0],
            category_input,
        )
        self.assertEqual(
            [element.text for element in clicked[1:]],
            list(main.START_CATEGORY_PATH),
        )
        self.assertEqual(category_input.value, "服装>>上衣>>卫衣")

    def test_category_selection_waits_for_enabled_input(self) -> None:
        category_input = _FocusRequiredInput()
        options = {
            value: SimpleNamespace(
                text=value,
                states=SimpleNamespace(is_displayed=True),
                rect=_FakeRect(120, 34),
            )
            for value in main.START_CATEGORY_PATH
        }
        automation = object.__new__(main.DewuStartPage)
        automation.settings = SimpleNamespace(timeout=1)
        input_calls = 0

        def start_input(_label: str, required: bool = True) -> _FocusRequiredInput:
            nonlocal input_calls
            input_calls += 1
            if input_calls == 1 and required:
                self.fail("类目输入框仍未可用")
            return category_input

        automation._start_input = start_input
        automation._wait_for_cascader_option = lambda value: options[value]
        automation._wait_until = lambda predicate, **_kwargs: predicate()

        def click(element: object) -> None:
            if element in options.values():
                selected = element.text
                index = main.START_CATEGORY_PATH.index(selected) + 1
                category_input.value = ">>".join(main.START_CATEGORY_PATH[:index])

        automation._click = click

        automation._select_category()

        self.assertEqual(category_input.value, "服装>>上衣>>卫衣")

    def test_blank_check_allows_category_until_brand_is_selected(self) -> None:
        fields = {
            "商品品牌": _FocusRequiredInput(),
            "商品类目": _FocusRequiredInput(),
            "适用人群": _FocusRequiredInput(),
        }
        brand_states = [None, fields["商品品牌"]]
        automation = object.__new__(main.DewuStartPage)

        def start_input(
            label: str,
            required: bool = True,
            allow_disabled: bool = False,
        ) -> _FocusRequiredInput | None:
            if required:
                self.fail("空白校验不应在异步字段未挂载时强制查找")
            if label == "商品品牌" and brand_states:
                return brand_states.pop(0)
            if label == "商品类目" and not allow_disabled:
                return None
            return fields.get(label)

        def wait(predicate, **_kwargs):
            for _ in range(2):
                value = predicate()
                if value:
                    return value
            self.fail("空白校验没有等待字段挂载")

        automation._start_input = start_input
        automation._start_image_items = lambda: []
        automation._wait_until = wait

        automation._verify_blank()

    def test_first_square_upload_uses_first_file(self) -> None:
        class FakeUploadInput:
            def __init__(self) -> None:
                self.uploaded: object | None = None

            def input(self, value: object) -> None:
                self.uploaded = value

        upload_input = FakeUploadInput()
        first_square = Path("first-square.jpg").resolve()
        automation = object.__new__(main.DewuStartPage)
        automation.media = SimpleNamespace(first_square=(first_square,))
        automation.settings = SimpleNamespace(upload_timeout=1)
        automation.result = SimpleNamespace(uploaded_counts={"start_image": 0})
        automation._start_file_input = lambda: upload_input
        automation._wait_until = lambda _predicate, **_kwargs: True

        with patch.object(Path, "is_file", return_value=True):
            automation._upload_first_square()

        self.assertEqual(upload_input.uploaded, str(first_square))

    def test_created_detail_candidates_exclude_existing_detail_tabs(self) -> None:
        existing = SimpleNamespace(
            url="https://stark.dewu.com/vueProduct/newProductApply/spuEdit/operation/old"
        )
        created = SimpleNamespace(
            url="https://stark.dewu.com/vueProduct/newProductApply/spuEdit/operation/new"
        )

        candidates = main._new_detail_tabs(
            [existing, created],
            {str(existing.url)},
        )

        self.assertEqual(candidates, [created])

    def test_media_validation_requires_first_square(self) -> None:
        product = SimpleNamespace(colors=())
        media = SimpleNamespace(
            first_square=(),
            carousel_by_color={},
            product_display_backs=(),
            details=(),
            outfit_fronts=(),
        )

        with self.assertRaises(main.ProductDataError):
            main._validate_media_for_page(product, media)


class WorkflowOrderTests(unittest.TestCase):
    def test_basic_fields_are_filled_before_structured_title(self) -> None:
        automation = object.__new__(main.DewuAutomation)
        automation.tab = SimpleNamespace(url="https://stark.dewu.com/vueProduct/newProductApply/spuEdit/operation/1")
        automation.settings = SimpleNamespace(skip_size_chart=False, save_draft=False)
        automation.result = main.RunResult()
        events: list[str] = []

        def record(name: str):
            def callback(*_args: object, **_kwargs: object) -> None:
                events.append(name)

            return callback

        for name in (
            "_verify_target_page",
            "_fill_title",
            "_fill_basic_fields",
            "_fill_attributes",
            "_fill_colors",
            "_fill_sizes",
            "_fill_skus",
            "_upload_carousel",
            "_upload_detail_sections",
            "_fill_external_link",
        ):
            setattr(automation, name, record(name.removeprefix("_fill_").replace("_", " ")))
        automation._read_visible_errors = lambda: []

        automation.run()

        self.assertLess(events.index("basic fields"), events.index("title"))

    def test_save_draft_is_attempted_after_nonblocking_page_warnings(self) -> None:
        automation = object.__new__(main.DewuAutomation)
        automation.tab = SimpleNamespace(
            url="https://stark.dewu.com/vueProduct/newProductApply/spuEdit/operation/1"
        )
        automation.settings = SimpleNamespace(skip_size_chart=False, save_draft=True)
        automation.result = main.RunResult()
        for name in (
            "_verify_target_page",
            "_fill_basic_fields",
            "_fill_title",
            "_fill_attributes",
            "_fill_colors",
            "_fill_sizes",
            "_upload_size_guidance",
            "_fill_skus",
            "_upload_carousel",
            "_upload_detail_sections",
            "_fill_external_link",
        ):
            setattr(automation, name, lambda: None)
        automation._read_visible_errors = lambda: ["销售规格 填写有误"]
        save_calls: list[bool] = []

        def save() -> None:
            save_calls.append(True)
            automation.result.status = "draft_saved"

        automation._save_draft = save

        automation.run()

        self.assertEqual(save_calls, [True])
        self.assertEqual(automation.result.status, "draft_saved")
        self.assertEqual(automation.result.validation_errors, ["销售规格 填写有误"])


class SaveResultTests(unittest.TestCase):
    def test_result_page_feedback_is_recognized_as_draft_save(self) -> None:
        self.assertTrue(
            main._is_save_result_page(
                "https://stark.dewu.com/main/newProductApply/spuEdit/result",
                "得物商家后台 提交成功",
            )
        )
        self.assertFalse(
            main._is_save_result_page(
                "https://stark.dewu.com/main/newProductApply/submitApply",
                "得物商家后台 提交成功",
            )
        )


class FormTextFieldTests(unittest.TestCase):
    def test_form_item_accepts_a_generic_text_label(self) -> None:
        form_item = object()
        automation = object.__new__(main.DewuAutomation)
        automation._visible_elements = lambda xpath, **_kwargs: (
            [form_item] if ".//*[normalize-space" in xpath else []
        )

        self.assertIs(automation._form_item("成分含量"), form_item)

    def test_form_text_field_focuses_before_input(self) -> None:
        input_element = _FocusRequiredInput()
        form_item = _FakeFormItem([input_element])
        automation = object.__new__(main.DewuAutomation)
        automation._form_item = lambda _label: form_item
        automation._wait_until = lambda predicate, **_kwargs: predicate()

        automation._fill_form_text("成分含量", "棉100%")

        self.assertTrue(input_element.focused)
        self.assertEqual(input_element.value, "棉100%")


class _FakeRect:
    def __init__(self, width: int, height: int) -> None:
        self.size = (width, height)


class _FakeSizeRow:
    def __init__(self, *, aria_hidden: str | None, class_name: str, size: tuple[int, int]) -> None:
        self._attrs = {
            "aria-hidden": aria_hidden,
            "class": class_name,
        }
        self.rect = _FakeRect(*size)

    def attr(self, name: str) -> str | None:
        return self._attrs.get(name)


class _FakeStates:
    is_displayed = True


class _FocusRequiredInput:
    states = _FakeStates()

    def __init__(self, value: str = "") -> None:
        self.value = value
        self.focused = False

    def focus(self) -> None:
        self.focused = True

    def input(self, value: str, *, clear: bool) -> None:
        if clear:
            self.value = ""
        if self.focused:
            self.value = str(value)

    def property(self, name: str) -> str | None:
        return self.value if name == "value" else None

    def attr(self, name: str) -> str | None:
        return self.value if name == "value" else None


class _FakeReadonlyInput(_FocusRequiredInput):
    def __init__(self, *, readonly: bool) -> None:
        super().__init__()
        self.readonly = readonly

    def attr(self, name: str) -> str | None:
        if name == "readonly" and self.readonly:
            return "readonly"
        return super().attr(name)


class _FakeSelectInput(_FocusRequiredInput):
    def __init__(self) -> None:
        super().__init__()
        self.dropdown_open = True
        self.input_events: list[tuple[object, bool]] = []

    def input(self, value: object, *, clear: bool) -> None:
        self.input_events.append((value, clear))
        if not clear:
            self.dropdown_open = False
        super().input(str(value), clear=clear)


class _FakeFormItem:
    def __init__(self, inputs: list[_FakeReadonlyInput]) -> None:
        self.inputs = inputs

    def eles(self, locator: str) -> list[_FakeReadonlyInput]:
        return self.inputs if "input" in locator else []


class _FakeCheckbox:
    def __init__(self, checked: bool) -> None:
        self.states = SimpleNamespace(is_checked=checked)

    def attr(self, name: str) -> str | None:
        return None


class _FakeCheckboxLabel:
    def __init__(self, text: str, checked: bool) -> None:
        self.text = text
        self.checkbox = _FakeCheckbox(checked)

    def ele(self, _locator: str, **_kwargs: object) -> _FakeCheckbox:
        return self.checkbox

    def attr(self, name: str) -> str | None:
        return None


class _FakeSizeDataRow:
    def __init__(self, inputs: list[_FocusRequiredInput]) -> None:
        self.inputs = inputs

    def eles(self, locator: str) -> list[_FocusRequiredInput]:
        return self.inputs if "input" in locator else []


class _FakeHeader:
    def __init__(self, text: str) -> None:
        self.text = text


class AttributeSelectionTests(unittest.TestCase):
    def test_multiselect_uses_editable_input_instead_of_readonly_display(self) -> None:
        editable_input = _FakeReadonlyInput(readonly=False)
        readonly_display = _FakeReadonlyInput(readonly=True)
        form_item = _FakeFormItem([editable_input, readonly_display])
        selected: set[str] = set()
        chosen_inputs: list[object] = []
        automation = object.__new__(main.DewuAutomation)
        automation._form_item = lambda _label: form_item
        automation._visible_elements = lambda *_args, **_kwargs: []
        automation._form_item_has_value = lambda _form, value: value in selected

        def select_from_input(input_element, value, *, required):
            chosen_inputs.append(input_element)
            selected.add(value)
            return True

        automation._select_from_input = select_from_input
        automation._wait_until = lambda predicate, **_kwargs: predicate()

        automation._choose_form_value("面料", ("棉",), required=True)

        self.assertEqual(chosen_inputs, [editable_input])

    def test_multiselect_closes_dropdown_after_selecting_value(self) -> None:
        input_element = _FakeSelectInput()
        automation = object.__new__(main.DewuAutomation)
        automation._click = lambda _element: None
        automation._wait_for_option = lambda _value, timeout: object()
        automation._input_has_value = lambda _input, _value: True
        automation._wait_until = lambda predicate, **_kwargs: predicate()

        self.assertTrue(
            automation._select_from_input(input_element, "棉", required=True)
        )
        self.assertFalse(input_element.dropdown_open)


class SizeChartRowTests(unittest.TestCase):
    def test_hidden_ant_table_measure_row_is_not_a_size_data_row(self) -> None:
        row = _FakeSizeRow(
            aria_hidden="true",
            class_name="ant-table-measure-row",
            size=(0, 0),
        )

        self.assertFalse(main._is_size_data_row(row))

    def test_size_chart_inputs_are_focused_before_clearing(self) -> None:
        size_input = _FocusRequiredInput()
        row = _FakeSizeDataRow([size_input])
        modal = object()
        table = object()
        automation = object.__new__(main.DewuAutomation)
        automation.product = SimpleNamespace(sizes=("M",))
        automation.settings = SimpleNamespace(size_chart={"M": {}})
        automation._locate_size_modal = lambda: modal
        automation._size_table = lambda _: table
        automation._size_rows = lambda _: [row]
        automation._ensure_size_rows = lambda *_: None
        automation._configure_size_columns = lambda *_: None
        automation._visible_elements = lambda xpath, **_: (
            [_FakeHeader("尺码")] if "thead" in xpath else []
        )
        automation._find_visible = lambda *_args, **_kwargs: object()
        automation._click = lambda *_args, **_kwargs: None
        automation._wait_until = lambda *_args, **_kwargs: True
        automation._select_product_sizes = lambda: None

        automation._fill_sizes()

        self.assertEqual(size_input.value, "M")

    def test_closed_size_modal_is_detected_from_fresh_lookup(self) -> None:
        automation = object.__new__(main.DewuAutomation)
        automation._locate_size_modal = lambda: None

        self.assertTrue(automation._size_modal_closed())

    def test_existing_size_chart_opens_with_edit_button(self) -> None:
        edit_button = object()
        clicked: list[object] = []
        automation = object.__new__(main.DewuAutomation)
        automation._locate_size_modal = lambda: None
        automation._visible_elements = lambda xpath, **_: (
            [edit_button] if "size-box" in xpath else []
        )
        automation._click = lambda element: clicked.append(element)
        automation._wait_for_size_modal = lambda: "modal"

        self.assertEqual(automation._open_size_modal(), "modal")
        self.assertEqual(clicked, [edit_button])

    def test_product_size_options_are_selected_after_size_chart(self) -> None:
        labels = [
            _FakeCheckboxLabel("全选", False),
            _FakeCheckboxLabel("M", False),
            _FakeCheckboxLabel("L", True),
        ]
        automation = object.__new__(main.DewuAutomation)
        automation.product = SimpleNamespace(sizes=("M", "L"))
        automation._size_option_labels = lambda: labels
        automation._click = lambda label: setattr(label.checkbox.states, "is_checked", True)
        automation._wait_until = lambda predicate, **_kwargs: predicate()

        automation._select_product_sizes()

        self.assertTrue(labels[1].checkbox.states.is_checked)
        self.assertTrue(labels[2].checkbox.states.is_checked)

    def test_sku_editable_fields_are_focused_before_input(self) -> None:
        inputs = [_FocusRequiredInput() for _ in range(9)]
        row = _FakeSizeDataRow(inputs)
        automation = object.__new__(main.DewuAutomation)
        automation.settings = SimpleNamespace(
            offer_type="现货",
            package_defaults={
                "length_cm": None,
                "width_cm": None,
                "height_cm": None,
                "weight_kg": None,
            },
        )
        automation._select_from_input = lambda *_args, **_kwargs: True
        sku = SimpleNamespace(
            color="白色",
            size="M",
            product_code="CODE",
            auxiliary_code="AUX",
            offer_amount=Decimal("399"),
            inventory=1000,
        )

        automation._fill_sku_row(row, sku)

        self.assertEqual(inputs[0].value, "CODE")
        self.assertEqual(inputs[1].value, "AUX")
        self.assertEqual(inputs[3].value, "399")
        self.assertEqual(inputs[4].value, "1000")

    def test_sku_js_write_is_verified_and_falls_back_when_vue_resets_fields(self) -> None:
        inputs = [_FocusRequiredInput() for _ in range(9)]

        class JsRow(_FakeSizeDataRow):
            def run_js(self, _script: str) -> list[str]:
                return ["CODE", "AUX", "现货", "399", "1000", "42", "38", "5", "0.8"]

        row = JsRow(inputs)
        automation = object.__new__(main.DewuAutomation)
        automation.settings = SimpleNamespace(
            offer_type="现货",
            package_defaults={
                "length_cm": "42",
                "width_cm": "38",
                "height_cm": "5",
                "weight_kg": "0.8",
            },
        )
        automation._select_from_input = lambda *_args, **_kwargs: True
        sku = SimpleNamespace(
            color="白色",
            size="M",
            product_code="CODE",
            auxiliary_code="AUX",
            offer_amount=Decimal("399"),
            inventory=1000,
        )

        automation._fill_sku_row(row, sku)

        self.assertEqual([item.value for item in inputs], ["CODE", "AUX", "", "399", "1000", "42", "38", "5", "0.8"])


class ExternalLinkTests(unittest.TestCase):
    def test_external_link_blurs_after_writing_fixed_no_value(self) -> None:
        automation = object.__new__(main.DewuAutomation)
        automation.settings = SimpleNamespace(external_link="无")
        element = object()
        filled: list[tuple[str, str]] = []
        blurred: list[object] = []
        automation._fill_placeholder = lambda placeholder, value: filled.append((placeholder, value))
        automation._external_link_element = lambda: element
        automation._blur_element = lambda target: blurred.append(target)

        automation._fill_external_link()

        self.assertEqual(filled, [(
            '请填写此商品的真实外网销售链接，如果是得物专供/得物首发商品，可如实备注或直接填写"无"',
            "无",
        )])
        self.assertEqual(blurred, [element])

    def test_size_rows_wait_for_async_initial_row(self) -> None:
        automation = object.__new__(main.DewuAutomation)
        table = object()
        states = [[], [object()]]
        automation._size_rows = lambda _: states.pop(0) if states else [object()]
        automation._wait_until = lambda predicate, **_kwargs: predicate()

        automation._ensure_size_rows(table, ("M",))

    def test_extra_blank_size_rows_are_removed_from_the_end(self) -> None:
        rows = [_FakeSizeDataRow([_FocusRequiredInput()]) for _ in range(3)]
        minus_control = object()
        automation = object.__new__(main.DewuAutomation)
        automation._size_rows = lambda _table: rows
        automation._visible_elements = lambda xpath, **_kwargs: (
            [minus_control] if "minus-circle" in xpath else []
        )
        automation._click = lambda _element: rows.pop()
        automation._wait_until = lambda predicate, **_kwargs: predicate()

        automation._ensure_size_rows(object(), ("M", "L"))

        self.assertEqual(len(rows), 2)

    def test_platform_default_size_rows_are_reconciled_to_source(self) -> None:
        rows = [
            _FakeSizeDataRow([_FocusRequiredInput(value), _FocusRequiredInput()])
            for value in ("XS", "S", "M", "L", "XL", "2XL")
        ]
        delete_controls = {row: object() for row in rows}
        plus_control = object()
        automation = object.__new__(main.DewuAutomation)
        automation._size_rows = lambda _table: rows
        automation._visible_elements = lambda xpath, *, scope=None, **_kwargs: (
            [delete_controls[scope]]
            if "minus-circle" in xpath and scope in delete_controls
            else [plus_control]
            if "plus-circle" in xpath and scope is rows[-1]
            else []
        )

        def click(control: object) -> None:
            if control is plus_control:
                row = _FakeSizeDataRow([_FocusRequiredInput(), _FocusRequiredInput()])
                rows.append(row)
                delete_controls[row] = object()
                return
            row = next(row for row, item in delete_controls.items() if item is control)
            rows.remove(row)

        automation._click = click
        automation._wait_until = lambda predicate, **_kwargs: predicate()

        automation._ensure_size_rows(
            object(),
            ("M", "L", "XL", "2XL", "3XL"),
        )

        values = [main._element_value(row.inputs[0]) for row in rows]
        self.assertEqual(values, ["M", "L", "XL", "2XL", ""])


if __name__ == "__main__":
    unittest.main()
