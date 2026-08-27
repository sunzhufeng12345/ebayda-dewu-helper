"""Excel/来源数据 → 得物「新品创建 V2」(/dop/api/v2/nps/create) 入参的映射层。

职责（对应迁移任务 5/6/7）：
- 类目路径 → category_id、品牌名 → brand_id（经 mappings.json，缺口进清单）
- 价格元(Decimal) → 分(int)；适用人群 → fit_id（中性/青少年重映射+告警）
- 颜色×尺码 → sale_properties + sku_list（含 bar_code/merchant_sku_code/pre_bidding）
- Excel 尺码表 sheet → size_table 内联数组
- 图片：MediaFiles.carousel_by_color（已按颜色拆分）→ 逐张 upload_media 拿
  img_key → spu_carousel_images 按颜色绑定（严禁整包传 ZIP）

不发送任何请求；网络动作由 DewuClient 负责，本模块只做纯数据变换，
可用任意商品数据离线单测。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional

_THIS_DIR = Path(__file__).resolve().parent
MAPPINGS_PATH = _THIS_DIR / "mappings.json"
PROPS_TEMPLATE_PATH = _THIS_DIR / "mappings_props_template.json"

# 适用人群 → fit_id（1通用 2男 3女 4儿童 5婴童 6中童 7大童）
# 中性/青少年是 Excel 侧语义，得物无对应枚举，按业务口径重映射并告警。
FIT_ID_MAP: Mapping[str, tuple[int, str | None]] = {
    "通用": (1, None),
    "男": (2, None),
    "女": (3, None),
    "儿童": (4, None),
    "婴童": (5, None),
    "中童": (6, None),
    "大童": (7, None),
    "中性": (1, "适用人群'中性'重映射为 fit_id=1(通用)"),
    "青少年": (7, "适用人群'青少年'重映射为 fit_id=7(大童)"),
}

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class PayloadError(ValueError):
    """无法构造合法入参（如映射缺 id、价格非法）时抛出，附缺口说明。"""


# ---------------------------------------------------------------------------
# 映射表（类目路径/品牌名 → id），本地 JSON 缓存，人工补全一次
# ---------------------------------------------------------------------------
def load_mappings(path: Path = MAPPINGS_PATH) -> dict[str, dict[str, dict[str, Any]]]:
    if not path.is_file():
        return {"categories": {}, "brands": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"categories": data.get("categories", {}), "brands": data.get("brands", {})}


# ---------------------------------------------------------------------------
# 官方元数据接口（2026-08-25 实测确认，取代 stark 抓包方案）：
#   /dop/api/v1/nps/query_category  {brand_id}        → 整棵类目树（含 id）
#   /dop/api/v1/nps/query_property  {category_id}     → 属性模板（id/候选值）
# ---------------------------------------------------------------------------
def fetch_category_tree(client, brand_id: int) -> list[dict]:
    """拉取品牌可用类目树（官方 query_category 接口）。"""
    result = client.request("/dop/api/v1/nps/query_category", {"brand_id": brand_id})
    return result.get("data") or []


def flatten_category_tree(tree: list[dict], prefix: str = "") -> dict[str, int]:
    """类目树 → {'服装>>上衣>>卫衣': 1002848, ...}（全层级路径 → id）。"""
    flat: dict[str, int] = {}
    for node in tree:
        name = node.get("category_name") or ""
        cid = node.get("category_id")
        path = f"{prefix}>>{name}" if prefix else name
        if name and isinstance(cid, int):
            flat[path] = cid
        children = node.get("product_category_list") or []
        if children:
            flat.update(flatten_category_tree(children, path))
    return flat


def sync_category_mappings(client, brand_id: int, mappings_path: Path = MAPPINGS_PATH) -> dict[str, int]:
    """query_category 拉树 → 自动填充 mappings.json 的 categories 区块（只增不删）。"""
    flat = flatten_category_tree(fetch_category_tree(client, brand_id))
    data: dict[str, Any] = {"categories": {}, "brands": {}}
    if mappings_path.is_file():
        raw = json.loads(mappings_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = raw
    cats = data.setdefault("categories", {})
    added = 0
    for path, cid in flat.items():
        entry = cats.get(path)
        if not entry or not entry.get("category_id"):
            cats[path] = {"category_id": cid}
            added += 1
    mappings_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"类目树同步：共 {len(flat)} 条路径，新增填充 {added} 条 -> {mappings_path.name}")
    return flat


def fetch_props_template_api(client, category_id: int, category_key: str = "") -> dict[str, dict[str, Any]]:
    """官方 query_property 接口拉属性模板，返回 load_props_template 同款结构；
    category_key 非空时顺手缓存进 mappings_props_template.json（离线可用）。"""
    result = client.request("/dop/api/v1/nps/query_property", {"category_id": category_id})
    items = result.get("data") or []
    # 官方字段 → 缓存统一格式（与 load_props_template 读取口径一致）
    cache_items = [
        {
            "name": p["property_name"],
            "property_id": p["property_id"],
            "definition_id": p.get("definition_id") or 0,
            "required": bool(p.get("required")),
            "value_type": p.get("type", 0),
            "property_type": p.get("property_type", 0),
            "value_options": p.get("value_options") or [],
        }
        for p in items
    ]
    tpl = {
        it["name"]: {
            "property_id": it["property_id"],
            "definition_id": it["definition_id"],
            "value_type": it["value_type"],
            "required": it["required"],
            "value_options": it["value_options"],
        }
        for it in cache_items
    }
    if category_key and cache_items:
        _save_props_template(category_key, cache_items)
    return tpl


def _save_props_template(category_key: str, items: list[dict]) -> None:
    data: dict[str, Any] = {}
    if PROPS_TEMPLATE_PATH.is_file():
        raw = json.loads(PROPS_TEMPLATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, list):
            data = {"服装>>上衣>>卫衣": raw}
    data[category_key] = items
    PROPS_TEMPLATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_props_template(
    category_key: str = "", path: Path = PROPS_TEMPLATE_PATH
) -> dict[str, dict[str, Any]]:
    """类目属性模板（抓自 stark queryProperties）：属性名 → id/候选值。

    mappings_props_template.json 按类目路径分键（如 "服装>>上衣>>卫衣"），
    不同衣服类型的属性集不同，换类目需用 sniff_prop_template.py 重抓并入库。
    category_key 为空时返回 {}（调用方应告警）。
    兼容旧格式（裸 list）：视为卫衣模板。
    """
    if not path.is_file() or not category_key:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):  # 旧格式迁移前兼容
        data = {"服装>>上衣>>卫衣": data}
    items = data.get(category_key) or []
    return {
        item["name"]: {
            "property_id": item["property_id"],
            "definition_id": item["definition_id"],
            "value_type": item.get("value_type", 0),
            "required": item.get("required", False),
            "value_options": item.get("value_options", []),
        }
        for item in items
    }


def props_template_categories(path: Path = PROPS_TEMPLATE_PATH) -> list[str]:
    """已缓存属性模板的类目清单（供缺口提示/人工核对）。"""
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return ["服装>>上衣>>卫衣"]
    return list(data.keys())


def resolve_category_id(mappings: dict, dewu_path: tuple[str, ...]) -> int:
    key = ">>".join(dewu_path)
    entry = mappings["categories"].get(key)
    if not entry or not entry.get("category_id"):
        raise PayloadError(
            f"类目「{key}」缺 category_id，请在 dewu_api/mappings.json 补全"
            "（可运行 build_mappings_template.py 生成待补清单）"
        )
    return int(entry["category_id"])


def resolve_brand_id(mappings: dict, brand_name: str) -> int:
    entry = mappings["brands"].get(brand_name)
    if not entry or not entry.get("brand_id"):
        raise PayloadError(f"品牌「{brand_name}」缺 brand_id，请在 dewu_api/mappings.json 补全")
    return int(entry["brand_id"])


def yuan_to_fen(yuan: Decimal | float | str) -> int:
    try:
        fen = int((Decimal(str(yuan)) * 100).quantize(Decimal("1")))
    except Exception as exc:  # noqa: BLE001
        raise PayloadError(f"价格“{yuan}”无法转换为分：{exc}") from exc
    if fen <= 0:
        raise PayloadError(f"价格必须为正数（分），得到 {fen}")
    return fen


def resolve_fit_id(crowd: str) -> tuple[int, str | None]:
    """返回 (fit_id, 重映射告警或None)。未知人群回落通用并告警。"""
    hit = FIT_ID_MAP.get(crowd)
    if hit:
        return hit
    return 1, f"适用人群'{crowd}'无映射，回落 fit_id=1(通用)"


# ---------------------------------------------------------------------------
# sale_properties + sku_list（颜色×尺码展开）
# ---------------------------------------------------------------------------
def build_sale_properties(colors: tuple[str, ...], sizes: tuple[str, ...]) -> list[dict]:
    props: list[dict] = []
    for color in colors:
        props.append({
            "property_type": 1, "property_id": 1, "value_type": 1, "definition_id": 1,
            "property_name": "颜色", "value": color, "level": 1,
        })
    for size in sizes:
        props.append({
            "property_type": 1, "property_id": 2, "value_type": 1, "definition_id": 2,
            "property_name": "尺码", "value": size, "level": 2,
        })
    return props


def build_sku_list(product) -> list[dict]:
    """product: models.ProductData。颜色×尺码全组合，仅保留来源 skus 命中的规格。

    - merchant_sku_code：来源 skus[].code（权威值）
    - bar_code：来源无 EAN，暂留空串，可后续人工/接口补（见缺口清单）
    - pre_bidding.bid_price：吊牌价（元→分）；stock：固定 SKU 库存
    """
    sku_by_variant = product.sku_by_variant
    sku_list: list[dict] = []
    for color in product.colors:
        for size in product.sizes:
            sku = sku_by_variant.get((color, size))
            if sku is None:
                continue  # 来源未提供的规格不生成，避免页面/API 校验失败
            sku_list.append({
                "bar_code": "",
                "merchant_sku_code": sku.product_code,
                "property_list": [
                    {"level": 1, "property_name": "颜色", "property_value": color},
                    {"level": 2, "property_name": "尺码", "property_value": size},
                ],
                "pre_bidding": {
                    "bid_price": yuan_to_fen(sku.offer_amount),
                    "bid_type": 1,
                    "stock": sku.inventory,
                    "brand_send_mode_type": 0,
                },
            })
    if not sku_list:
        raise PayloadError("颜色×尺码与来源 skus 无交集，无法生成 SKU")
    return sku_list


# ---------------------------------------------------------------------------
# 尺码表（Excel 尺码表 sheet → size_table 内联数组）
# ---------------------------------------------------------------------------
def _strip_cm_suffix(name: str) -> str:
    """测量名去 (cm)/(CM) 后缀：官方 UI 建草稿的 sizeTable 用裸名（衣长/胸围/袖长）。"""
    return re.sub(r"[(（]\s*[cC][mM]\s*[)）]\s*$", "", name).strip()


def build_size_table(size_chart: Mapping[str, Mapping[str, str]]) -> list[dict]:
    """size_chart: {尺码: {测量名: 值}}（config_loader.Config.size_chart）。

    列式结构（2026-08-27 抓 stark draftDetail 旧草稿实证，推翻 8/25 的 JSON 字符串结论）：
        [{size_key: "尺码", size_value: "M,L,XL", remark_sort: 0, type: 1},
         {size_key: "衣长", size_value: "64,66,68", remark_sort: 0, type: 2}, ...]
    得物前端按 size_value.split(",") 渲染：首行 type=1 是尺码名序列，
    其后每个测量参数一行 type=2，值按尺码顺序逗号分隔。
    旧实现把 name/value JSON 串当 size_value 传，API 虽接受但后台渲染成 JSON 碎片。
    """
    sizes = list(size_chart.keys())
    if not sizes:
        return []
    measure_names: list[str] = []
    for measures in size_chart.values():
        for name in measures:
            bare = _strip_cm_suffix(str(name))
            if bare and bare not in measure_names:
                measure_names.append(bare)
    table: list[dict] = [
        {"size_key": "尺码", "size_value": ",".join(str(s) for s in sizes), "remark_sort": 0, "type": 1}
    ]
    for name in measure_names:
        values = []
        for measures in size_chart.values():
            hit = next((v for k, v in measures.items() if _strip_cm_suffix(str(k)) == name), "")
            values.append(str(hit) if hit not in (None, "") else "")
        table.append({"size_key": name, "size_value": ",".join(values), "remark_sort": 0, "type": 2})
    return table


# ---------------------------------------------------------------------------
# 图片上传链路（对应迁移任务 3）
# ---------------------------------------------------------------------------
def upload_carousel_images(client, media_files) -> tuple[dict[str, list[str]], list[str]]:
    """MediaFiles.carousel_by_color → 逐张 upload_media → {颜色: [img_key...]}。

    严禁把 ZIP 整包发给接口；media_files 必须是 models.extract_and_resolve_media()
    解压后的结果。返回 (img_keys_by_color, 上传告警列表)。
    """
    keys_by_color: dict[str, list[str]] = {}
    warnings: list[str] = []
    for color, paths in media_files.carousel_by_color.items():
        keys: list[str] = []
        for img_path in paths:
            try:
                keys.append(client.upload_media(img_path))
            except Exception as exc:  # noqa: BLE001 - 单张失败不拖垮整链路
                warnings.append(f"颜色'{color}'图片 {Path(img_path).name} 上传失败：{exc}")
        if keys:
            keys_by_color[color] = keys
        else:
            warnings.append(f"颜色'{color}'无任何图片上传成功")
    return keys_by_color, warnings


def build_spu_carousel_images(img_keys_by_color: Mapping[str, list[str]]) -> list[dict]:
    """img_key 按颜色绑定成 spu_carousel_images（property_name 固定'颜色'）。"""
    images: list[dict] = []
    for color, keys in img_keys_by_color.items():
        for key in keys:
            images.append({
                "img_key": key,
                "property_name": "颜色",
                "property_value": color,
            })
    return images


# ---------------------------------------------------------------------------
# 详情图三区块 + 尺码推荐/试穿报告（官方 apiId=1207 字段，2026-08-25 补）
# ---------------------------------------------------------------------------
def upload_detail_images(client, media_files) -> dict[str, list[str]]:
    """上传 Chrome 方案三区块图，返回 {区块: [img_key...]}。

    media_files: models.extract_and_resolve_media() 的产物。
    区块映射（Chrome 后台名 → API 字段）：
      商品展示 product_display_backs → spu_display_images
      细节呈现 details              → detail_display_images
      穿搭效果 outfit_fronts        → wear_effect_images
    upload_media type 实测（2026-08-25 生产盲测 1-8）：1=轮播图(25:16/1:1 严格)，
    2=场景图(1:1), 3=穿搭轮播(1:1), 5=艺术品, 8=视频封面；4/6/7 对非方图放行，
    详情类区块统一用 4。
    """
    sections = {
        "spu_display_images": media_files.product_display_backs,
        "detail_display_images": media_files.details,
        "wear_effect_images": media_files.outfit_fronts,
    }
    result: dict[str, list[str]] = {}
    for field, paths in sections.items():
        keys = [client.upload_media(p, media_type=4) for p in paths]
        if keys:
            result[field] = keys
    return result


def build_detail_images(section_keys: Mapping[str, list[str]], color: str) -> dict:
    """三区块 img_key → API 数组结构（绑定第一颜色，与 Chrome 方案全色共用口径一致）。"""
    return {
        field: [
            {"img_key": key, "property_name": "颜色", "property_value": color}
            for key in keys
        ]
        for field, keys in section_keys.items()
    }


def _read_sheet_rows(xlsx_path) -> list[list]:
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    return [list(row) for row in ws.iter_rows(values_only=True)]


def build_size_recommend(xlsx_path) -> dict:
    """尺码推荐 Excel（身高×体重矩阵）→ size_recommend_table_template。

    官方结构：{tips, name, template_id?, split_report_table:[{size_key,size_value,type}]}。
    转换规则：首行为表头（体重段），每数据行 size_key=身高，size_value=体重段→尺码的
    JSON 数组；温馨提示列并入 tips。
    """
    rows = _read_sheet_rows(xlsx_path)
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    tips = next((h for h in header if "提示" in h), "尺码推荐仅供参考")
    body = [r for r in rows[1:] if any(c is not None for c in r)]
    weight_cols = [i for i, h in enumerate(header) if h and "提示" not in h and i > 0]
    table = []
    for r in body:
        cells = [str(c).strip() if c is not None else "" for c in r]
        size_value = [
            {"weight": header[i], "size": cells[i]} for i in weight_cols if cells[i]
        ]
        table.append({
            "size_key": cells[0],
            "size_value": json.dumps(size_value, ensure_ascii=False),
            "type": 1,
        })
    return {"name": "尺码推荐", "tips": tips, "split_report_table": table}


def build_try_on_report(xlsx_path) -> dict:
    """试穿报告 Excel（行式表）→ {report_ext, report_table}。

    官方结构：report_ext={tips,name}；report_table=[{size_key,size_value}]。
    转换规则：size_key=试穿者，size_value=整行信息 JSON（身高/体重/性别/尺码/体验）。
    """
    rows = _read_sheet_rows(xlsx_path)
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    tips = next((h for h in header if "提示" in h), "试穿报告仅供参考")
    body = [r for r in rows[1:] if any(c is not None for c in r)]
    table = []
    for r in body:
        cells = [str(c).strip() if c is not None else "" for c in r]
        info = {header[i]: cells[i] for i in range(len(header)) if header[i] and "提示" not in header[i]}
        table.append({
            "size_key": cells[0],
            "size_value": json.dumps(info, ensure_ascii=False),
        })
    return {"report_ext": {"name": "试穿报告", "tips": tips}, "report_table": table}


# ---------------------------------------------------------------------------
# 主入口：组装完整 create 入参
# ---------------------------------------------------------------------------
def build_create_payload(
    product,
    cfg,
    mappings: dict,
    img_keys_by_color: Mapping[str, list[str]],
    allow_missing: bool = False,
    detail_images: Optional[dict] = None,
    size_recommend: Optional[dict] = None,
    try_on_report: Optional[dict] = None,
) -> dict:
    """product: models.ProductData；cfg: config_loader.Config。

    外链规则：website 必须是真实外网 URL；Excel 填“无”时无法通过 API 校验，
    抛 PayloadError 提示补链（这是与 Chrome 方案的口径差异之一）。
    allow_missing=True（dry-run 用）：映射缺 id/外链缺失时以 0 或告警占位继续，
    把缺口写进 _warnings 而不是抛异常。
    """
    warnings: list[str] = list(product.warnings)

    def _id_or_zero(kind: str, key: str, resolver) -> int:
        try:
            return resolver()
        except PayloadError as exc:
            if not allow_missing:
                raise
            warnings.append(f"[缺口] {exc}")
            return 0

    fit_id, fit_warn = resolve_fit_id(cfg.applicable_crowd)
    if fit_warn:
        warnings.append(fit_warn)

    website = (cfg.external_link or "").strip()
    if not _URL_RE.match(website):
        msg = (
            f"website 需真实外网 URL，当前为“{website}”（'无'是页面口径，API 不接受）。"
            "请在 Excel 基础设置-外链填入商品真实链接。"
        )
        if not allow_missing:
            raise PayloadError(msg)
        warnings.append(f"[缺口] {msg}")
        website = "https://www.example.com/placeholder"

    article_number = (product.item_no or product.code)[:30]

    price_fen = yuan_to_fen(product.release_price)

    # 属性来源 = models._build_dewu_attributes() 的产物（面料归一化/袖长推导/
    # 衣长/版型/厚度/是否加绒兜底），与 Chrome 方案 main.py 同口径。
    # 2026-08-25 生产实证：property_id=0 会被服务端并进同一属性报"不能重复填写"，
    # 必须用真实模板 id（抓自 stark 后台 queryProperties 接口，缓存于
    # mappings_props_template.json，sourceId=类目 id）。
    attr_items: list[tuple[str, tuple[str, ...]]] = list(product.attributes.items())
    # 必填属性兜底（对齐 main.py 必填集合）：袖长缺失时兜底"长袖"。
    attr_names = {name for name, _ in attr_items}
    for required, fallback in (("袖长", ("长袖",)),):
        if required not in attr_names:
            attr_items.append((required, fallback))
            warnings.append(f"必填属性「{required}」缺失，使用兜底值{''.join(fallback)}")
    category_key = ">>".join(cfg.category_fallback)
    prop_tpl = load_props_template(category_key)
    if not prop_tpl:
        cached = props_template_categories()
        warnings.append(
            f"[缺口] 类目「{category_key}」无属性模板缓存（已缓存：{'、'.join(cached) or '无'}），"
            "所有属性将被跳过。请运行 sniff_prop_template.py 抓取该类目模板。"
        )
    base_properties = []
    for name, values in attr_items:
        tpl = prop_tpl.get(name)
        if tpl is None:
            warnings.append(f"属性「{name}」不在类目模板中，跳过（值：{'、'.join(values)}）")
            continue
        # 值校验：value_type=0 为自由文本直传；候选值属性在列表内直选，
        # 不在的剔除并告警（对齐面料兜底哲学：不中止）。
        if tpl["value_type"] == 0 or not tpl["value_options"]:
            picked, dropped = list(values), []
        else:
            candidates = tpl["value_options"]
            picked = [v for v in values if v in candidates]
            dropped = [v for v in values if v not in candidates]
        if dropped:
            warnings.append(
                f"属性「{name}」值 {'、'.join(dropped)} 不在候选值中，已剔除"
            )
        if not picked:
            warnings.append(f"属性「{name}」全部值被剔除，跳过该属性")
            continue
        value = ",".join(picked)
        base_properties.append(
            {
                "property_type": 1,
                "value_type": tpl["value_type"],
                "property_id": tpl["property_id"],
                "definition_id": tpl["definition_id"],
                "property_name": name,
                "value": value,
                "end_value": value,
                "level": 1,
                "image_url": "",
            }
        )

    # 结构化标题（对齐 Chrome 方案 main.py：brand + 卖点 + 类目 + 人群 分段填）
    title_parts = product.title  # TitleParts(selling_point, category, audience)
    title_full = f"{product.brand}{title_parts.selling_point}{title_parts.category}{title_parts.audience}"
    payload = {
        "brand_id": _id_or_zero("brand", product.brand, lambda: resolve_brand_id(mappings, product.brand)),
        "category_id": _id_or_zero("category", ">>".join(cfg.category_fallback), lambda: resolve_category_id(mappings, cfg.category_fallback)),
        "fit_id": fit_id,
        "article_number": article_number,
        "website": website,
        "title": title_full,
        "title_type": 1,  # 结构化标题
        "structure_title_info": {
            "input_part_list": [
                {"part_name": "品牌", "input_value": product.brand},
                {"part_name": "卖点提炼", "input_value": title_parts.selling_point},
                {"part_name": "类目", "input_value": title_parts.category},
                {"part_name": "适用人群", "input_value": title_parts.audience},
            ]
        },
        "properties": "；".join(
            f"{name}：{'、'.join(values)}" for name, values in product.attributes.items()
        ),
        "price": price_fen,
        "auth_price": price_fen,
        "stock": cfg.sku_inventory,
        "create_type": 2,  # 只推草稿，红线：不提交审核/出价
        "base_properties": base_properties,
        "sale_properties": build_sale_properties(product.colors, product.sizes),
        "sku_list": build_sku_list(product),
        "spu_carousel_images": build_spu_carousel_images(img_keys_by_color),
        "size_table": build_size_table(cfg.size_chart),
        "size_ext": {"tips": "尺码表仅供参考", "name": "尺码表", "type": 1},
        # 附属信息（非接口字段，调用方剥离后打印/记录）
        "_warnings": warnings,
    }
    # 详情图三区块 + 尺码推荐/试穿报告（可选模块，有数据才带）
    if detail_images:
        payload.update(detail_images)  # spu_display_images/detail_display_images/wear_effect_images
    if size_recommend:
        payload["size_recommend_table_template"] = size_recommend
    if try_on_report:
        payload["report_ext"] = try_on_report["report_ext"]
        payload["report_table"] = try_on_report["report_table"]
    if not payload["spu_carousel_images"]:
        raise PayloadError("spu_carousel_images 为空：轮播图必填，请先完成图片上传")
    # 轮播图按颜色分组必填：缺色告警（allow_missing 时放行，真实调用前必须补齐）
    missing_colors = [c for c in product.colors if c not in img_keys_by_color]
    if missing_colors:
        msg = f"以下颜色缺轮播图，草稿将缺这些颜色的图：{'、'.join(missing_colors)}"
        if allow_missing:
            warnings.append(msg)
        else:
            raise PayloadError(msg)
    return payload
