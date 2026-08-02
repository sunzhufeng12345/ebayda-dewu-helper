from decimal import Decimal
from unittest import TestCase

from models import ProductDataError, _build_skus


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
