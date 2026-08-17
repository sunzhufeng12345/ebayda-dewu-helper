from decimal import Decimal
from unittest import TestCase

from models import ProductDataError, _build_dewu_attributes, _build_skus, _build_title


def sku(price: object) -> dict[str, object]:
    return {
        "code": "CM07-黑色M",
        "price": price,
        "stock": 0,
        "attributes": [
            {"attributeName": "颜色", "attributeValue": "黑色"},
            {"attributeName": "尺码", "attributeValue": "M"},
        ],
    }


class BuildSkusTest(TestCase):
    def test_missing_source_price_uses_zero_for_diagnostics(self) -> None:
        _, _, skus, _ = _build_skus(
            [sku(None)],
            code="CM07",
            release_price=Decimal("99"),
        )

        self.assertEqual(skus[0].source_price, Decimal("0"))
        self.assertEqual(skus[0].offer_amount, Decimal("99"))

    def test_invalid_source_price_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProductDataError, "price .*合法数字"):
            _build_skus(
                [sku("not-a-price")],
                code="CM07",
                release_price=Decimal("99"),
            )


class BuildDewuAttributesTest(TestCase):
    def test_round_collar_is_inferred_from_product_name(self) -> None:
        attributes, inferred = _build_dewu_attributes(
            "帕丁顿熊 春秋冬季潮流手写字体印花圆领套头卫衣",
            "春",
            {},
            (),
        )

        self.assertEqual(attributes["领型"], ("圆领",))
        self.assertIn("领型", inferred)

    def test_fabric_percent_suffix_is_stripped(self) -> None:
        # 来源材质带百分比（多行各一条）时，面料应只保留成分名并丢弃兜底词。
        attributes, _ = _build_dewu_attributes(
            "测试商品",
            "春",
            {"材质": ("棉 - 30", "涤纶(聚酯纤维) - 67", "其他 - 3")},
            (),
        )

        self.assertEqual(attributes["面料"], ("棉", "聚酯纤维"))

    def test_fabric_combined_string_is_split(self) -> None:
        # 来源把多成分连写在一个值里时同样要拆开并归一。
        attributes, _ = _build_dewu_attributes(
            "测试商品",
            "春",
            {"材质": ("棉 - 30、涤纶(聚酯纤维) - 67、其他 - 3",)},
            (),
        )

        self.assertEqual(attributes["面料"], ("棉", "聚酯纤维"))

    def test_fabric_filler_only_is_kept(self) -> None:
        # 来源只有"其他"时保留原值，让页面报错暴露数据问题而不是悄悄留空。
        attributes, _ = _build_dewu_attributes(
            "测试商品",
            "春",
            {"材质": ("其它 - 100",)},
            (),
        )

        self.assertEqual(attributes["面料"], ("其它",))

    def test_composition_joins_all_components_with_percentages(self) -> None:
        # 成分含量是文本字段：每个成分都保留占比，名称与面料下拉同一套归一规则。
        attributes, _ = _build_dewu_attributes(
            "测试商品",
            "春",
            {"材质": ("棉 - 30、涤纶(聚酯纤维) - 67、其他 - 3",)},
            (
                {"attributeCode": "caizhi", "attributeValue": "棉 - 30", "subValueNumber": 30},
                {"attributeCode": "caizhi", "attributeValue": "涤纶(聚酯纤维) - 67", "subValueNumber": 67},
                {"attributeCode": "caizhi", "attributeValue": "其他 - 3", "subValueNumber": 3},
            ),
        )

        self.assertEqual(attributes["成分含量"], ("棉30%、聚酯纤维67%、其他3%",))

    def test_composition_percentage_falls_back_to_sub_value_number(self) -> None:
        # 值文本没有「- 30」后缀时，占比退回行级 subValueNumber。
        attributes, _ = _build_dewu_attributes(
            "测试商品",
            "春",
            {"材质": ("棉", "涤纶")},
            (
                {"attributeCode": "caizhi", "attributeValue": "棉", "subValueNumber": 95},
                {"attributeCode": "caizhi", "attributeValue": "涤纶", "subValueNumber": 5},
            ),
        )

        self.assertEqual(attributes["成分含量"], ("棉95%、聚酯纤维5%",))

    def test_fabric_alias_and_dedup(self) -> None:
        # 同义写法映射为得物标准选项，且去重保持顺序。
        attributes, _ = _build_dewu_attributes(
            "测试商品",
            "春",
            {"材质": ("纯棉 - 60", "涤纶 - 40", "聚酯纤维 - 5")},
            (),
        )

        self.assertEqual(attributes["面料"], ("棉", "聚酯纤维"))

    def test_fabric_unknown_value_passes_through(self) -> None:
        # 映射表外的值原样保留，由页面步骤报错暴露新词，而不是悄悄丢弃。
        attributes, _ = _build_dewu_attributes(
            "测试商品",
            "春",
            {"材质": ("太空纤维 - 100",)},
            (),
        )

        self.assertEqual(attributes["面料"], ("太空纤维",))


class BuildTitleTest(TestCase):
    def test_short_catalog_name_uses_source_style_to_reach_platform_minimum(self) -> None:
        title = _build_title(
            "休闲裤",
            "帕丁顿熊",
            "男士休闲直筒裤",
            {"风格": ("日韩风", "百搭风")},
        )

        combined = "".join(("帕丁顿熊", title.selling_point, title.category, title.audience))
        self.assertGreaterEqual(len(combined), 16)
        self.assertIn("日韩风", title.selling_point)
        self.assertIn("百搭风", title.selling_point)
