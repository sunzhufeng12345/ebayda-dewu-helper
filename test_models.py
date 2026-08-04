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
