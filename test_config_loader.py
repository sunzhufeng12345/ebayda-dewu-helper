"""config_loader 的轻量单元测试（不依赖 DrissionPage，仅用 openpyxl）。

用 unittest.TestCase 组织，CI 的 `python -m unittest` 可直接收集。验证：
1. 读取真实 配置文件/config.xlsx 的默认值与映射；
2. resolve_category 的关键词匹配 / 最长优先 / 回落兜底；
3. 配置文件缺失时回落默认并产出告警；
4. 类目路径用“>>”分隔：类目名自带“/”不再产生层级歧义；
5. 单个 sheet 解析异常时整表回落默认并告警，不崩溃；
6. 属性覆盖支持中文逗号分隔多值。
"""

import tempfile
import unittest
from pathlib import Path

import openpyxl

from config_loader import Config, load_config, resolve_category

ROOT = Path(__file__).resolve().parent

_TEMP_DIRS: list = []  # 持有 TemporaryDirectory 引用，避免文件被提前清理


class FakeProduct:
    def __init__(self, category_path):
        self.category_path = tuple(category_path)


def _workbook_with(sheet_name: str, rows: list) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
    tmp = tempfile.TemporaryDirectory()
    _TEMP_DIRS.append(tmp)
    path = Path(tmp.name) / "配置.xlsx"
    wb.save(path)
    return path


class LoadRealConfigTests(unittest.TestCase):
    def test_load_real_config(self):
        cfg = load_config(ROOT / "配置文件" / "配置.xlsx")
        self.assertEqual(cfg.offer_type, "直发")
        self.assertEqual(cfg.price_proof_source, "品牌官网")
        self.assertEqual(cfg.release_proof_source, "品牌官网")
        self.assertEqual(cfg.applicable_crowd, "通用")
        self.assertEqual(cfg.sku_inventory, 1000)
        self.assertEqual(cfg.external_link, "无")
        self.assertEqual(cfg.category_fallback, ("服装", "上衣", "卫衣"))
        # 尺码表列名必须与得物弹窗一致
        first = next(iter(cfg.size_chart.values()))
        self.assertIn("1/2胸围(cm)", first)
        self.assertIn(("卫衣", ("服装", "上衣", "卫衣")), cfg.category_mapping)


class ResolveCategoryTests(unittest.TestCase):
    def test_resolve_mapping(self):
        cfg = load_config(ROOT / "配置文件" / "配置.xlsx")
        p = FakeProduct(["男装", "男士卫衣", "男士套头卫衣"])
        self.assertEqual(resolve_category(cfg, p), ("服装", "上衣", "卫衣"))

    def test_resolve_longest_keyword(self):
        cfg = Config(
            category_mapping=[
                ("卫衣", ("服装", "上衣", "卫衣")),
                ("连帽卫衣", ("服装", "上衣", "连帽卫衣")),
            ]
        )
        p = FakeProduct(["男装", "连帽卫衣"])
        self.assertEqual(resolve_category(cfg, p), ("服装", "上衣", "连帽卫衣"))

    def test_resolve_fallback(self):
        cfg = Config(category_fallback=("服装", "上衣", "卫衣"))
        p = FakeProduct(["男装", "男士风衣"])
        self.assertEqual(resolve_category(cfg, p), ("服装", "上衣", "卫衣"))

    def test_slash_inside_category_name(self):
        # 类目名自带“/”（如 三坑/COSPLAY）时路径必须保持一个整体层级。
        cfg = load_config(ROOT / "配置文件" / "配置.xlsx")
        p = FakeProduct(["女装", "三坑/COSPLAY", "COSPLAY"])
        # 未配映射时回落兜底
        self.assertEqual(resolve_category(cfg, p), ("服装", "上衣", "卫衣"))

        cfg2 = Config(
            category_mapping=[("COSPLAY", ("女装", "三坑/COSPLAY", "COSPLAY"))]
        )
        self.assertEqual(
            resolve_category(cfg2, p), ("女装", "三坑/COSPLAY", "COSPLAY")
        )


class FallbackAndWarningsTests(unittest.TestCase):
    def test_missing_file_fallback(self):
        cfg = load_config(ROOT / "不存在的路径.xlsx")
        self.assertTrue(cfg.warnings, "缺失配置应产生告警")
        self.assertEqual(cfg.offer_type, "直发")

    def test_legacy_slash_path_rejected(self):
        # 旧版用“/”做分隔符的路径无法区分层级，应告警并忽略该行。
        path = _workbook_with(
            "类目映射",
            [
                ["来源类目关键词", "得物起始路径"],
                ["COSPLAY", "女装/三坑/COSPLAY/COSPLAY"],
                ["汉服", "女装>>三坑/COSPLAY>>汉服"],
            ],
        )
        cfg = load_config(path)
        mapping = dict(cfg.category_mapping)
        self.assertEqual(mapping.get("汉服"), ("女装", "三坑/COSPLAY", "汉服"))
        self.assertNotIn("COSPLAY", mapping)
        self.assertTrue(any("COSPLAY" in w for w in cfg.warnings), cfg.warnings)

    def test_sheet_parse_error_falls_back(self):
        # 单元格值能令解析抛异常（如 1E+999 -> int(inf) 溢出）时，
        # 该 sheet 回落默认并告警，load_config 不崩溃。
        path = _workbook_with(
            "基础设置",
            [
                ["项目", "值"],
                ["每条SKU库存", "1E+999"],
            ],
        )
        cfg = load_config(path)
        self.assertEqual(cfg.sku_inventory, 1000)
        self.assertTrue(any("基础设置" in w for w in cfg.warnings), cfg.warnings)

    def test_attribute_chinese_comma(self):
        path = _workbook_with(
            "属性覆盖",
            [
                ["字段", "值（多值用逗号分隔）"],
                ["图案", "字母，印花"],
            ],
        )
        cfg = load_config(path)
        self.assertEqual(cfg.attribute_overrides, {"图案": ("字母", "印花")})


if __name__ == "__main__":
    unittest.main(verbosity=2)
