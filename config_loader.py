"""从 配置文件/config.xlsx 读取得物自动上架的运行配置。

设计目标（面向非技术店铺运营）：
- 配置缺失、Excel 损坏、字段非法时一律回落内置默认值并收集告警，绝不整体崩溃；
- 读到的合法值覆盖内置常量，供 main.py 在启动阶段整体套用；
- 类目映射提供「来源类目关键词 → 得物起始路径」的桥接，取代写死的 START_CATEGORY_PATH。

内置默认值与 main.py / models.py 的现有常量保持一致，因此不提供配置文件时行为与
此前完全等价。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

try:  # openpyxl 为可选依赖：缺失时回落默认配置而不是报错。
    from openpyxl import load_workbook

    _HAS_OPENPYXL = True
except Exception:  # pragma: no cover - 仅在未安装依赖时触发
    _HAS_OPENPYXL = False


# ---------------------------------------------------------------------------
# 内置默认值（与 main.py / models.py 现有常量保持一致，作为兜底）
# ---------------------------------------------------------------------------
DEFAULT_OFFER_TYPE = "直发"
DEFAULT_PRICE_PROOF_SOURCE = "品牌官网"
DEFAULT_RELEASE_PROOF_SOURCE = "品牌官网"
DEFAULT_APPLICABLE_CROWD = "通用"
DEFAULT_SKU_INVENTORY = 1000
DEFAULT_EXTERNAL_LINK = "无"
DEFAULT_CATEGORY_FALLBACK: tuple[str, ...] = ("服装", "上衣", "卫衣")
DEFAULT_PACKAGE: Mapping[str, str] = {
    "length_cm": "42",
    "width_cm": "38",
    "height_cm": "5",
    "weight_kg": "0.8",
}
DEFAULT_SIZE_CHART: Mapping[str, Mapping[str, str]] = {
    "M": {"1/2胸围(cm)": "55", "衣长(cm)": "68", "袖长(cm)": "61"},
    "L": {"1/2胸围(cm)": "57", "衣长(cm)": "70", "袖长(cm)": "62"},
    "XL": {"1/2胸围(cm)": "59", "衣长(cm)": "72", "袖长(cm)": "63"},
    "2XL": {"1/2胸围(cm)": "61", "衣长(cm)": "74", "袖长(cm)": "64"},
    "3XL": {"1/2胸围(cm)": "63", "衣长(cm)": "76", "袖长(cm)": "65"},
}

# 枚举白名单：基础设置里允许的取值，超出则回落默认并告警。
OFFER_TYPES = {"直发", "寄卖"}
PROOF_SOURCES = {"品牌官网", "得物", "天猫", "京东", "其他"}
AUDIENCES = {"通用", "男", "女", "中性", "儿童", "青少年"}


@dataclass
class Config:
    """一次运行解析出的全部可配置项。"""

    offer_type: str = DEFAULT_OFFER_TYPE
    price_proof_source: str = DEFAULT_PRICE_PROOF_SOURCE
    release_proof_source: str = DEFAULT_RELEASE_PROOF_SOURCE
    applicable_crowd: str = DEFAULT_APPLICABLE_CROWD
    sku_inventory: int = DEFAULT_SKU_INVENTORY
    external_link: str = DEFAULT_EXTERNAL_LINK
    category_fallback: tuple[str, ...] = DEFAULT_CATEGORY_FALLBACK
    package_defaults: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_PACKAGE)
    )
    size_chart: Mapping[str, Mapping[str, str]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_SIZE_CHART.items()}
    )
    attribute_overrides: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    category_mapping: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _iter_rows(wb, sheet: str) -> list[tuple]:
    if sheet not in wb.sheetnames:
        return []
    ws = wb[sheet]
    return [tuple(cell for cell in row) for row in ws.iter_rows(values_only=True)]


def _as_text(value) -> str:
    return "" if value is None else str(value).strip()


def _split_category_path(value: str) -> tuple[str, ...]:
    # 类目名本身可含“/”（如 三坑/COSPLAY），因此路径的层级分隔符固定为“>>”，
    # 与得物页面 Cascader 回显一致；“/”不再作为分隔符，否则含“/”的类目名
    # 会被错误地切成多级，导致页面上逐级点选与回显校验全部失败。
    return tuple(
        part.strip() for part in value.replace("＞＞", ">>").split(">>") if part.strip()
    )


# ---------------------------------------------------------------------------
# 各 sheet 解析
# ---------------------------------------------------------------------------
def _apply_basic(wb, cfg: Config) -> None:
    rows = _iter_rows(wb, "基础设置")
    if len(rows) < 2:
        return
    data: dict[str, object] = {}
    for row in rows[1:]:
        if not row or len(row) < 2:
            continue
        key = _as_text(row[0])
        if key:
            data[key] = row[1]

    v = data.get("出价类型")
    if v is not None:
        s = _as_text(v)
        if s:
            if s in OFFER_TYPES:
                cfg.offer_type = s
            else:
                cfg.warnings.append(
                    f"出价类型'{s}'不在白名单{OFFER_TYPES}，使用默认'{DEFAULT_OFFER_TYPE}'"
                )

    v = data.get("价格证明渠道")
    if v is not None:
        s = _as_text(v)
        if s:
            if s in PROOF_SOURCES:
                cfg.price_proof_source = s
            else:
                cfg.warnings.append(
                    f"价格证明渠道'{s}'不在白名单，使用默认'{DEFAULT_PRICE_PROOF_SOURCE}'"
                )

    v = data.get("日期证明渠道")
    if v is not None:
        s = _as_text(v)
        if s:
            if s in PROOF_SOURCES:
                cfg.release_proof_source = s
            else:
                cfg.warnings.append(
                    f"日期证明渠道'{s}'不在白名单，使用默认'{DEFAULT_RELEASE_PROOF_SOURCE}'"
                )

    v = data.get("适用人群")
    if v is not None:
        s = _as_text(v)
        if s:
            if s in AUDIENCES:
                cfg.applicable_crowd = s
            else:
                cfg.warnings.append(
                    f"适用人群'{s}'不在白名单{AUDIENCES}，使用默认'{DEFAULT_APPLICABLE_CROWD}'"
                )

    v = data.get("每条SKU库存")
    if v is not None:
        s = _as_text(v)
        if s:
            try:
                cfg.sku_inventory = int(float(s))
            except ValueError:
                cfg.warnings.append(f"每条SKU库存'{s}'不是合法整数，使用默认{DEFAULT_SKU_INVENTORY}")

    v = data.get("外链")
    if v is not None:
        s = _as_text(v)
        if s:
            cfg.external_link = s

    v = data.get("起始类目(兜底)")
    if v is not None:
        s = _as_text(v)
        if s:
            parts = _split_category_path(s)
            if len(parts) == 3:
                cfg.category_fallback = parts
            else:
                cfg.warnings.append(
                    f"起始类目(兜底)“{s}”不是三级路径（格式：一级>>二级>>三级，"
                    "如 服装>>上衣>>卫衣），使用默认"
                )


def _apply_package(wb, cfg: Config) -> None:
    rows = _iter_rows(wb, "包装尺寸")
    if len(rows) < 2:
        return
    header = [_as_text(h) for h in rows[0]]
    mapping = {"长(cm)": "length_cm", "宽(cm)": "width_cm", "高(cm)": "height_cm", "重(kg)": "weight_kg"}
    pkg = dict(DEFAULT_PACKAGE)
    changed = False
    for i, h in enumerate(header):
        key = mapping.get(h)
        if not key or i >= len(rows[1]):
            continue
        val = _as_text(rows[1][i])
        if val:
            pkg[key] = val
            changed = True
    if changed:
        cfg.package_defaults = pkg


def _apply_size_chart(wb, cfg: Config) -> None:
    rows = _iter_rows(wb, "尺码表")
    if len(rows) < 2:
        return
    header = [_as_text(h) for h in rows[0]]
    if not header or header[0] != "尺码":
        cfg.warnings.append("尺码表首列应为'尺码'，跳过尺码表解析")
        return
    measure_cols = header[1:]
    chart: dict[str, dict[str, str]] = {}
    for row in rows[1:]:
        if not row or not _as_text(row[0]):
            continue
        size = _as_text(row[0])
        measures: dict[str, str] = {}
        for i, m in enumerate(measure_cols, start=1):
            if not m or i >= len(row):
                continue
            val = _as_text(row[i])
            if val:
                measures[m] = val
        if measures:
            chart[size] = measures
    if chart:
        cfg.size_chart = chart


def _apply_attribute_overrides(wb, cfg: Config) -> None:
    rows = _iter_rows(wb, "属性覆盖")
    if len(rows) < 2:
        return
    overrides: dict[str, tuple[str, ...]] = {}
    for row in rows[1:]:
        if not row or len(row) < 2:
            continue
        field_name = _as_text(row[0])
        if not field_name:
            continue
        val = _as_text(row[1])
        if not val:
            continue
        # 中文输入法常打出全角逗号；先归一成半角再分隔，避免多值被当成一个整体。
        vals = tuple(v for v in val.replace("，", ",").split(",") if v.strip())
        if vals:
            overrides[field_name] = vals
    if overrides:
        cfg.attribute_overrides = overrides


def _apply_category_mapping(wb, cfg: Config) -> None:
    rows = _iter_rows(wb, "类目映射")
    if len(rows) < 2:
        return
    mapping: list[tuple[str, tuple[str, ...]]] = []
    for row in rows[1:]:
        if not row or len(row) < 2:
            continue
        keyword = _as_text(row[0])
        if not keyword:
            continue
        path_val = _as_text(row[1])
        if not path_val:
            continue
        path = _split_category_path(path_val)
        if len(path) != 3:
            cfg.warnings.append(
                f"类目映射“{keyword}”的得物路径“{path_val}”不是三级路径"
                "（格式：一级>>二级>>三级），已忽略该行"
            )
            continue
        mapping.append((keyword, path))
    if mapping:
        cfg.category_mapping = mapping


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def load_config(config_path: Path | None = None) -> Config:
    """读取配置；文件缺失/无 openpyxl/解析失败时回落内置默认值并收集告警。

    config_path 可以是配置文件本身，也可以是其所在目录（会自动查找
    「配置.xlsx」或「config.xlsx」）。
    """
    cfg = Config()
    if config_path is None:
        config_path = Path(__file__).resolve().parent / "配置文件"
    config_path = Path(config_path)
    if config_path.is_dir():
        for name in ("配置.xlsx", "config.xlsx"):
            candidate = config_path / name
            if candidate.is_file():
                config_path = candidate
                break
    if not config_path.is_file():
        cfg.warnings.append(f"未找到配置文件 {config_path}，使用内置默认配置")
        return cfg
    if not _HAS_OPENPYXL:
        cfg.warnings.append("未安装 openpyxl，无法读取 Excel 配置，使用内置默认配置")
        return cfg
    try:
        wb = load_workbook(config_path, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 任何解析异常都回落默认
        cfg.warnings.append(f"读取配置失败：{exc}，使用内置默认配置")
        return cfg
    try:
        # 每个 sheet 独立兜底：单个表解析异常（如单元格写出 inf/损坏数据）时
        # 告警并让该部分保持默认，不影响其余 sheet，也绝不向调用方抛异常。
        for sheet_name, applier in (
            ("基础设置", _apply_basic),
            ("包装尺寸", _apply_package),
            ("尺码表", _apply_size_chart),
            ("属性覆盖", _apply_attribute_overrides),
            ("类目映射", _apply_category_mapping),
        ):
            try:
                applier(wb, cfg)
            except Exception as exc:  # noqa: BLE001 - 单表失败只降级该表
                cfg.warnings.append(
                    f"解析「{sheet_name}」失败：{exc}，该部分使用默认配置"
                )
    finally:
        wb.close()
    return cfg


def resolve_category(cfg: Config, product) -> tuple[str, ...]:
    """按来源类目匹配映射表，返回得物起始路径；无命中时回落兜底类目。

    匹配规则：拿商品 category_path 拼成的字符串，对映射表关键词做包含匹配；
    多个命中时取「最长关键词」（最具体）优先。
    """
    haystack = "/".join(getattr(product, "category_path", ()) or ())
    best: tuple[str, ...] | None = None
    best_len = -1
    for keyword, path in cfg.category_mapping:
        if not keyword:
            continue
        if keyword in haystack:
            if len(keyword) > best_len:
                best = path
                best_len = len(keyword)
    return best if best is not None else cfg.category_fallback
