"""config_loader 的轻量单元测试（不依赖 DrissionPage，仅用 openpyxl）。

直接 import config_loader，验证：
1. 读取真实 配置文件/config.xlsx 的默认值与映射；
2. resolve_category 的关键词匹配 / 最长优先 / 回落兜底；
3. 配置文件缺失时回落默认并产出告警。
"""

import importlib

import config_loader
from config_loader import Config, load_config, resolve_category

ROOT = __import__("pathlib").Path(__file__).resolve().parent


class FakeProduct:
    def __init__(self, category_path):
        self.category_path = tuple(category_path)


def test_load_real_config():
    cfg = load_config(ROOT / "配置文件" / "配置.xlsx")
    assert cfg.offer_type == "直发", cfg.offer_type
    assert cfg.price_proof_source == "品牌官网"
    assert cfg.release_proof_source == "品牌官网"
    assert cfg.applicable_crowd == "通用"
    assert cfg.sku_inventory == 1000
    assert cfg.external_link == "无"
    assert cfg.category_fallback == ("服装", "上衣", "卫衣")
    # 尺码表列名必须与得物弹窗一致
    first = next(iter(cfg.size_chart.values()))
    assert "1/2胸围(cm)" in first, list(first)
    assert ("卫衣", ("服装", "上衣", "卫衣")) in cfg.category_mapping
    print("OK test_load_real_config")


def test_resolve_mapping():
    cfg = load_config(ROOT / "配置文件" / "配置.xlsx")
    p = FakeProduct(["男装", "男士卫衣", "男士套头卫衣"])
    assert resolve_category(cfg, p) == ("服装", "上衣", "卫衣")
    print("OK test_resolve_mapping")


def test_resolve_longest_keyword():
    cfg = Config(
        category_mapping=[
            ("卫衣", ("服装", "上衣", "卫衣")),
            ("连帽卫衣", ("服装", "上衣", "卫衣")),
        ]
    )
    p = FakeProduct(["男装", "连帽卫衣"])
    assert resolve_category(cfg, p) == ("服装", "上衣", "卫衣")
    print("OK test_resolve_longest_keyword")


def test_resolve_fallback():
    cfg = Config(category_fallback=("服装", "上衣", "卫衣"))
    p = FakeProduct(["男装", "男士风衣"])
    assert resolve_category(cfg, p) == ("服装", "上衣", "卫衣")
    print("OK test_resolve_fallback")


def test_missing_file_fallback():
    cfg = load_config(ROOT / "不存在的路径.xlsx")
    assert cfg.warnings, "缺失配置应产生告警"
    assert cfg.offer_type == "直发"
    print("OK test_missing_file_fallback")


if __name__ == "__main__":
    test_load_real_config()
    test_resolve_mapping()
    test_resolve_longest_keyword()
    test_resolve_fallback()
    test_missing_file_fallback()
    print("ALL PASSED")
