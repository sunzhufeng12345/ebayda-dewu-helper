from decimal import Decimal
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zipfile import ZipFile

from models import (
    ProductDataError,
    _build_dewu_attributes,
    _build_media_references,
    _build_skus,
    _build_title,
    extract_and_resolve_media,
    load_product,
)


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


class OptionalColorDewuMediaTest(TestCase):
    def test_empty_legacy_color_image_array_is_treated_as_no_images(self) -> None:
        references = _build_media_references({"colorDewuPaths": []})

        self.assertEqual(references.carousel_by_color, {})

    def test_color_images_follow_source_order_without_requiring_two_files(self) -> None:
        source = load_product(Path(__file__).with_name("1632.json"))
        colors = ("无图", "一张", "两张", "四张")
        color_paths = {
            "一张": ("one-1.jpg",),
            "两张": ("two-1.jpg", "two-2.jpg"),
            "四张": ("four-1.jpg", "four-2.jpg", "four-3.jpg", "four-4.jpg"),
        }
        product = replace(
            source,
            colors=colors,
            media_references=replace(
                source.media_references,
                main=(),
                details=(),
                first_square=("start.jpg",),
                first_long=(),
                carousel_by_color=color_paths,
            ),
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            zip_path = root / "images.zip"
            with ZipFile(zip_path, "w") as archive:
                archive.writestr("fixture/第一张方图/start.jpg", b"start")
                for paths in color_paths.values():
                    for path in paths:
                        archive.writestr(f"fixture/颜色图_得物平铺图/{path}", path.encode())

            media = extract_and_resolve_media(product, zip_path, root / "work")

        self.assertEqual(media.carousel_by_color["无图"], ())
        self.assertEqual(
            tuple(path.name for path in media.carousel_by_color["四张"]),
            ("four-1.jpg", "four-2.jpg", "four-3.jpg", "four-4.jpg"),
        )
        self.assertEqual(
            tuple(path.name for path in media.product_display_backs),
            ("two-2.jpg", "four-2.jpg"),
        )
        self.assertEqual(
            tuple(path.name for path in media.outfit_fronts),
            ("one-1.jpg", "two-1.jpg", "four-1.jpg", "four-3.jpg", "four-4.jpg"),
        )
