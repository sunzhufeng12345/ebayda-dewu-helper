"""得物新品草稿自动化。

常用命令（在本文件目录执行）：
  .venv/bin/python main.py
  .venv/bin/python main.py --execute --no-save
  .venv/bin/python main.py --execute --no-save --skip-size-chart
  .venv/bin/python main.py --execute

前两条分别用于数据预检和首次页面填写调试；最后一条才会尝试保存草稿。
外网链接按业务规则固定填写“无”。
程序没有提交审核操作。
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from models import (
    MediaFiles,
    ProductData,
    ProductDataError,
    SkuData,
    extract_and_resolve_media,
    load_product,
)


# 下面这些值是得物页面的业务默认值，不是来源商品事实；如果商家账号的口径不同，
# 只需要在这里调整默认值，来源 JSON 的解析逻辑不需要跟着修改。
DEFAULT_OFFER_TYPE = "现货"
DEFAULT_PRICE_PROOF_SOURCE = "品牌官网"
DEFAULT_RELEASE_PROOF_SOURCE = "品牌官网"

# 同一商品所有 SKU 共用的包装信息。值为 None 时保留页面为空，不会凭空猜测尺寸或重量。
PACKAGE_DEFAULTS: Mapping[str, str | None] = {
    "length_cm": None,
    "width_cm": None,
    "height_cm": None,
    "weight_kg": None,
}

# 当前来源数据只提供尺码名称，没有提供实测参数。下面先按宽松卫衣的常见
# 递增关系填写临时估算值，仅用于本次页面流程调试；正式商品应替换为实测值。
SIZE_CHART: Mapping[str, Mapping[str, str]] = {
    "M": {"1/2胸围(cm)": "55", "衣长(cm)": "68", "袖长(cm)": "61"},
    "L": {"1/2胸围(cm)": "57", "衣长(cm)": "70", "袖长(cm)": "62"},
    "XL": {"1/2胸围(cm)": "59", "衣长(cm)": "72", "袖长(cm)": "63"},
    "2XL": {"1/2胸围(cm)": "61", "衣长(cm)": "74", "袖长(cm)": "64"},
    "3XL": {"1/2胸围(cm)": "63", "衣长(cm)": "76", "袖长(cm)": "65"},
}

# 这里的值只覆盖本次运行内存中的来源属性，不会回写或修改原始 JSON 文件。
ATTRIBUTE_OVERRIDES: Mapping[str, tuple[str, ...]] = {}

# 页面和媒体上传限制集中定义，预检与实际上传共用这些规则，避免前后判断不一致。
TARGET_HOST = "stark.dewu.com"
START_PATH_FRAGMENT = "/vueProduct/newProductApply/start"
START_PAGE_URL = f"https://{TARGET_HOST}{START_PATH_FRAGMENT}?noLayout=1"
START_CATEGORY_PATH = ("服装", "上衣", "卫衣")
START_AUDIENCE = "通用"
TARGET_PATH_FRAGMENT = "/vueProduct/newProductApply/spuEdit/operation/"
MAX_CAROUSEL_PER_COLOR = 5
MAX_PRODUCT_DISPLAY_IMAGES = 20
MAX_DETAIL_IMAGES = 8
MAX_OUTFIT_IMAGES = 15
MAX_CAROUSEL_FILE_BYTES = 5 * 1024 * 1024
MAX_DETAIL_FILE_BYTES = 20 * 1024 * 1024


class AutomationError(RuntimeError):
    """当前页面无法安全验证或修改时抛出的自动化异常。"""


@dataclass(frozen=True)
class RunSettings:
    # 一次浏览器任务的运行参数。冻结数据类可避免填写过程中被意外改写。
    debugger_port: int
    offer_type: str
    price_proof_source: str
    release_proof_source: str
    external_link: str
    save_draft: bool
    skip_size_chart: bool = False
    timeout: float = 12.0
    upload_timeout: float = 120.0
    package_defaults: Mapping[str, str | None] = field(default_factory=lambda: PACKAGE_DEFAULTS)
    size_chart: Mapping[str, Mapping[str, str]] = field(default_factory=lambda: SIZE_CHART)


@dataclass
class RunResult:
    # 运行结果既用于终端 JSON 输出，也用于失败时告诉人工已经完成到哪一步。
    status: str = "not_started"
    completed_sections: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    uploaded_counts: dict[str, int] = field(
        default_factory=lambda: {
            "start_image": 0,
            "carousel": 0,
            "product_display": 0,
            "detail": 0,
            "outfit": 0,
        }
    )
    sku_rows: int = 0
    submission: str = "not_attempted"
    page_url: str = ""


class DewuStartPage:
    # 起始页使用 Element UI 表单结构，与详情页的 main 内容区定位逻辑分开维护。
    def __init__(
        self,
        browser: Any,
        tab: Any,
        media: MediaFiles,
        settings: RunSettings,
        result: RunResult,
    ) -> None:
        self.browser = browser
        self.tab = tab
        self.media = media
        self.settings = settings
        self.result = result

    def run(self) -> Any:
        # 起始页完成后只负责创建申请并返回新的详情页，不保存草稿或提交审核。
        self._verify_blank()
        self._select_brand()
        self._select_category()
        self._select_audience()
        self._verify_blank()
        self._upload_first_square()
        self._verify_external_link_blank()
        return self._create_application()

    def _verify_blank(self) -> None:
        fields = {
            label: _element_value(self._start_input(label))
            for label in ("商品品牌", "商品类目", "适用人群")
        }
        external_link = self._start_input("商品链接", required=False)
        if external_link is not None:
            fields["商品链接"] = _element_value(external_link)
        image_count = len(self._start_image_items())
        _validate_start_page_state(fields, image_count)

    def _select_brand(self) -> None:
        input_element = self._start_input("商品品牌")
        self._click(input_element)
        option = self._wait_until(
            self._first_visible_select_option,
            message="商品品牌下拉框没有可选项",
        )
        self._click(option)
        self._wait_until(
            lambda: bool(_element_value(self._start_input("商品品牌"))),
            message="商品品牌没有回显",
        )

    def _select_category(self) -> None:
        input_element = self._start_input("商品类目")
        self._click(input_element)
        for value in START_CATEGORY_PATH:
            option = self._wait_until(
                lambda value=value: self._wait_for_cascader_option(value),
                message=f"商品类目没有找到：{value}",
            )
            self._click(option)
        self._wait_until(
            lambda: _start_category_value_matches(
                _element_value(self._start_input("商品类目"))
            ),
            message="商品类目没有回显完整路径：服装>>上衣>>卫衣",
        )

    def _select_audience(self) -> None:
        input_element = self._start_input("适用人群")
        self._click(input_element)
        option = self._wait_until(
            lambda: self._wait_for_select_option(START_AUDIENCE),
            message="适用人群没有找到：通用",
        )
        self._click(option)
        self._wait_until(
            lambda: _element_value(self._start_input("适用人群")) == START_AUDIENCE,
            message="适用人群没有回显：通用",
        )

    def _upload_first_square(self) -> None:
        if not self.media.first_square:
            raise AutomationError("来源图片中没有第一张方图，无法创建新品申请")
        image_path = self.media.first_square[0].expanduser().resolve()
        if not image_path.is_file():
            raise AutomationError(f"第一张方图文件不存在：{image_path}")

        upload_input = self._start_file_input()
        upload_input.input(str(image_path))
        self._wait_until(
            self._first_square_uploaded,
            timeout=self.settings.upload_timeout,
            message="等待第一张方图上传完成超时",
        )
        self.result.uploaded_counts["start_image"] = 1

    def _verify_external_link_blank(self) -> None:
        value = _element_value(self._start_input("商品链接"))
        if value:
            raise AutomationError(f"起始页商品链接不是空值：{value}")

    def _create_application(self) -> Any:
        before_urls = {
            str(tab.url)
            for tab in self.browser.get_tabs()
            if _is_detail_page_url(str(tab.url))
        }
        button = self._find_visible(
            "//button[normalize-space(.)='创建新品发布申请']",
            "创建新品发布申请按钮",
        )
        self._click(button)

        def locate_created_tab() -> Any | None:
            candidates = _new_detail_tabs(self.browser.get_tabs(), before_urls)
            if len(candidates) > 1:
                raise AutomationError(
                    f"创建新品申请后出现多个新详情页：{len(candidates)}"
                )
            return candidates[0] if candidates else None

        detail_tab = self._wait_until(
            locate_created_tab,
            timeout=max(self.settings.timeout, 30),
            message="创建新品发布申请后没有进入详情页",
        )
        try:
            detail_tab.set.activate()
        except Exception as error:
            raise AutomationError("无法激活创建后的新品详情页") from error
        self.tab = detail_tab
        self.result.page_url = str(detail_tab.url)
        self.result.completed_sections.append("new_product_start")
        return detail_tab

    def _start_form_item(self, label: str, *, required: bool = True) -> Any | None:
        literal = _xpath_literal(label)
        items = self._visible_elements(
            "//form//*[contains(concat(' ',normalize-space(@class),' '),' el-form-item ')]"
            f"[.//label[contains(normalize-space(.),{literal})]]"
        )
        if len(items) != 1:
            if not required and not items:
                return None
            raise AutomationError(f"起始页字段“{label}”数量异常：{len(items)}")
        return items[0]

    def _start_input(self, label: str, *, required: bool = True) -> Any | None:
        form_item = self._start_form_item(label, required=required)
        if form_item is None:
            return None
        inputs = [
            item
            for item in form_item.eles(
                "xpath:.//input[not(@type='file') and not(@disabled)] | "
                ".//textarea[not(@disabled)]"
            )
            if _is_displayed(item)
        ]
        if not inputs:
            if not required:
                return None
            raise AutomationError(f"起始页字段“{label}”没有输入框")
        return inputs[0]

    def _start_image_items(self) -> list[Any]:
        form_item = self._start_form_item("商品图片", required=False)
        if form_item is None:
            return []
        return self._visible_elements(
            ".//ul[contains(@class,'el-upload-list')]//li",
            scope=form_item,
        )

    def _start_file_input(self) -> Any:
        form_item = self._start_form_item("商品图片")
        inputs = form_item.eles("xpath:.//input[@type='file']")
        if len(inputs) != 1:
            raise AutomationError(f"起始页商品图片文件输入框数量异常：{len(inputs)}")
        return inputs[0]

    def _first_square_uploaded(self) -> bool:
        items = self._start_image_items()
        if len(items) != 1:
            return False
        item = items[0]
        if "is-success" not in str(item.attr("class") or ""):
            return False
        images = item.eles("xpath:.//img")
        return any(str(image.attr("src") or "").strip() for image in images)

    def _first_visible_select_option(self) -> Any | None:
        options = self._visible_elements(
            "//body//li[contains(@class,'el-select-dropdown__item')]"
        )
        return next((_item for _item in options if _has_layout(_item)), None)

    def _wait_for_select_option(self, value: str) -> Any | None:
        literal = _xpath_literal(value)
        options = self._visible_elements(
            "//body//li[contains(@class,'el-select-dropdown__item')]"
            f"[normalize-space(.)={literal}]"
        )
        return next((_item for _item in options if _has_layout(_item)), None)

    def _wait_for_cascader_option(self, value: str) -> Any | None:
        literal = _xpath_literal(value)
        options = self._visible_elements(
            "//body//li[contains(@class,'el-cascader-node')]"
            f"[normalize-space(.)={literal}]"
        )
        return next((_item for _item in options if _has_layout(_item)), None)

    def _find_visible(self, xpath: str, description: str) -> Any:
        elements = self._visible_elements(xpath)
        if not elements:
            raise AutomationError(f"找不到{description}")
        return elements[0]

    def _visible_elements(self, xpath: str, *, scope: Any | None = None) -> list[Any]:
        locator = xpath if xpath.startswith("xpath:") else f"xpath:{xpath}"
        owner = scope or self.tab
        try:
            elements = owner.eles(locator, timeout=1)
        except TypeError:
            elements = owner.eles(locator)
        return [element for element in elements if _is_displayed(element)]

    def _click(self, element: Any) -> None:
        self._scroll(element)
        element.click(timeout=self.settings.timeout)

    def _scroll(self, element: Any) -> None:
        try:
            element.scroll.to_see(center=True)
        except Exception:
            pass

    def _wait_until(
        self,
        predicate: Callable[[], Any],
        *,
        timeout: float | None = None,
        message: str = "等待页面状态变化超时",
    ) -> Any:
        deadline = time.monotonic() + (timeout or self.settings.timeout)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except Exception as error:
                last_error = error
            time.sleep(0.15)
        if last_error:
            raise AutomationError(f"{message}：{last_error}") from last_error
        raise AutomationError(message)


def parse_args() -> argparse.Namespace:
    # 命令行只负责收集参数；文件存在性和数值范围在主流程中统一校验。
    parser = argparse.ArgumentParser(
        description="读取选品中心 JSON 和图片 ZIP，填写得物新品页面并仅保存草稿。",
    )
    parser.add_argument("--json", type=Path, help="来源 API JSON；省略时自动使用当前目录唯一 JSON")
    parser.add_argument("--images", type=Path, help="图片 ZIP；省略时自动使用当前目录唯一 ZIP")
    parser.add_argument("--work-dir", type=Path, help="图片解压目录，默认 .dewu_work")
    parser.add_argument("--port", type=int, default=9222, help="Chrome 远程调试端口，默认 9222")
    parser.add_argument("--offer-type", default=DEFAULT_OFFER_TYPE, help="SKU 出价类型")
    parser.add_argument("--price-proof-source", default=DEFAULT_PRICE_PROOF_SOURCE)
    parser.add_argument("--release-proof-source", default=DEFAULT_RELEASE_PROOF_SOURCE)
    parser.add_argument(
        "--attribute",
        action="append",
        default=[],
        metavar="字段=值1,值2",
        help="覆盖或补充得物属性，可重复传入",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真正操作浏览器；不传时只解析和检查数据",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="执行填写但停在保存草稿前，用于首次调试选择器",
    )
    parser.add_argument(
        "--skip-size-chart",
        action="store_true",
        help="跳过尺码表，仅用于暂不确定尺码表逻辑时继续调试后续步骤",
    )
    parser.add_argument("--timeout", type=float, default=12.0, help="普通页面操作超时秒数")
    return parser.parse_args()


class DewuAutomation:
    # 该类只负责“已经准备好的商品数据”如何映射到当前得物页面，
    # 不负责解析 JSON 或解压 ZIP，从而把数据问题和页面问题分开定位。
    def __init__(
        self,
        tab: Any,
        product: ProductData,
        media: MediaFiles,
        settings: RunSettings,
        result: RunResult,
    ) -> None:
        self.tab = tab
        self.product = product
        self.media = media
        self.settings = settings
        self.result = result

    def run(self) -> RunResult:
        # 页面填写按“安全检查 -> 基础信息 -> 销售规格 -> 图片 -> 补充信息”的顺序进行。
        # 每完成一个大区块就记录标记，异常时可以判断最后一个已确认完成的阶段。
        self.result.status = "running"
        self.result.page_url = str(self.tab.url)
        self._verify_target_page()
        if self.settings.skip_size_chart:
            self._skip_size_chart()

        self._fill_title()
        self.result.completed_sections.append("title")

        self._fill_basic_fields()
        self._fill_attributes()
        self.result.completed_sections.append("basic_info")

        self._fill_colors()
        if not self.settings.skip_size_chart:
            self._fill_sizes()
        self._fill_skus()
        self.result.completed_sections.append("sales_info")

        self._upload_carousel()
        self._upload_detail_sections()
        self.result.completed_sections.append("media")

        self._fill_external_link()
        self.result.completed_sections.append("supplement")

        # 页面可能在填写过程中留下异步校验错误，保存前再统一读取一次可见错误。
        errors = self._read_visible_errors()
        if errors:
            self.result.status = "needs_input"
            self.result.validation_errors.extend(errors)
            return self.result

        # --no-save 用于首次调试选择器；填写成功不代表已经保存草稿。
        if not self.settings.save_draft:
            self.result.status = "filled_not_saved"
            return self.result

        self._save_draft()
        return self.result

    def _verify_target_page(self) -> None:
        # 先校验域名、路径和关键按钮，防止把输入写入错误标签页。
        # “提交审核”只用于确认页面类型，程序从不点击它。
        parsed = urlparse(str(self.tab.url))
        if parsed.hostname != TARGET_HOST or TARGET_PATH_FRAGMENT not in parsed.path:
            raise AutomationError(f"当前标签页不是得物新品草稿页：{self.tab.url}")
        self._find_visible("//button[normalize-space(.)='保存草稿']", "保存草稿按钮")
        self._find_visible("//button[normalize-space(.)='提交审核']", "提交审核按钮")
        if not self._visible_elements("//*[normalize-space(.)='申请新品']"):
            raise AutomationError("页面未显示“申请新品”，可能尚未加载完成或登录已过期")

    def _fill_title(self) -> None:
        # 结构化标题的三段分别写入独立控件，同时在写入前检查平台要求的总长度。
        self._fill_placeholder("卖点提炼", self.product.title.selling_point)
        self._fill_placeholder("类目", self.product.title.category)
        # 适用人群标题片段由基础信息中的下拉控件统一设置；这里不直接写只读标题输入框。

        title_text = "".join(
            (
                self.product.brand,
                self.product.title.selling_point,
                self.product.title.category,
                self.product.title.audience,
            )
        )
        if len(title_text) < 16 or len(title_text) > 60:
            raise AutomationError(f"结构化标题长度不合法：{len(title_text)}，标题={title_text}")

    def _fill_basic_fields(self) -> None:
        # 基础字段包含货号、发售价格和发售日期；日期统一选择当天，证明渠道使用运行配置。
        self._choose_form_value("适用人群", (self.product.audience,), required=True)
        self._fill_placeholder("请输入货号（货号重复不计入特殊符号）", self.product.item_no)

        self._choose_radio("发售价格", "有发售价格")
        self._fill_placeholder("请输入发售价格", _number_text(self.product.release_price))
        self._select_by_placeholder(
            "请选择发售价证明渠道来源",
            self.settings.price_proof_source,
            required=True,
        )

        self._choose_radio("发售日期", "年/月/日")
        self._choose_today()
        self._select_by_placeholder(
            "请输入正确的发售日期证明渠道来源（与搜索转化相关）",
            self.settings.release_proof_source,
            required=True,
        )

    def _fill_attributes(self) -> None:
        # 按页面顺序填写属性。required 中的字段缺失会中止任务，非必填字段失败则记录警告继续。
        required = {"领型", "衣长", "版型", "厚度", "面料", "是否加绒"}
        attributes = dict(self.product.attributes)

        ordered_labels = (
            "领型",
            "衣长",
            "版型",
            "厚度",
            "适用季节",
            "面料",
            "是否加绒",
            "成分含量",
            "图案",
            "风格",
            "袖长",
            "衣门襟",
        )
        for label in ordered_labels:
            # 来源没有该属性时跳过；来源有值但页面找不到对应控件时再按必填级别处理。
            values = attributes.get(label)
            if not values:
                continue
            try:
                if label == "成分含量":
                    self._fill_form_text(label, values[0])
                else:
                    self._choose_form_value(label, values, required=label in required)
            except AutomationError as error:
                if label in required:
                    raise
                self.result.warnings.append(str(error))

    def _fill_colors(self) -> None:
        # 颜色行是可增删的动态控件。先核对页面已有前缀，再补空行，最后删除多余的空行。
        # 任何已有非空内容不一致都不覆盖，避免误改人工草稿。
        locator = "//main//input[@placeholder='请选择或输入标准色']"
        inputs = self._visible_elements(locator)
        if not inputs:
            raise AutomationError("页面没有颜色输入框")

        values = [_element_value(item) for item in inputs]
        first_blank = next((index for index, value in enumerate(values) if not value), len(values))
        existing = tuple(values[:first_blank])
        if any(values[first_blank:]):
            raise AutomationError(f"页面颜色行中存在空行后又有已填值：{values}")
        if existing != self.product.colors[: len(existing)]:
            raise AutomationError(
                f"页面已有颜色与来源前缀不一致：来源={self.product.colors}，页面={tuple(values)}；"
                "程序不会删除或覆盖已有颜色"
            )
        if len(existing) > len(self.product.colors):
            raise AutomationError(
                f"页面已有 {len(existing)} 个颜色，超过来源 {len(self.product.colors)} 个"
            )

        color_form_item = inputs[0].ele(
            "xpath:./ancestor::*[contains(concat(' ',normalize-space(@class),' '),' el-form-item ')][1]",
            timeout=2,
        )
        if not color_form_item:
            raise AutomationError("找不到颜色商品规格容器")

        filled_count = len(existing)
        while filled_count < len(self.product.colors):
            # 页面可能预渲染多个空行，也可能只有一个初始行；两种情况分别复用或点击“新增”。
            inputs = self._visible_elements(locator)
            if filled_count < len(inputs):
                target_input = inputs[filled_count]
            else:
                add_button = self._find_visible(
                    ".//button[normalize-space(.)='新增']",
                    "颜色新增按钮",
                    scope=color_form_item,
                )
                previous_count = len(inputs)
                self._click(add_button)
                self._wait_until(
                    lambda: len(self._visible_elements(locator)) == previous_count + 1
                )
                target_input = self._visible_elements(locator)[-1]
            self._set_creatable_select(target_input, self.product.colors[filled_count])
            filled_count += 1

        inputs = self._visible_elements(locator)
        while len(inputs) > len(self.product.colors):
            # 只允许删除空行，来源之外的非空颜色必须由人工确认后再处理。
            extra_input = inputs[-1]
            if _element_value(extra_input):
                raise AutomationError("页面存在来源之外的非空颜色行，程序不会删除")
            row = extra_input.ele(
                "xpath:./ancestor::*[.//button[normalize-space(.)='删除']][1]",
                timeout=1,
            )
            delete_button = row.ele("xpath:.//button[normalize-space(.)='删除']", timeout=1) if row else None
            if not delete_button or _is_disabled(delete_button):
                raise AutomationError("无法移除多余的空颜色行，请手工清理后重试")
            previous_count = len(inputs)
            self._click(delete_button)
            self._wait_until(lambda: len(self._visible_elements(locator)) == previous_count - 1)
            inputs = self._visible_elements(locator)

        values = tuple(_element_value(item) for item in self._visible_elements(locator))
        if values != self.product.colors:
            raise AutomationError(f"颜色回显不一致：期望 {self.product.colors}，实际 {values}")

    def _fill_sizes(self) -> None:
        # 尺码表是一个独立弹窗/抽屉：先取得或打开弹窗，再同步列配置、行数和每一行的尺码。
        # 尺码名称来自来源 SKU，测量值只使用显式配置的 SIZE_CHART，不做推测。
        modal = self._open_size_modal()

        # 只请求配置中真正出现过的测量列，并用 dict.fromkeys 保持首次出现顺序且去重。
        requested_parameters = tuple(
            dict.fromkeys(
                parameter
                for row in self.settings.size_chart.values()
                for parameter in row.keys()
            )
        )
        self._configure_size_columns(modal, requested_parameters)

        # 先调整行数，再读取表头和已有内容；已有非空尺码与来源冲突时不覆盖。
        table = self._size_table(modal)
        self._ensure_size_rows(table, len(self.product.sizes))
        rows = self._size_rows(table)
        if len(rows) != len(self.product.sizes):
            raise AutomationError(
                f"尺码表行数与来源不一致：期望 {len(self.product.sizes)}，实际 {len(rows)}；"
                "程序不会删除已有非空尺码行"
            )
        headers = [
            _clean_header_text(item.text)
            for item in self._visible_elements(".//thead//th", scope=table)
        ]
        if not headers or _canonical_size_label(headers[0]) != "尺码":
            raise AutomationError(f"尺码表第一列不是尺码：{headers}")

        current_sizes: list[str] = []
        for row in rows:
            row_inputs = [item for item in row.eles("xpath:.//input") if _is_displayed(item)]
            current_sizes.append(
                _element_value(row_inputs[0]) if row_inputs else ""
            )
        conflicts = [
            (index + 1, current, expected)
            for index, (current, expected) in enumerate(zip(current_sizes, self.product.sizes))
            if current and current != expected
        ]
        if conflicts:
            raise AutomationError(
                f"尺码表已有内容与来源不一致：{conflicts}；程序不会覆盖已有尺码"
            )

        # 第一列填写尺码名称，后续列只在 SIZE_CHART 提供非空值时填写。
        # 每次输入后页面可能替换当前行节点，因此下一次操作前重新读取行和输入框。
        def cell_value(row_index: int, input_index: int) -> str:
            current_rows = self._size_rows(table)
            if row_index >= len(current_rows):
                return ""
            current_inputs = [
                item
                for item in current_rows[row_index].eles("xpath:.//input")
                if _is_displayed(item)
            ]
            if input_index >= len(current_inputs):
                return ""
            return _element_value(current_inputs[input_index])

        for index, size in enumerate(self.product.sizes):
            current_rows = self._size_rows(table)
            if index >= len(current_rows):
                raise AutomationError(f"尺码表第 {index + 1} 行不存在")
            inputs = [
                item
                for item in current_rows[index].eles("xpath:.//input")
                if _is_displayed(item)
            ]
            if not inputs:
                raise AutomationError(f"尺码表第 {index + 1} 行没有输入框")
            self._input_value(inputs[0], size)
            self._wait_until(
                lambda index=index, size=size: cell_value(index, 0) == size,
                message=f"尺码表第 {index + 1} 行回显失败：{size}",
            )

            chart_values = self.settings.size_chart.get(size, {})
            for input_index in range(1, len(inputs)):
                header = headers[input_index] if input_index < len(headers) else ""
                value = _lookup_size_value(chart_values, header)
                if value in (None, ""):
                    continue
                current_rows = self._size_rows(table)
                current_inputs = [
                    item
                    for item in current_rows[index].eles("xpath:.//input")
                    if _is_displayed(item)
                ]
                if input_index >= len(current_inputs):
                    raise AutomationError(
                        f"尺码表第 {index + 1} 行缺少第 {input_index + 1} 列输入框"
                    )
                self._input_value(current_inputs[input_index], str(value))
                self._wait_until(
                    lambda index=index, input_index=input_index, value=str(value): (
                        cell_value(index, input_index) == value
                    ),
                    message=f"尺码表第 {index + 1} 行第 {input_index + 1} 列回显失败",
                )

        # 点击确定后必须等待弹窗真正消失，并进一步等待销售规格行生成。
        confirm = self._find_visible(
            ".//button[normalize-space(.)='确 定' or normalize-space(.)='确定']",
            "尺码表确定按钮",
            scope=modal,
        )
        self._click(confirm)

        try:
            self._wait_until(self._size_modal_closed, timeout=5)
        except AutomationError:
            errors = [
                item.text.strip()
                for item in self._visible_elements(
                    ".//*[contains(@class,'error') or contains(@class,'Error')]",
                    scope=modal,
                )
                if item.text.strip()
            ]
            detail = "；".join(dict.fromkeys(errors)) or "弹窗仍未关闭"
            if not self.settings.size_chart:
                detail += "；来源 JSON 没有尺码测量值，请在 main.py 的 SIZE_CHART 中补充真实数据"
            raise AutomationError(f"尺码表保存失败：{detail}")

        self._select_product_sizes()
        self._wait_until(lambda: self._sku_variant_rows_present())

    def _skip_size_chart(self) -> None:
        # 调试开关只关闭当前尺码弹窗，不清空或猜测尺码数据；后续销售规格仍按页面现状校验。
        modal = self._locate_size_modal()
        if modal is not None:
            cancel = self._find_visible(
                ".//button[normalize-space(.)='取 消' or normalize-space(.)='取消']",
                "尺码表取消按钮",
                scope=modal,
            )
            self._click(cancel)
            self._wait_until(
                lambda: self._locate_size_modal() is None,
                timeout=5,
                message="跳过尺码表时弹窗没有关闭",
            )
        self.result.warnings.append("已按 --skip-size-chart 跳过尺码表")

    def _fill_skus(self) -> None:
        # 尺码表保存后页面会生成颜色×尺码的 SKU 表，表格可能分页。
        # 逐页按颜色和尺码匹配来源记录，同时用页面文本签名检测分页是否真的变化。
        expected = set(self.product.sku_by_variant)
        actual_total = self._sku_total()
        if actual_total != len(expected):
            raise AutomationError(
                f"销售规格总数不一致：来源 {len(expected)} 条，页面 {actual_total} 条"
            )
        self._go_to_first_sku_page()

        processed: set[tuple[str, str]] = set()
        first_page_signature = ""

        while True:
            # 每次循环只处理当前页，直到下一页按钮不存在或已禁用。
            rows = self._sku_rows()
            if not rows:
                raise AutomationError("销售规格表没有可填写的 SKU 行")
            signature = "|".join(row.text[:120] for row in rows)
            if signature == first_page_signature:
                raise AutomationError("SKU 分页没有变化，停止以避免重复填写")
            first_page_signature = signature

            for row in rows:
                # 页面顺序不必与来源顺序相同，所以不能按索引填写，必须按规格键匹配。
                cells = row.eles("xpath:./td")
                if len(cells) < 2:
                    continue
                color = cells[0].text.strip()
                size = cells[1].text.strip()
                key = (color, size)
                sku = self.product.sku_by_variant.get(key)
                if sku is None:
                    raise AutomationError(f"页面出现来源中不存在的 SKU：{color}/{size}")
                if key in processed:
                    raise AutomationError(f"页面 SKU 重复：{color}/{size}")
                self._fill_sku_row(row, sku)
                processed.add(key)

            next_button = self._next_page_button()
            if next_button is None or _is_disabled(next_button):
                break
            old_signature = signature
            self._click(next_button)
            self._wait_until(lambda: "|".join(row.text[:120] for row in self._sku_rows()) != old_signature)

        missing = expected - processed
        extra = processed - expected
        if missing or extra:
            raise AutomationError(
                f"SKU 行校验失败：缺少={sorted(missing)}，多出={sorted(extra)}"
            )
        self.result.sku_rows = len(processed)

    def _fill_sku_row(self, row: Any, sku: SkuData) -> None:
        # 一行 SKU 的前五个输入框分别是编码、辅助编码、出价类型、出价和库存，
        # 后四个输入框是包装长宽高和重量；具体列顺序依赖页面固定结构。
        inputs = [item for item in row.eles("xpath:.//input") if _is_displayed(item)]
        if len(inputs) < 9:
            raise AutomationError(
                f"SKU {sku.color}/{sku.size} 输入框数量异常：期望至少 9，实际 {len(inputs)}"
            )

        self._input_value(inputs[0], sku.product_code)
        self._input_value(inputs[1], sku.auxiliary_code)
        self._select_from_input(inputs[2], self.settings.offer_type, required=True)
        self._input_value(inputs[3], _number_text(sku.offer_amount))
        self._input_value(inputs[4], str(sku.inventory))

        # 包装默认值为空时保持页面原值/空值，避免用猜测数据覆盖人工填写。
        package_values = (
            self.settings.package_defaults.get("length_cm"),
            self.settings.package_defaults.get("width_cm"),
            self.settings.package_defaults.get("height_cm"),
            self.settings.package_defaults.get("weight_kg"),
        )
        for element, value in zip(inputs[5:9], package_values):
            if value not in (None, ""):
                self._input_value(element, str(value))

    def _upload_carousel(self) -> None:
        # 轮播图按颜色逐行上传。每上传一张都等待页面计数增加，避免异步上传尚未完成
        # 就继续操作下一张；页面已有多余图片时停止，不自动删除。
        uploaded_total = 0
        for color in self.product.colors:
            expected_files = self.media.carousel_by_color[color]
            if len(expected_files) < 2:
                raise AutomationError(f"颜色“{color}”不足两张轮播图")
            row = self._carousel_row(color)
            current = self._carousel_image_count(row)
            if current > len(expected_files):
                raise AutomationError(
                    f"颜色“{color}”页面已有 {current} 张图，超过来源 {len(expected_files)} 张；"
                    "为避免误删，程序不会继续"
                )
            while current < len(expected_files):
                # 首张图片使用行末的文件控件，后续图片使用页面更新后的首个控件。
                row = self._carousel_row(color)
                file_inputs = row.eles("xpath:.//input[@type='file']")
                if not file_inputs:
                    raise AutomationError(
                        f"颜色“{color}”上传到第 {current + 1} 张时没有文件输入框"
                    )
                upload_input = file_inputs[-1] if current == 0 else file_inputs[0]
                upload_input.input(str(expected_files[current]))
                current += 1
                self._wait_carousel_count(color, current)

            if current != len(expected_files):
                raise AutomationError(
                    f"颜色“{color}”轮播图数量不一致：期望 {len(expected_files)}，实际 {current}"
                )

            uploaded_total += current

        self.result.uploaded_counts["carousel"] = uploaded_total

    def _upload_detail_sections(self) -> None:
        # 三个图片区块使用同一套上传和数量核对逻辑，分别记录到结果摘要。
        self.result.uploaded_counts["product_display"] = self._upload_section(
            "商品展示",
            self.media.product_display_backs,
        )
        self.result.uploaded_counts["detail"] = self._upload_section(
            "细节呈现",
            self.media.details,
        )
        self.result.uploaded_counts["outfit"] = self._upload_section(
            "穿搭效果",
            self.media.outfit_fronts,
        )

    def _upload_section(self, label: str, files: Sequence[Path]) -> int:
        # 区块上传采用幂等策略：数量已经完全一致就复用；部分存在则暂停，要求人工清理。
        container = self._detail_section(label)
        existing = _uploaded_count(container.text)
        if not files:
            if existing:
                raise AutomationError(
                    f"{label}页面已有 {existing} 张图，但来源没有该类图片；"
                    "程序不会保留无法核对的旧图片"
                )
            self.result.warnings.append(f"{label}没有来源图片，已跳过")
            return 0

        input_element = self._find_any(
            ".//input[@type='file']",
            f"{label}文件输入框",
            scope=container,
        )
        if existing == len(files):
            return existing
        if existing != 0:
            raise AutomationError(
                f"{label}页面已有 {existing} 张，来源为 {len(files)} 张；"
                "为避免重复或误删，请先在页面清空该区域"
            )

        # 一次性把当前区块的所有文件交给浏览器，随后只以页面“已上传数量”作为成功依据。
        input_element.input([str(path) for path in files])
        self._wait_until(
            lambda: _uploaded_count(self._detail_section(label).text) == len(files),
            timeout=self.settings.upload_timeout,
            message=f"等待{label}上传完成超时",
        )
        return len(files)

    def _detail_section(self, label: str) -> Any:
        # 通过区块标题和“已上传”文本定位唯一图片区，避免同类控件或隐藏模板被误选。
        literal = _xpath_literal(label)
        sections = self._visible_elements(
            "//main//*[contains(concat(' ',normalize-space(@class),' '),' item-card ')]"
            f"[.//*[normalize-space(.)={literal}] and contains(normalize-space(.),'已上传')]"
        )
        if len(sections) != 1:
            raise AutomationError(f"{label}图片区数量异常：{len(sections)}")
        return sections[0]

    def _fill_external_link(self) -> None:
        # 外链的合法性已在主流程预检；这里仅把最终确认过的值写入页面。
        placeholder = (
            '请填写此商品的真实外网销售链接，如果是得物专供/得物首发商品，可如实备注或直接填写"无"'
        )
        self._fill_placeholder(placeholder, self.settings.external_link)

    def _save_draft(self) -> None:
        # 保存前先清掉旧的成功提示，避免把上一次运行的反馈误认为本次保存成功。
        # 保存按钮点击后只等待成功提示，不触发“提交审核”。
        if self._save_success_messages():
            self._wait_until(
                lambda: not self._save_success_messages(),
                timeout=6,
                message="页面已有未消失的保存成功提示，无法区分本次保存结果",
            )

        button = self._find_visible("//button[normalize-space(.)='保存草稿']", "保存草稿按钮")
        self._click(button)

        def success_message() -> Any | None:
            messages = self._save_success_messages()
            return messages[0] if messages else None

        try:
            message = self._wait_until(success_message, timeout=20, message="没有捕获到保存草稿成功提示")
        except AutomationError as error:
            self.result.status = "not_saved"
            self.result.validation_errors.append(str(error))
            return

        # 即使草稿保存成功，submission 仍保持未尝试，因为程序明确不提交审核。
        self.result.submission = "not_attempted"
        self.result.page_url = str(self.tab.url)
        self.result.warnings.append(f"保存反馈：{message.text.strip()}")
        errors = self._read_visible_errors()
        if errors:
            self.result.status = "needs_input"
            self.result.validation_errors.extend(errors)
            self.result.warnings.append("页面已出现保存反馈，但保存后仍检测到阻止项，请人工复核草稿")
            return
        self.result.status = "draft_saved"

    def _save_success_messages(self) -> list[Any]:
        # 不依赖具体 UI 框架的完整类名，只筛选常见消息容器中同时出现“保存/草稿”和“成功”的提示。
        candidates = self._visible_elements(
            "//*[contains(@class,'message') or contains(@class,'toast')"
            " or contains(@class,'notification')]"
        )
        return [
            item
            for item in candidates
            if "成功" in item.text and ("保存" in item.text or "草稿" in item.text)
        ]

    def _fill_placeholder(self, placeholder: str, value: str) -> None:
        # 通过稳定的 placeholder 定位普通输入框，并在输入后读取 value 做回显确认。
        literal = _xpath_literal(placeholder)
        element = self._find_visible(
            f"//main//input[@placeholder={literal}] | //main//textarea[@placeholder={literal}]",
            f"输入框：{placeholder}",
        )
        self._scroll(element)
        self._input_value(element, str(value))
        expected = str(value).strip()
        self._wait_until(
            lambda: _element_value(element) == expected,
            message=f"输入框回显失败：{placeholder}",
        )

    def _input_value(self, element: Any, value: Any, *, clear: bool = True) -> None:
        # DrissionPage 在 macOS 上用 JS 清空输入框后不会恢复焦点，后续文字可能落到上一个字段。
        # 先聚焦当前元素，再执行清空和输入，兼容普通文本框及已打开的下拉输入框。
        if clear:
            element.focus()
        element.input(value, clear=clear)

    def _fill_form_text(self, label: str, value: str) -> None:
        # 对没有稳定 placeholder 的文本字段，先定位表单项，再使用其中最后一个可编辑控件。
        form_item = self._form_item(label)
        inputs = [
            item
            for item in form_item.eles("xpath:.//input[not(@disabled)] | .//textarea[not(@disabled)]")
            if _is_displayed(item)
        ]
        if not inputs:
            raise AutomationError(f"字段“{label}”没有可输入控件")
        inputs[-1].input(str(value), clear=True)

    def _choose_form_value(self, label: str, values: Sequence[str], *, required: bool) -> None:
        # 兼容单选标签、多选标签和下拉输入三种页面控件形态。
        # values 按来源顺序尝试，required 决定找不到控件时是报错还是记录警告。
        form_item = self._form_item(label)
        for value in values:
            if self._form_item_has_value(form_item, value):
                continue
            literal = _xpath_literal(value)
            option_labels = self._visible_elements(
                f".//label[normalize-space(.)={literal} or .//*[normalize-space(.)={literal}]]",
                scope=form_item,
            )
            if option_labels:
                self._click(option_labels[0])
                self._wait_until(
                    lambda: self._form_item_has_value(self._form_item(label), value),
                    message=f"字段“{label}”没有选中“{value}”",
                )
                form_item = self._form_item(label)
                continue

            inputs = [
                item
                for item in form_item.eles("xpath:.//input[not(@disabled)]")
                if _is_displayed(item)
            ]
            if not inputs:
                if required:
                    raise AutomationError(f"字段“{label}”没有可选择控件")
                self.result.warnings.append(f"页面没有可填写的可选字段：{label}")
                return
            if not self._select_from_input(inputs[-1], value, required=required):
                continue
            self._wait_until(
                lambda: self._form_item_has_value(self._form_item(label), value),
                message=f"字段“{label}”没有回显“{value}”",
            )
            form_item = self._form_item(label)

    def _choose_radio(self, field_label: str, value: str) -> None:
        # 单选项点击后重新定位表单项，确认新节点的 checked 状态，而不是只相信 click 返回。
        form_item = self._form_item(field_label)
        literal = _xpath_literal(value)
        radio = self._find_visible(
            f".//label[normalize-space(.)={literal} or .//*[normalize-space(.)={literal}]]",
            f"{field_label}={value}",
            scope=form_item,
        )
        if _label_is_checked(radio):
            return
        self._click(radio)
        self._wait_until(
            lambda: _label_is_checked(
                self._find_visible(
                    f".//label[normalize-space(.)={literal} or .//*[normalize-space(.)={literal}]]",
                    f"{field_label}={value}",
                    scope=self._form_item(field_label),
                )
            ),
            message=f"单选项没有选中：{field_label}={value}",
        )

    def _select_by_placeholder(self, placeholder: str, value: str, *, required: bool) -> None:
        # 用 placeholder 找到页面下拉输入，再复用统一的选项查找和回显校验。
        literal = _xpath_literal(placeholder)
        elements = self._visible_elements(f"//main//input[@placeholder={literal}]")
        if not elements:
            if required:
                raise AutomationError(f"找不到下拉框：{placeholder}")
            self.result.warnings.append(f"页面没有显示可选字段：{placeholder}")
            return
        self._select_from_input(elements[0], value, required=required)

    def _select_from_input(self, input_element: Any, value: str, *, required: bool) -> bool:
        # 先处理已是目标值的幂等情况；否则打开下拉、等待选项、点击并检查最终显示值。
        if _element_value(input_element) == value:
            return True
        self._click(input_element)
        if input_element.attr("readonly") is None:
            input_element.input(value, clear=True)
        option = self._wait_for_option(value, timeout=5)
        if option is None:
            if required:
                raise AutomationError(f"下拉选项不存在：{value}")
            self.result.warnings.append(f"下拉选项不存在，已跳过：{value}")
            return False
        self._click(option)
        self._wait_until(
            lambda: self._input_has_value(input_element, value),
            message=f"下拉框没有回显：{value}",
        )
        return True

    def _form_item_has_value(self, form_item: Any, value: str) -> bool:
        # 页面组件的选中状态可能体现在 checked、el-tag 或 input value 中，因此逐层兼容判断。
        literal = _xpath_literal(value)
        labels = self._visible_elements(
            f".//label[normalize-space(.)={literal} or .//*[normalize-space(.)={literal}]]",
            scope=form_item,
        )
        if any(_label_is_checked(label) for label in labels):
            return True

        tags = self._visible_elements(
            f".//*[contains(concat(' ',normalize-space(@class),' '),' el-tag ')]"
            f"[normalize-space(.)={literal} or .//*[normalize-space(.)={literal}]]",
            scope=form_item,
        )
        if tags:
            return True

        return any(
            _element_value(item) == value
            for item in form_item.eles(
                "xpath:.//input[not(@type='radio') and not(@type='checkbox')] | .//textarea"
            )
        )

    def _input_has_value(self, input_element: Any, value: str) -> bool:
        # 普通输入直接读 value；下拉输入则额外检查祖先选择器中的标签和可见文本。
        if _element_value(input_element) == value:
            return True
        select = input_element.ele(
            "xpath:./ancestor::*[contains(@class,'select')][1]",
            timeout=1,
        )
        if not select:
            return False
        literal = _xpath_literal(value)
        selected_tags = select.eles(
            "xpath:.//*[contains(@class,'tag') or contains(@class,'selection-item')]"
            f"[normalize-space(.)={literal} or .//*[normalize-space(.)={literal}]]"
        )
        if any(_is_displayed(item) for item in selected_tags):
            return True
        return re.sub(r"\s+", " ", select.text).strip() == value

    def _set_creatable_select(self, input_element: Any, value: str) -> None:
        # 颜色允许输入新建值：有匹配选项就点击，没有匹配项则回车确认创建。
        from DrissionPage.common import Keys

        self._click(input_element)
        input_element.input(value, clear=True)
        option = self._wait_for_option(value, timeout=3)
        if option is not None:
            self._click(option)
        else:
            input_element.input(Keys.ENTER, clear=False)
        self._wait_until(lambda: _element_value(input_element) == value)

    def _choose_today(self) -> None:
        # 发售日期固定为本机当天；优先点击日期控件中的 today 单元格，找不到时再直接输入格式化日期。
        date_input = self._find_visible("//main//input[@placeholder='选择日期']", "发售日期输入框")
        today_value = date.today().strftime("%Y.%m.%d")
        current = _element_value(date_input)
        if current == today_value:
            return
        self._click(date_input)
        today_cells = self._visible_elements(
            "//table[contains(@class,'el-date-table')]//td["
            "contains(@class,'today') and not(contains(@class,'disabled'))]"
        )
        if today_cells:
            # 日期弹层已在视口内；先滚动会触发日期组件重绘，使当前单元格节点失效。
            today_cells[-1].click(timeout=self.settings.timeout)
        else:
            date_input.input(today_value, clear=True)
        self._wait_until(lambda: _element_value(date_input) == today_value)

    def _form_item(self, label: str) -> Any:
        # 得物表单使用 el-form-item 包裹字段，统一从 label 文本向上确定控件作用域。
        literal = _xpath_literal(label)
        xpath = (
            "//main//*[contains(concat(' ',normalize-space(@class),' '),' el-form-item ')]"
            f"[./label[normalize-space(.)={literal}] or .//label[normalize-space(.)={literal}]][1]"
        )
        return self._find_visible(xpath, f"表单字段：{label}")

    def _wait_for_option(self, value: str, timeout: float) -> Any | None:
        # 下拉选项由页面异步渲染，按两种已知选项结构轮询查找。
        literal = _xpath_literal(value)

        def locate() -> Any | None:
            options = self._visible_elements(
                "//li[contains(@class,'select-dropdown__item')]"
                f"[normalize-space(.)={literal} or .//*[normalize-space(.)={literal}]]"
                " | //div[contains(@class,'select-item-option')]"
                f"[normalize-space(.)={literal} or .//*[normalize-space(.)={literal}]]"
            )
            # 页面会保留 display:none 的下拉模板，DrissionPage 仍可能把它标记为 displayed；
            # 只有有实际布局尺寸的候选才是当前打开的选项。
            for option in options:
                try:
                    width, height = option.rect.size
                except Exception:
                    continue
                if width > 0 and height > 0:
                    return option
            return None

        try:
            return self._wait_until(locate, timeout=timeout)
        except AutomationError:
            return None

    def _locate_size_modal(self) -> Any | None:
        # 弹窗定位同时要求存在“添加尺码表”标题和确定按钮，排除其他无关弹窗。
        candidates = self._visible_elements(
            "//*[(@role='dialog' or contains(@class,'modal') or contains(@class,'dialog')"
            " or contains(@class,'drawer'))]"
            "[.//*[contains(normalize-space(.),'添加尺码表')]"
            " and .//button[normalize-space(.)='确 定' or normalize-space(.)='确定']]"
        )
        for candidate in reversed(candidates):
            try:
                width, height = candidate.rect.size
            except Exception:
                continue
            if width > 0 and height > 0:
                return candidate
        return None

    def _open_size_modal(self) -> Any:
        # 首次配置显示“添加尺码表”，已有配置显示“编辑”；重复运行时优先复用已有表。
        modal = self._locate_size_modal()
        if modal is not None:
            return modal

        edit_buttons = self._visible_elements(
            "//main//*[contains(concat(' ',normalize-space(@class),' '),' size-box ')]"
            "//button[normalize-space(.)='编辑']"
        )
        if edit_buttons:
            self._click(edit_buttons[0])
        else:
            self._click(
                self._find_visible(
                    "//button[normalize-space(.)='添加尺码表']",
                    "添加尺码表按钮",
                )
            )
        return self._wait_for_size_modal()

    def _size_modal_closed(self) -> bool:
        # 确定按钮会替换整个弹窗节点，关闭判断必须重新查询页面而不是读取旧节点状态。
        return self._locate_size_modal() is None

    def _size_option_labels(self) -> list[Any]:
        # 尺码表弹窗之外的商品规格区负责真正生成颜色×尺码销售规格。
        boxes = self._visible_elements(
            "//main//*[contains(concat(' ',normalize-space(@class),' '),' size-box ')]"
        )
        if len(boxes) != 1:
            raise AutomationError(f"商品规格尺码容器数量异常：{len(boxes)}")
        return self._visible_elements(
            ".//label[.//input[@type='checkbox']]",
            scope=boxes[0],
        )

    def _size_option_is_checked(self, size: str) -> bool:
        normalized = re.sub(r"\s+", "", size)
        return any(
            re.sub(r"\s+", "", label.text) == normalized
            and _label_is_checked(label)
            for label in self._size_option_labels()
        )

    def _select_product_sizes(self) -> None:
        # 尺码测量表与商品销售规格是两个控件；保存测量表后还必须勾选商品实际销售尺码。
        labels = self._size_option_labels()
        source_sizes = set(self.product.sizes)
        selected_extra = {
            re.sub(r"\s+", "", label.text)
            for label in labels
            if re.sub(r"\s+", "", label.text) not in {"", "全选"}
            and _label_is_checked(label)
            and re.sub(r"\s+", "", label.text) not in source_sizes
        }
        if selected_extra:
            raise AutomationError(
                f"页面已选来源之外的商品尺码：{sorted(selected_extra)}；程序不会自动取消"
            )

        for size in self.product.sizes:
            normalized = re.sub(r"\s+", "", size)
            current_labels = [
                label
                for label in self._size_option_labels()
                if re.sub(r"\s+", "", label.text) == normalized
            ]
            if len(current_labels) != 1:
                raise AutomationError(f"商品规格中找不到唯一尺码选项：{size}")
            if _label_is_checked(current_labels[0]):
                continue
            self._click(current_labels[0])
            self._wait_until(
                lambda size=size: self._size_option_is_checked(size),
                message=f"商品规格尺码没有选中：{size}",
            )

    def _wait_for_size_modal(self) -> Any:
        # 打开按钮点击后使用统一等待器，避免后续立即查询到尚未挂载的弹窗。
        return self._wait_until(
            self._locate_size_modal,
            message="添加尺码表抽屉没有打开",
        )

    def _configure_size_columns(
        self,
        modal: Any,
        requested_parameters: Sequence[str],
    ) -> None:
        # 先收集弹窗提供的标准列名，再关闭未请求列、打开请求列，最后等待表头回显。
        # “尺码”列始终必须存在，即使 SIZE_CHART 为空也要能填写尺码名称。
        labels = self._visible_elements(
            ".//label[.//input[@type='checkbox']]",
            scope=modal,
        )
        available: dict[str, str] = {}
        for label in labels:
            text = re.sub(r"\s+", "", label.text)
            canonical = _canonical_size_label(text)
            if canonical == "尺码" or canonical.endswith(("(cm)", "(kg)")):
                available[canonical] = text

        if "尺码" not in available:
            raise AutomationError("尺码表抽屉中找不到“尺码”复选框")

        requested = {_canonical_size_label(value) for value in requested_parameters}
        missing = requested - set(available)
        if missing:
            raise AutomationError(f"尺码表不存在这些参数列：{sorted(missing)}")

        for canonical, label in available.items():
            self._set_modal_checkbox(
                modal,
                label,
                canonical == "尺码" or canonical in requested,
            )

        self._wait_until(
            lambda: {"尺码", *requested}.issubset(
                {
                    _canonical_size_label(item.text)
                    for item in self._visible_elements(
                        ".//table[.//th[contains(.,'操作')]]//thead//th",
                        scope=modal,
                    )
                }
            ),
            message="等待尺码表列更新超时",
        )

    def _set_modal_checkbox(self, modal: Any, label: str, checked: bool) -> None:
        # 只有当前状态与目标状态不一致时才点击，避免重复点击导致复选框反选。
        literal = _xpath_literal(label)
        labels = self._visible_elements(
            f".//label[normalize-space(.)={literal} or .//*[normalize-space(.)={literal}]]",
            scope=modal,
        )
        if not labels:
            if label == "尺码":
                raise AutomationError("尺码表弹窗中找不到“尺码”复选框")
            return
        checkbox = labels[0].ele("xpath:.//input[@type='checkbox']", timeout=1)
        current = bool(checkbox.states.is_checked) if checkbox else False
        if current != checked:
            self._click(labels[0])

            # 点击后重新获取节点，因为前端可能替换整个 label/checkbox 元素。
            def has_expected_state() -> bool:
                current_labels = self._visible_elements(
                    f".//label[normalize-space(.)={literal} or .//*[normalize-space(.)={literal}]]",
                    scope=modal,
                )
                if not current_labels:
                    return False
                current_checkbox = current_labels[0].ele(
                    "xpath:.//input[@type='checkbox']",
                    timeout=1,
                )
                return bool(current_checkbox and current_checkbox.states.is_checked) == checked

            self._wait_until(
                has_expected_state,
                message=f"尺码表复选框状态没有更新：{label}",
            )

    def _size_table(self, modal: Any) -> Any:
        # 弹窗中可能存在隐藏模板表格，选择带“操作”表头且当前可见的最后一张。
        tables = self._visible_elements(".//table[.//th[contains(.,'操作')]]", scope=modal)
        if not tables:
            raise AutomationError("尺码表弹窗中找不到尺码表格")
        return tables[-1]

    def _size_rows(self, table: Any) -> list[Any]:
        # Ant Design 会在 tbody 中保留一个 aria-hidden 的零高度测量行，不能把它算作尺码数据行。
        return [
            row
            for row in self._visible_elements(".//tbody/tr", scope=table)
            if _is_size_data_row(row)
        ]

    def _ensure_size_rows(self, table: Any, expected: int) -> None:
        # 只能新增缺少的空行；如果现有行超过来源数量，不自动删除，避免破坏人工数据。
        while True:
            rows = self._size_rows(table)
            if len(rows) > expected:
                raise AutomationError(
                    f"尺码表已有 {len(rows)} 行，超过来源 {expected} 行；"
                    "请手工删除多余尺码后重试"
                )
            if len(rows) >= expected:
                return
            if not rows:
                self._wait_until(
                    lambda: bool(self._size_rows(table)),
                    message="等待尺码表初始行超时",
                )
                continue
            add_controls = self._visible_elements(
                ".//*[contains(@class,'plus-circle') or @alt='plus-circle']",
                scope=rows[-1],
            )
            if not add_controls:
                raise AutomationError(
                    f"尺码表只有 {len(rows)} 行，且找不到新增行按钮；需要 {expected} 行"
                )
            before = len(rows)
            self._click(add_controls[-1])
            self._wait_until(
                lambda: len(self._size_rows(table)) == before + 1
            )

    def _sku_variant_rows_present(self) -> bool:
        # 尺码表关闭后，页面 SKU 行不能再出现“--”这类未生成的占位尺码。
        keys = {
            (cells[0].text.strip(), cells[1].text.strip())
            for row in self._sku_rows()
            for cells in [row.eles("xpath:./td")]
            if len(cells) >= 2
        }
        return bool(keys) and all(size != "--" for _, size in keys)

    def _sku_rows(self) -> list[Any]:
        # 通过至少 9 个输入框过滤出真正的 SKU 行，排除表头、隐藏行和其他表格行。
        rows = self._visible_elements("//main//tr[contains(@class,'el-table__row')]")
        result: list[Any] = []
        for row in rows:
            cells = row.eles("xpath:./td")
            inputs = row.eles("xpath:.//input")
            if len(cells) < 2 or len(inputs) < 9:
                continue
            first_class = str(cells[0].attr("class") or "")
            if "is-hidden" in first_class:
                continue
            color = cells[0].text.strip()
            size = cells[1].text.strip()
            if color or size:
                result.append(row)
        return result

    def _sku_pagination(self) -> Any:
        # 页面只应有一个包含总条数的销售规格分页器；数量异常说明页面结构不符合预期。
        paginations = self._visible_elements(
            "//main//div[contains(@class,'el-pagination')][contains(normalize-space(.),'共')"
            " and contains(normalize-space(.),'条')]"
        )
        if len(paginations) != 1:
            raise AutomationError(f"销售规格分页器数量异常：{len(paginations)}")
        return paginations[0]

    def _sku_total(self) -> int:
        # 从分页器的“共 N 条”文本读取总 SKU 数，用于和来源笛卡尔积做硬校验。
        text = re.sub(r"\s+", " ", self._sku_pagination().text)
        match = re.search(r"共\s*(\d+)\s*条", text)
        if not match:
            raise AutomationError(f"无法读取销售规格总数：{text}")
        return int(match.group(1))

    def _go_to_first_sku_page(self) -> None:
        # 每次任务从第 1 页开始，保证分页遍历的起点确定。
        pagination = self._sku_pagination()
        active = self._visible_elements(
            ".//li[contains(@class,'number') and contains(@class,'active')]",
            scope=pagination,
        )
        if active and active[0].text.strip() == "1":
            return
        first_page = self._find_visible(
            ".//li[contains(@class,'number') and normalize-space(.)='1']",
            "销售规格第 1 页",
            scope=pagination,
        )
        self._click(first_page)
        self._wait_until(
            lambda: any(
                item.text.strip() == "1"
                for item in self._visible_elements(
                    ".//li[contains(@class,'number') and contains(@class,'active')]",
                    scope=self._sku_pagination(),
                )
            ),
            message="销售规格无法切回第 1 页",
        )

    def _next_page_button(self) -> Any | None:
        # 返回下一页按钮；是否可点击由调用方结合 disabled 状态判断。
        buttons = self._visible_elements(
            ".//button[contains(@class,'btn-next')]",
            scope=self._sku_pagination(),
        )
        return buttons[0] if buttons else None

    def _carousel_row(self, color: str) -> Any:
        # 颜色文本和文件上传控件共同限定目标行，防止同名普通文本行被选中。
        literal = _xpath_literal(color)
        rows = self._visible_elements(
            "//main//tr[.//input[@type='file']]"
            f"[./td[1][contains(normalize-space(.),{literal})]]"
            "[contains(.,'上传图片')]"
        )
        if len(rows) != 1:
            raise AutomationError(f"颜色“{color}”轮播图行数量异常：{len(rows)}")
        return rows[0]

    def _wait_carousel_count(self, color: str, expected: int) -> None:
        # 上传控件调用成功不等于服务器处理完成，必须以页面计数达到目标为准。
        self._wait_until(
            lambda: self._carousel_image_count(self._carousel_row(color)) == expected,
            timeout=self.settings.upload_timeout,
            message=f"等待颜色“{color}”上传到 {expected} 张超时",
        )

    def _carousel_image_count(self, row: Any) -> int:
        # 轮播图数量从行首“已上传 N 张”文本中提取。
        cells = row.eles("xpath:./td")
        if not cells:
            raise AutomationError("轮播图行没有单元格")
        return _image_count(cells[0].text)

    def _read_visible_errors(self) -> list[str]:
        # 只读取当前可见的表单错误，并把父节点文本压成单行，便于终端 JSON 输出和人工定位。
        errors: list[str] = []
        for element in self._visible_elements(
            "//*[contains(@class,'form-item__error')]"
            " | //*[normalize-space(.)='填写有误']"
        ):
            text = element.text.strip()
            parent_text = element.parent(1).text.strip() if element.parent(1) else text
            candidate = re.sub(r"\s+", " ", parent_text or text)
            if candidate and candidate not in errors:
                errors.append(candidate[:240])
        return errors

    def _find_visible(
        self,
        xpath: str,
        description: str,
        *,
        scope: Any | None = None,
    ) -> Any:
        # 大多数页面操作都必须作用于可见元素；找不到时统一抛出带业务描述的错误。
        elements = self._visible_elements(xpath, scope=scope)
        if not elements:
            raise AutomationError(f"找不到{description}")
        return elements[0]

    def _find_any(
        self,
        xpath: str,
        description: str,
        *,
        scope: Any | None = None,
    ) -> Any:
        # 少数场景（例如文件 input）不要求 visible 过滤，但仍统一处理 XPath 前缀和空结果。
        locator = xpath if xpath.startswith("xpath:") else f"xpath:{xpath}"
        owner = scope or self.tab
        try:
            elements = owner.eles(locator, timeout=1)
        except TypeError:
            elements = owner.eles(locator)
        if not elements:
            raise AutomationError(f"找不到{description}")
        return elements[0]

    def _visible_elements(self, xpath: str, *, scope: Any | None = None) -> list[Any]:
        # scope 用于把查找限制在当前表单项/弹窗/图片区，避免全页面同名元素相互干扰。
        locator = xpath if xpath.startswith("xpath:") else f"xpath:{xpath}"
        owner = scope or self.tab
        try:
            elements = owner.eles(locator, timeout=1)
        except TypeError:
            elements = owner.eles(locator)
        return [element for element in elements if _is_displayed(element)]

    def _click(self, element: Any) -> None:
        # 点击前滚动到视口，降低固定头部或懒加载导致的点击失败概率。
        self._scroll(element)
        element.click(timeout=self.settings.timeout)

    def _scroll(self, element: Any) -> None:
        # 滚动只是辅助动作；部分元素不支持 scroll API 时不影响主流程继续尝试点击。
        try:
            element.scroll.to_see(center=True)
        except Exception:
            pass

    def _wait_until(
        self,
        predicate: Callable[[], Any],
        *,
        timeout: float | None = None,
        message: str = "等待页面状态变化超时",
    ) -> Any:
        # 页面操作大量依赖异步渲染，因此统一采用短间隔轮询，并保留最后一次异常用于诊断。
        deadline = time.monotonic() + (timeout or self.settings.timeout)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except Exception as error:
                last_error = error
            time.sleep(0.15)
        if last_error:
            raise AutomationError(f"{message}：{last_error}") from last_error
        raise AutomationError(message)


def _connect_to_chrome(port: int) -> Any:
    # 只连接已经由人工启动、登录的 Chrome，不负责自动登录或处理验证码。
    command = _chrome_start_command(port)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    except OSError as error:
        raise AutomationError(
            f"Chrome 调试端口 {port} 未开放。先运行：\n{command}\n"
            "然后在这个 Chrome 中登录得物并打开新品草稿页。"
        ) from error

    try:
        from DrissionPage import Chromium, ChromiumOptions

        options = ChromiumOptions().set_address(f"127.0.0.1:{port}").existing_only()
        browser = Chromium(options)
    except Exception as error:
        raise AutomationError(
            f"无法连接 Chrome 调试端口 {port}。先运行：\n{command}\n"
            "然后在这个 Chrome 中登录得物并打开新品草稿页。"
        ) from error
    return browser


def _activate_dewu_tab(tab: Any, description: str) -> Any:
    # DrissionPage 的键盘输入依赖当前激活标签页，后台标签可能导致输入无异常但页面不回显。
    try:
        tab.set.activate()
    except Exception as error:
        raise AutomationError(f"无法激活{description}，请将该页面切到前台后重试") from error
    return tab


def attach_to_dewu_start_tab(port: int) -> tuple[Any, Any]:
    # 起始页是每次新品申请的固定入口；多个候选时停止，避免使用半填写页面。
    browser = _connect_to_chrome(port)
    candidates = [
        tab for tab in browser.get_tabs() if _is_start_page_url(str(tab.url))
    ]
    if len(candidates) > 1:
        raise AutomationError(
            f"找到 {len(candidates)} 个得物新品申请起始页；请只保留本次要填写的一个空白页面"
        )
    if candidates:
        return browser, _activate_dewu_tab(candidates[0], "得物新品申请起始页")

    try:
        tab = browser.new_tab(START_PAGE_URL)
    except Exception as error:
        raise AutomationError(f"无法打开得物新品申请起始页：{START_PAGE_URL}") from error
    return browser, _activate_dewu_tab(tab, "得物新品申请起始页")


def attach_to_dewu_tab(port: int) -> tuple[Any, Any]:
    # 保留详情页连接入口，供已有草稿调试或兼容调用使用；主流程从起始页入口开始。
    browser = _connect_to_chrome(port)
    candidates = [
        tab for tab in browser.get_tabs() if _is_detail_page_url(str(tab.url))
    ]
    if not candidates:
        raise AutomationError("已连接 Chrome，但没有找到打开的得物“申请新品/编辑草稿”标签页")
    if len(candidates) > 1:
        raise AutomationError(
            f"找到 {len(candidates)} 个得物新品草稿标签页；请只保留本次要填写的一个页面"
        )
    return browser, _activate_dewu_tab(candidates[0], "得物新品草稿标签页")


def _chrome_start_command(port: int) -> str:
    # 使用脚本目录下独立 profile，避免影响用户日常 Chrome 配置和登录状态。
    profile = Path(__file__).resolve().parent / ".chrome-profile"
    return (
        'open -na "Google Chrome" --args '
        f'--remote-debugging-port={port} --user-data-dir="{profile}"'
    )


def main() -> int:
    # 主入口先完成无浏览器的数据预检；只有传入 --execute 才连接 Chrome。
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    result: RunResult | None = None
    try:
        json_path = _resolve_input(args.json, script_dir, "*.json", "JSON")
        zip_path = _resolve_input(args.images, script_dir, "*.zip", "ZIP")
        work_dir = (args.work_dir or script_dir / ".dewu_work").resolve()

        # 先把来源转换为不可变 ProductData，再依次应用配置文件和命令行属性覆盖。
        product = load_product(json_path)
        if ATTRIBUTE_OVERRIDES:
            product = replace(
                product,
                attributes={**product.attributes, **ATTRIBUTE_OVERRIDES},
            )
        product = _with_attribute_overrides(product, args.attribute)
        # 图片在此阶段完成解压、引用解析和页面大小/格式预检，浏览器阶段只接收合格路径。
        media = extract_and_resolve_media(product, zip_path, work_dir)
        _validate_media_for_page(product, media)

        _validate_runtime_inputs(args)
        external_link = product.external_link
        settings = RunSettings(
            debugger_port=args.port,
            offer_type=args.offer_type,
            price_proof_source=args.price_proof_source,
            release_proof_source=args.release_proof_source,
            external_link=external_link,
            save_draft=not args.no_save,
            skip_size_chart=args.skip_size_chart,
            timeout=args.timeout,
        )

        # 无 --execute 时只输出摘要，便于先检查标题、SKU、图片数量和警告。
        _print_preflight(
            product,
            media,
            json_path,
            zip_path,
            execute=args.execute,
            external_link=external_link,
        )
        if not args.execute:
            return 0

        result = RunResult(warnings=_effective_warnings(product, media))
        # 从这里开始才会产生浏览器外部副作用；异常会被转换为结构化结果返回。
        browser, start_tab = attach_to_dewu_start_tab(args.port)
        detail_tab = DewuStartPage(browser, start_tab, media, settings, result).run()
        result = DewuAutomation(detail_tab, product, media, settings, result).run()
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0 if result.status in {"draft_saved", "filled_not_saved"} else 3
    except (ProductDataError, AutomationError) as error:
        status = "needs_input" if isinstance(error, ProductDataError) else "paused_for_user"
        if result is not None:
            result.status = status
            if str(error) not in result.validation_errors:
                result.validation_errors.append(str(error))
            payload: Mapping[str, Any] = asdict(result)
        else:
            payload = {
                "status": status,
                "error": str(error),
                "submission": "not_attempted",
            }
        print(
            json.dumps(payload, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        print("\n已由用户中止，未尝试提交审核。", file=sys.stderr)
        return 130
    except Exception as error:
        message = f"未预期错误 {type(error).__name__}: {error}"
        if result is not None:
            result.status = "paused_for_user"
            result.validation_errors.append(message)
            payload = asdict(result)
        else:
            payload = {
                "status": "paused_for_user",
                "error": message,
                "submission": "not_attempted",
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 4


def _resolve_input(explicit: Path | None, directory: Path, pattern: str, label: str) -> Path:
    # 显式指定路径时优先使用它；未指定时要求目录中只能有一个候选文件，
    # 避免多个商品数据并存时按文件名顺序误选。
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise ProductDataError(f"{label} 文件不存在：{path}")
        return path
    candidates = sorted(path for path in directory.glob(pattern) if path.is_file())
    if len(candidates) != 1:
        raise ProductDataError(
            f"当前目录应当恰好有一个 {label} 文件，实际 {len(candidates)} 个；请显式传入路径"
        )
    return candidates[0].resolve()


def _validate_runtime_inputs(args: argparse.Namespace) -> None:
    # 在接触浏览器前集中校验端口、超时时间和证明渠道，
    # 让执行阶段只处理已经满足基本约束的参数。
    if not 1 <= args.port <= 65_535:
        raise ProductDataError(f"Chrome 调试端口必须在 1-65535 之间：{args.port}")
    if args.timeout <= 0:
        raise ProductDataError(f"timeout 必须大于 0：{args.timeout}")
    if not str(args.offer_type).strip():
        raise ProductDataError("offer-type 不能为空")
    if not str(args.price_proof_source).strip():
        raise ProductDataError("price-proof-source 不能为空")
    if not str(args.release_proof_source).strip():
        raise ProductDataError("release-proof-source 不能为空")

def _validate_media_for_page(product: ProductData, media: MediaFiles) -> None:
    # models.py 只负责找到文件；这里按得物页面限制检查格式、数量和单文件大小。
    # 预先拒绝不合格文件可以避免上传到一半才发现页面限制。
    allowed_suffixes = {".jpg", ".jpeg", ".png"}
    if not media.first_square:
        raise ProductDataError("来源图片没有第一张方图，无法填写申请新品起始页")
    first_square = media.first_square[0]
    if first_square.suffix.casefold() not in allowed_suffixes:
        raise ProductDataError(f"第一张方图格式不支持：{first_square}")

    for color in product.colors:
        files = media.carousel_by_color[color]
        if len(files) > MAX_CAROUSEL_PER_COLOR:
            raise ProductDataError(
                f"颜色“{color}”轮播图有 {len(files)} 张，页面上限为 {MAX_CAROUSEL_PER_COLOR} 张"
            )
        for path in files:
            if path.suffix.casefold() not in allowed_suffixes:
                raise ProductDataError(f"轮播图格式不支持：{path}")
            if path.stat().st_size > MAX_CAROUSEL_FILE_BYTES:
                raise ProductDataError(f"轮播图超过 5 MB：{path}")

    # 三个图片区块的上限不同，但格式和文件大小规则相同，因此统一遍历。
    sections = (
        ("商品展示", media.product_display_backs, MAX_PRODUCT_DISPLAY_IMAGES),
        ("细节呈现", media.details, MAX_DETAIL_IMAGES),
        ("穿搭效果", media.outfit_fronts, MAX_OUTFIT_IMAGES),
    )
    for label, files, maximum in sections:
        if len(files) > maximum:
            raise ProductDataError(f"{label}有 {len(files)} 张图，页面上限为 {maximum} 张")
        for path in files:
            if path.suffix.casefold() not in allowed_suffixes:
                raise ProductDataError(f"{label}图片格式不支持：{path}")
            if path.stat().st_size > MAX_DETAIL_FILE_BYTES:
                raise ProductDataError(f"{label}图片超过 20 MB：{path}")


def _with_attribute_overrides(product: ProductData, raw_overrides: Sequence[str]) -> ProductData:
    # 将重复出现的 --attribute 参数按“字段=值1,值2”解析，并返回新的 ProductData。
    # 使用 replace 保持原对象不可变，且不影响来源解析得到的其他字段。
    if not raw_overrides:
        return product
    parsed = dict(product.attributes)
    for raw in raw_overrides:
        # 后面的同名覆盖会替换前面的值，命令行顺序即最终生效顺序。
        if "=" not in raw:
            raise ProductDataError(f"属性覆盖格式错误：{raw}，应为 字段=值1,值2")
        label, raw_values = raw.split("=", 1)
        values = tuple(value.strip() for value in raw_values.split(",") if value.strip())
        if not label.strip() or not values:
            raise ProductDataError(f"属性覆盖不能为空：{raw}")
        parsed[label.strip()] = values
    return replace(product, attributes=parsed)


def _print_preflight(
    product: ProductData,
    media: MediaFiles,
    json_path: Path,
    zip_path: Path,
    *,
    execute: bool,
    external_link: str,
) -> None:
    # 预检摘要是人工确认入口：展示来源、标题、规格、价格、图片数量、警告和下一步动作。
    # 这里不连接浏览器，也不修改页面。
    offer_prices = sorted({_number_text(sku.offer_amount) for sku in product.skus})
    inventory_values = sorted({sku.inventory for sku in product.skus})
    summary = {
        "status": "ready_to_execute" if execute else "dry_run_ok",
        "source_json": str(json_path),
        "image_zip": str(zip_path),
        "product": {
            "source_id": product.source_id,
            "code": product.code,
            "brand": product.brand,
            "category_path": list(product.category_path),
            "item_no": product.item_no,
            "release_price": _number_text(product.release_price),
            "release_season": product.release_season,
            "title": asdict(product.title),
            "colors": list(product.colors),
            "sizes": list(product.sizes),
            "sku_count": len(product.skus),
            "sku_offer_prices": offer_prices,
            "sku_inventory": inventory_values,
            "total_sku_inventory": sum(sku.inventory for sku in product.skus),
            "attributes": {
                label: list(values)
                for label, values in product.attributes.items()
            },
            "inferred_attributes": list(product.inferred_attributes),
            "external_link": external_link,
            "external_link_ready": bool(external_link.strip()),
        },
        "media": {
            "extraction_root": str(media.extraction_root),
            "first_square": len(media.first_square),
            "carousel_colors": {color: len(paths) for color, paths in media.carousel_by_color.items()},
            "product_display_backs": len(media.product_display_backs),
            "details": len(media.details),
            "outfit_fronts": len(media.outfit_fronts),
        },
        "warnings": _effective_warnings(product, media),
        "next_action": (
            "即将连接浏览器，只保存草稿，不提交审核"
            if execute
            else "核对摘要后加 --execute；首次建议同时加 --no-save"
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def _effective_warnings(
    product: ProductData,
    media: MediaFiles,
) -> list[str]:
    # 合并来源解析、图片解析和当前配置产生的警告。
    warnings = list(product.warnings) + list(media.warnings)
    if not SIZE_CHART:
        warnings.append(
            f"SIZE_CHART 为空，本次只填写 {'/'.join(product.sizes)} 尺码名称；"
            "若页面强制要求测量参数，程序会停在尺码抽屉并报告错误"
        )
    if not any(value not in (None, "") for value in PACKAGE_DEFAULTS.values()):
        warnings.append(
            "PACKAGE_DEFAULTS 为空，SKU 包装长宽高和重量不会写入；"
            "若当前类目强制要求，请先在 main.py 顶部补充真实值"
        )
    return warnings


def _is_start_page_url(url: str) -> bool:
    # 起始页和详情页共用同一个商家域名，必须同时校验域名和路径片段。
    parsed = urlparse(str(url))
    return parsed.hostname == TARGET_HOST and START_PATH_FRAGMENT in parsed.path


def _is_detail_page_url(url: str) -> bool:
    parsed = urlparse(str(url))
    return parsed.hostname == TARGET_HOST and TARGET_PATH_FRAGMENT in parsed.path


def _new_detail_tabs(tabs: Sequence[Any], before_urls: set[str]) -> list[Any]:
    # 创建申请后只接受此前不存在的新详情页，避免误用旧调试草稿。
    return [
        tab
        for tab in tabs
        if _is_detail_page_url(str(tab.url)) and str(tab.url) not in before_urls
    ]


def _validate_start_page_state(fields: Mapping[str, str], image_count: int) -> None:
    # 起始页只能从空白状态开始，任何残留值都不自动覆盖或删除。
    residual = [
        f"{label}={str(value).strip()}"
        for label, value in fields.items()
        if str(value or "").strip()
    ]
    if image_count != 0:
        residual.append(f"商品图片={image_count}张")
    if residual:
        raise AutomationError(
            "申请新品起始页不是空白页，程序不会覆盖已有内容：" + "；".join(residual)
        )


def _start_category_value_matches(value: str) -> bool:
    # Element Cascader 的回显使用“>>”，部分页面版本会使用斜杠或插入空白。
    parts = tuple(
        part.strip()
        for part in re.split(r"\s*(?:>>|/|／)\s*", str(value).strip())
        if part.strip()
    )
    return parts == START_CATEGORY_PATH


def _is_displayed(element: Any) -> bool:
    # DrissionPage 元素状态读取可能因节点失效而抛异常；失效节点按不可见处理。
    try:
        return bool(element and element.states.is_displayed)
    except Exception:
        return False


def _has_layout(element: Any) -> bool:
    # 下拉框会保留 display:none 的模板节点，只有有实际尺寸的节点才可点击。
    try:
        width, height = element.rect.size
        return width > 0 and height > 0
    except Exception:
        return False


def _is_size_data_row(element: Any) -> bool:
    # Ant Design 表格的测量行可能被 DrissionPage 误报为 displayed，需额外检查语义和布局尺寸。
    try:
        if str(element.attr("aria-hidden") or "").casefold() == "true":
            return False
        classes = set(str(element.attr("class") or "").split())
        if "ant-table-measure-row" in classes:
            return False
        width, height = element.rect.size
        return width > 0 and height > 0
    except Exception:
        return False


def _element_value(element: Any) -> str:
    # 受控输入框的实时内容在 DOM property 中，HTML attribute 可能仍保留初始值。
    try:
        value = element.property("value")
    except Exception:
        value = None
    if value is None:
        try:
            value = element.attr("value")
        except Exception:
            value = ""
    return str(value or "").strip()


def _is_disabled(element: Any) -> bool:
    # 同时兼容 HTML disabled、aria-disabled、CSS 类和浏览器状态四种禁用表达方式。
    try:
        if element.attr("disabled") is not None:
            return True
        if str(element.attr("aria-disabled") or "").casefold() == "true":
            return True
        classes = set(str(element.attr("class") or "").split())
        if classes.intersection({"disabled", "is-disabled"}):
            return True
        return not bool(element.states.is_enabled)
    except Exception:
        return False


def _label_is_checked(label: Any) -> bool:
    # 单选/复选控件既可能有真实 input 状态，也可能只在外层 label 上维护 CSS 状态。
    try:
        control = label.ele(
            "xpath:.//input[@type='radio' or @type='checkbox']",
            timeout=1,
        )
        if control and bool(control.states.is_checked):
            return True
        classes = set(str(label.attr("class") or "").split())
        return bool(classes.intersection({"is-checked", "ant-radio-wrapper-checked", "ant-checkbox-wrapper-checked"}))
    except Exception:
        return False


def _xpath_literal(value: str) -> str:
    # 把任意文本安全转换成 XPath 字符串字面量，尤其处理同时包含单双引号的中文文本。
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


def _number_text(value: Decimal) -> str:
    # 去除 Decimal 的指数表示和无意义尾随零，生成适合输入框的普通数字文本。
    normalized = value.normalize()
    return format(normalized, "f")


def _image_count(text: str) -> int:
    # 从图片区文本中提取“数字 + 张”；页面文案变化或缺失时按 0 处理。
    match = re.search(r"(\d+)\s*张", text)
    return int(match.group(1)) if match else 0


def _uploaded_count(text: str) -> int:
    # 从“已上传 N/...”文案读取当前区块上传数量。
    match = re.search(r"已上传\s*(\d+)\s*/", text)
    return int(match.group(1)) if match else 0


def _clean_header_text(text: str) -> str:
    # 统一尺码表表头中的空白、全角括号和“区间值”后缀，便于配置按表头匹配。
    return re.sub(r"\s+", "", text).replace("（", "(").replace("）", ")").replace("区间值", "")


def _canonical_size_label(text: str) -> str:
    # 预留统一入口；当前规范化规则与普通表头清洗相同。
    return _clean_header_text(text)


def _lookup_size_value(values: Mapping[str, str], header: str) -> str | None:
    # 配置表头与页面表头可能只存在全角符号或空白差异，因此比较规范化后的名称。
    normalized = _canonical_size_label(header)
    for key, value in values.items():
        if _canonical_size_label(key) == normalized:
            return value
    return None


if __name__ == "__main__":
    raise SystemExit(main())
