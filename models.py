"""得物新品自动化 —— 来源数据解析与图片准备。

======================================================================
一、模块职责
======================================================================
    本模块把“选品中心来源 JSON + 图片 ZIP”转换成浏览器阶段可直接消费的
    只读数据对象，与 main.py 的页面操作逻辑彻底分离：

        1. 解析来源 JSON，归一化为不可变 ProductData（商品快照）；
        2. 解压图片 ZIP，解析出每张图在磁盘上的真实路径（MediaFiles）；
        3. 所有解析失败都以 ProductDataError 抛出，由 main() 统一转换成
           结构化 JSON 输出。

    main.py 只消费本模块的结果，不接触原始 JSON / ZIP。

======================================================================
二、数据流
======================================================================
    来源 JSON ──load_product()──▶ ProductData ──┐
         ▲                                       │
         │ 图片 ZIP ──extract_and_resolve_media()│──▶ MediaFiles
         │                                       │
    浏览器阶段(main.py) ◀── ProductData + MediaFiles 一起使用
        （填写起始页 + 详情页 + 上传图片 + 保存草稿）

======================================================================
三、来源 JSON 字段 → 内部字段 对照表
======================================================================
    下面“对照表”是定位字段来源的权威清单。表中“JSON 字段”列里的
    attributes[] 指商品 attributes 数组里的每一行，行内用 attributeName /
    attributeCode 区分用途。

    | 来源 JSON 字段                                  | 内部字段                 |
    |-------------------------------------------------|--------------------------|
    | data（接口包装）/ 商品对象本身                  | 商品对象                 |
    | id                                              | ProductData.source_id    |
    | code                                            | ProductData.code         |
    | itemNumber                                      | ProductData.item_no      |
    | name                                            | source_name / 标题 / 属性推导 |
    | categoryPath（“/”分隔的多级路径）               | ProductData.category_path|
    | imageSets[] 中 platform=="得物" 的套图          | 图片套图选择             |
    | imageSets[].shopName                            | ProductData.brand        |
    | attributes[]：吊牌价（diaopaijia）              | release_price            |
    | attributes[]：上市时间（sssj）                  | release_season（春/夏/秋/冬）|
    | attributes[]：库存数量（kucun）                 | 仅汇总提示，不参与填写    |
    | attributes[]：领型 / 风格 / 穿着方式 / 材质     | attributes（页面属性）    |
    | attributes[]：材质(caizhi) 行 subValueNumber    | 成分含量                 |
    | attributes[]：商品类型与品牌(leixing-pinpai) 行 subValueText | 设计元素 |
    | skus[]                                          | ProductData.skus         |
    |   skus[].attributes：颜色 / 尺码                | skus.color / skus.size   |
    |   skus[].code                                   | skus.product_code        |
    |   skus[].supplierSkuCode                        | skus.auxiliary_code      |
    |   skus[].price                                  | 仅诊断，不参与页面填写    |
    |   skus[].stock                                  | 仅校验，不参与页面填写    |
    | imageSets[].mainImagePaths                      | media.main               |
    | imageSets[].detailImagePaths                    | media.details            |
    | imageSets[].firstSquarePaths                    | media.first_square       |
    | imageSets[].firstLongPaths                      | media.first_long         |
    | imageSets[].colorDewuPaths（颜色→图片数组）     | media.carousel_by_color  |

======================================================================
四、固定业务规则（写死，不是来源事实）
======================================================================
    以下值由业务方确定，与来源 JSON 无关；若口径变化，改本模块顶部常量
    （见 main.py 顶部“配置区”）即可，无需改解析逻辑：
        - FIXED_EXTERNAL_LINK：外链固定填“无”
        - FIXED_SKU_INVENTORY：每条 SKU 库存固定填 1000
        - 适用人群固定“通用”
        - SKU 出价固定使用吊牌价（忽略来源 skus[].price）
======================================================================
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from zipfile import BadZipFile, ZipFile


# ---- 安全上限：防止异常压缩包占满磁盘或产生过多文件 ----
# 这些限制只影响本地预处理，不改变正常商品图片的解析规则。
MAX_ZIP_MEMBERS = 2_000  # ZIP 内文件/目录总数上限
MAX_ZIP_MEMBER_BYTES = 100 * 1024 * 1024  # 单个成员解压后字节数上限（100 MB）
MAX_ZIP_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 全部成员解压后总字节数上限（2 GB）

# ---- 固定业务规则（写死，与来源 JSON 无关）----
# 口径变化时改这里即可，解析逻辑无需跟着修改。
FIXED_EXTERNAL_LINK = "无"  # 外网链接按业务规则固定填写“无”
FIXED_SKU_INVENTORY = 1000  # 每条 SKU 的库存固定填写 1000


class ProductDataError(ValueError):
    """来源数据无法转换为安全浏览器输入时抛出的数据异常。"""


@dataclass(frozen=True)
class TitleParts:
    """得物结构化标题的三个片段。

    页面标题被拆成三个独立控件分别填写：卖点提炼 / 类目 / 适用人群。
    全部由 _build_title() 从来源 name 推导，不是 JSON 里的现成字段：
        - selling_point：商品名去除品牌、适用人群、类目后缀后的剩余文字；
        - category：从 known_categories 匹配到的服饰类别（如“卫衣”）；
        - audience：从商品名关键词识别（男款/女款/男女同款/儿童款），
          识别不到时兜底“通用”。
    """
    selling_point: str
    category: str
    audience: str


@dataclass(frozen=True)
class SkuData:
    """单个销售规格（SKU）行。

    字段来源对照（见模块顶部“JSON 字段 → 内部字段 对照表”）：
        - color / size：定位键，来自 skus[].attributes 中的“颜色”“尺码”，
          必须与页面按颜色×尺码生成的规格组合完全一致；
        - product_code：来自 skus[].code（来源 SKU 编码），权威值直接填写；
        - auxiliary_code：来自 skus[].supplierSkuCode，缺省时回退到 product_code；
        - source_price：来自 skus[].price，只保留作诊断，不参与页面填写；
        - offer_amount：固定等于吊牌价 release_price（写死规则）；
        - inventory：固定等于 FIXED_SKU_INVENTORY=1000（写死规则）。
    price 和 inventory 用 Decimal/int，避免二进制浮点精度误差。
    """
    color: str
    size: str
    product_code: str
    auxiliary_code: str
    source_price: Decimal
    offer_amount: Decimal
    inventory: int


@dataclass(frozen=True)
class MediaReferences:
    """图片引用（仅文件名/相对路径，尚未访问磁盘）。

    直接从所选 imageSets 套图的图片字段拷贝而来，只做去空格、去空串的
    归一化，不校验文件是否存在。真正的磁盘定位在 extract_and_resolve_media()
    中按 basename + 目录双重条件完成。字段与 JSON 的对应关系见模块顶部
    对照表（mainImagePaths / detailImagePaths / firstSquarePaths /
    firstLongPaths / colorDewuPaths）。
    """
    main: tuple[str, ...]
    details: tuple[str, ...]
    first_square: tuple[str, ...]
    first_long: tuple[str, ...]
    carousel_by_color: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class ProductData:
    """从来源 JSON 归一化后的只读商品快照。

    供 main.py 的预检与浏览器阶段共同使用，冻结后不可修改，避免填写过程中
    被意外改写。字段来源说明：
        - source_id：来源 JSON 的 id；
        - code：来源 JSON 的 code（商品编码，参与图片工作目录名）；
        - source_name：来源 JSON 的 name（商品名，标题与属性推导的原料）；
        - brand：优先取套图 shopName，缺失时用商品名首段兜底；
        - category_path：来自 categoryPath，按“/”拆分；
        - item_no：来源 JSON 的 itemNumber（货号，页面货号输入框）；
        - audience：固定写死“通用”（业务规则）；
        - title / release_price / release_season：由 name + attributes 推导；
        - attributes：得物页面属性，字段名已映射为页面控件名（见
          _build_dewu_attributes）；
        - inferred_attributes：哪些属性不是来源原值而是按商品名推导的；
        - colors / sizes / skus：来自 skus[] 数组的归一化结果；
        - media_references：图片引用（见 MediaReferences）；
        - external_link：固定写死 FIXED_EXTERNAL_LINK="无"；
        - warnings：解析过程中产生的可解释性提示，供预检展示。
    """
    source_id: int
    code: str
    source_name: str
    brand: str
    category_path: tuple[str, ...]
    item_no: str
    audience: str
    title: TitleParts
    release_price: Decimal
    release_season: str
    attributes: Mapping[str, tuple[str, ...]]
    inferred_attributes: tuple[str, ...]
    colors: tuple[str, ...]
    sizes: tuple[str, ...]
    skus: tuple[SkuData, ...]
    media_references: MediaReferences
    external_link: str
    warnings: tuple[str, ...]

    @property
    def sku_by_variant(self) -> Mapping[tuple[str, str], SkuData]:
        # 页面填写 SKU 时需要按“颜色 + 尺码”快速定位来源记录，因此在使用处构建索引。
        return {(sku.color, sku.size): sku for sku in self.skus}


@dataclass(frozen=True)
class MediaFiles:
    """ZIP 解压后的真实磁盘路径，按页面图片区块重新组织。

    由 extract_and_resolve_media() 产出，把 MediaReferences 的引用解析为
    磁盘上唯一存在的文件。区块与页面用途的对应：
        - carousel_by_color：每个颜色至少正、背两张“得物平铺图”，第一张
          作为穿搭效果，第二张作为商品展示（fronts / backs）；
        - product_display_backs：商品展示区，来自各颜色的第二张平铺图；
        - outfit_fronts：穿搭效果区，来自各颜色的第一张平铺图；
        - details：细节呈现区，来自 detailImagePaths；
        - main / first_square / first_long：方图、第一张方图、第一张长图，
          其中 first_square[0] 用于起始页上传。
    图片所在目录是 _resolve_reference() 的匹配条件，保证不会拿错同名文件。
    """
    extraction_root: Path
    carousel_by_color: Mapping[str, tuple[Path, ...]]
    product_display_backs: tuple[Path, ...]
    outfit_fronts: tuple[Path, ...]
    details: tuple[Path, ...]
    main: tuple[Path, ...]
    first_square: tuple[Path, ...]
    first_long: tuple[Path, ...]
    warnings: tuple[str, ...]


def load_product(
    json_path: Path,
) -> ProductData:
    """把来源 JSON 解析为不可变 ProductData。

    解析顺序：读取 JSON -> 拆除接口包装 -> 解析商品基础字段 -> 推导标题与
    属性 -> 校验 SKU 完整性 -> 收集图片引用。任何不安全或无法确定的数据
    都直接抛 ProductDataError，让 main.py 转成结构化 JSON，绝不带着脏数据
    进入浏览器阶段。字段与来源 JSON 的对应关系见模块顶部对照表。
    """
    payload = _read_json(json_path)
    data = _unwrap_api_payload(payload)

    # 商品编码用于来源 SKU 和图片工作目录；货号单独使用平台来源的 itemNumber。
    code = _required_text(data, "code")
    item_no = _required_text(data, "itemNumber")
    source_name = _required_text(data, "name").strip()
    category_path = tuple(
        part.strip() for part in _required_text(data, "categoryPath").split("/") if part.strip()
    )
    if not category_path:
        raise ProductDataError("categoryPath 不能为空")

    # 优先选择平台为“得物”的套图，避免多个平台套图存在时误用其他平台图片。
    image_set = _first_image_set(data)
    brand = str(image_set.get("shopName") or "").strip()
    if not brand:
        # 某些来源没有 shopName，只能用商品名的第一段作为保守兜底。
        brand = source_name.split(maxsplit=1)[0]

    # 属性同时保留“按名称分组”的结果和原始行，前者用于页面填写，后者用于
    # 吊牌价、上市时间、库存等带编码或附加字段的特殊解析。
    grouped_attributes, attribute_rows = _group_attributes(data.get("attributes"))
    release_price = _release_price(attribute_rows)
    release_season = _release_season(attribute_rows)
    title = _build_title(source_name, brand, category_path[-1])

    recommended_attributes, inferred_attributes = _build_dewu_attributes(
        source_name,
        release_season,
        grouped_attributes,
        attribute_rows,
    )

    colors, sizes, skus, sku_warnings = _build_skus(
        data.get("skus"),
        code=code,
        release_price=release_price,
    )

    # 来源汇总库存只用于提示；页面实际填写的是固定的每条 SKU 库存。
    warnings = list(sku_warnings)
    summary_inventory = _attribute_number(attribute_rows, "kucun", "库存数量")
    sku_inventory = sum(sku.inventory for sku in skus)
    if summary_inventory is not None and int(summary_inventory) != sku_inventory:
        warnings.append(
            f"固定 SKU 库存合计 {sku_inventory} 与来源汇总库存 {int(summary_inventory)} 不一致；"
            f"这是预期行为，销售规格每条 SKU 固定填写 {FIXED_SKU_INVENTORY}"
        )

    media_references = _build_media_references(image_set)

    return ProductData(
        source_id=int(data.get("id") or 0),
        code=code,
        source_name=source_name,
        brand=brand,
        category_path=category_path,
        item_no=item_no,
        audience="通用",
        title=title,
        release_price=release_price,
        release_season=release_season,
        attributes=recommended_attributes,
        inferred_attributes=inferred_attributes,
        colors=colors,
        sizes=sizes,
        skus=skus,
        media_references=media_references,
        external_link=FIXED_EXTERNAL_LINK,
        warnings=tuple(warnings),
    )


def extract_and_resolve_media(
    product: ProductData,
    zip_path: Path,
    work_root: Path,
) -> MediaFiles:
    """解压图片 ZIP 并把 JSON 中的图片引用解析为唯一磁盘路径。

    分两步：
        1. 幂等解压：按 ZIP 内容 SHA-256 前缀做缓存目录名，已解压且带
           .complete 标记时直接复用，重复运行不会反复解压；
        2. 路径解析：把 MediaReferences 中的每个引用按 basename + 目录
           双重条件定位到唯一文件，零个/多个匹配都报错，防止静默选错图。
    产出 MediaFiles 后，main.py 的 _validate_media_for_page() 还会按得物
    页面限制（格式/数量/大小）再校验一遍。
    """
    zip_path = zip_path.expanduser().resolve()
    if not zip_path.is_file():
        raise ProductDataError(f"图片压缩包不存在：{zip_path}")

    work_root = work_root.expanduser().resolve()
    # 商品编码直接参与目录名，因此必须先经过路径安全检查。
    product_work_root = _safe_work_directory(work_root, product.code)
    if product_work_root.is_symlink():
        raise ProductDataError(f"图片工作目录不能是符号链接：{product_work_root}")
    if product_work_root.exists() and not product_work_root.is_dir():
        raise ProductDataError(f"图片工作路径不是目录：{product_work_root}")
    product_work_root.mkdir(parents=True, exist_ok=True)
    extraction_root = _extract_zip_atomically(zip_path, product_work_root)

    # 主图、详情图等按来源字段解析；缺失图片会在 _resolve_reference 中明确失败。
    refs = product.media_references
    main = tuple(_resolve_reference(extraction_root, ref, "方图") for ref in refs.main)
    first_square = tuple(
        _resolve_reference(extraction_root, ref, "第一张方图") for ref in refs.first_square
    )
    first_long = tuple(
        _resolve_reference(extraction_root, ref, "第一张长图") for ref in refs.first_long
    )
    details = tuple(_resolve_reference(extraction_root, ref, "详情图") for ref in refs.details)

    carousel_by_color: dict[str, tuple[Path, ...]] = {}
    fronts: list[Path] = []
    backs: list[Path] = []
    warnings: list[str] = []
    for color in product.colors:
        # 每个颜色至少需要正面和背面两张平铺图；第一张用于穿搭效果，第二张用于商品展示。
        color_refs = refs.carousel_by_color.get(color, ())
        if not color_refs:
            raise ProductDataError(f"颜色“{color}”没有得物平铺图")
        files = tuple(
            _resolve_reference(extraction_root, ref, "颜色图_得物平铺图")
            for ref in color_refs
        )
        if len(files) < 2:
            raise ProductDataError(f"颜色“{color}”必须至少有正面、背面两张平铺图")
        carousel_by_color[color] = files
        fronts.append(files[0])
        backs.append(files[1])

    if not details:
        warnings.append("来源 JSON 和 ZIP 均没有详情图，运行时将跳过“细节呈现”")
    if first_long and first_long[0].stat().st_size < 10_000:
        warnings.append("第一张长图文件很小，可能是空白图；主流程不会使用它")

    return MediaFiles(
        extraction_root=extraction_root,
        carousel_by_color=carousel_by_color,
        product_display_backs=tuple(backs),
        outfit_fronts=tuple(fronts),
        details=details,
        main=main,
        first_square=first_square,
        first_long=first_long,
        warnings=tuple(warnings),
    )


def _read_json(path: Path) -> Mapping[str, Any]:
    # 使用 utf-8-sig 兼容带 BOM 的导出文件，并把底层 JSON 异常转换为业务错误。
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ProductDataError(f"JSON 文件不存在：{path}")
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            payload = json.load(file)
    except json.JSONDecodeError as error:
        raise ProductDataError(f"JSON 格式错误：{error}") from error
    if not isinstance(payload, Mapping):
        raise ProductDataError("JSON 顶层必须是对象")
    return payload


def _unwrap_api_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    # 来源既可能是完整接口响应，也可能是已经取出的 data 对象。
    # 统一在这里处理，后续逻辑只面对商品对象。
    if "data" in payload:
        if payload.get("code") not in (None, 0, 200):
            raise ProductDataError(
                f"来源接口返回失败：code={payload.get('code')}, message={payload.get('message')}"
            )
        data = payload.get("data")
    else:
        data = payload
    if not isinstance(data, Mapping):
        raise ProductDataError("JSON 中的 data 必须是商品对象")
    return data


def _required_text(data: Mapping[str, Any], key: str) -> str:
    # 所有必填文本在入口统一去空格并检查，避免空值在后续 XPath 或标题中扩散。
    value = str(data.get(key) or "").strip()
    if not value:
        raise ProductDataError(f"缺少必填来源字段：{key}")
    return value


def _first_image_set(data: Mapping[str, Any]) -> Mapping[str, Any]:
    # imageSets 可能包含多个平台的图片套图；优先精确匹配“得物”，否则使用首个有效对象。
    image_sets = data.get("imageSets")
    if not isinstance(image_sets, Sequence) or isinstance(image_sets, (str, bytes)):
        raise ProductDataError("imageSets 必须是数组")
    candidates = [item for item in image_sets if isinstance(item, Mapping)]
    if not candidates:
        raise ProductDataError("来源商品没有图片套图")
    preferred = next((item for item in candidates if item.get("platform") == "得物"), None)
    return preferred or candidates[0]


def _group_attributes(
    raw_attributes: Any,
) -> tuple[Mapping[str, tuple[str, ...]], tuple[Mapping[str, Any], ...]]:
    # grouped 用于常规表单字段，rows 保留原始属性以便读取 attributeCode、子值等信息。
    if not isinstance(raw_attributes, Sequence) or isinstance(raw_attributes, (str, bytes)):
        raise ProductDataError("attributes 必须是数组")
    rows = tuple(row for row in raw_attributes if isinstance(row, Mapping))
    grouped: dict[str, list[str]] = {}
    for row in rows:
        name = str(row.get("attributeName") or "").strip()
        value = _attribute_scalar(row)
        if not name or value is None:
            continue
        text = str(value).strip()
        if text and text not in grouped.setdefault(name, []):
            grouped[name].append(text)
    return {name: tuple(values) for name, values in grouped.items()}, rows


def _attribute_scalar(row: Mapping[str, Any]) -> Any:
    # 不同版本的来源接口可能把属性值放在不同字段，按优先级取第一个非空值。
    for key in ("attributeValue", "valueText", "valueNumber"):
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _find_attribute_rows(
    rows: Iterable[Mapping[str, Any]],
    code: str,
    name: str,
) -> list[Mapping[str, Any]]:
    # 同时支持属性编码和中文名称匹配，兼容接口字段编码大小写差异。
    code_folded = code.casefold()
    return [
        row
        for row in rows
        if str(row.get("attributeCode") or "").casefold() == code_folded
        or str(row.get("attributeName") or "").strip() == name
    ]


def _release_price(rows: Sequence[Mapping[str, Any]]) -> Decimal:
    # 吊牌价是页面发售价格的基础，也是来源 SKU price 为 0 时的出价计算基准。
    matches = _find_attribute_rows(rows, "diaopaijia", "吊牌价")
    if not matches:
        raise ProductDataError("来源属性缺少吊牌价")
    value = _attribute_scalar(matches[0])
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise ProductDataError(f"吊牌价格式错误：{value}") from error
    if not price.is_finite() or price <= 0:
        raise ProductDataError("吊牌价必须大于 0")
    return price


def _release_season(rows: Sequence[Mapping[str, Any]]) -> str:
    # 得物属性只接受春/夏/秋/冬等季节值，因此从上市时间文本中提取首个可识别季节。
    matches = _find_attribute_rows(rows, "sssj", "上市时间")
    text = str(_attribute_scalar(matches[0]) if matches else "")
    for season in ("春", "夏", "秋", "冬"):
        if season in text:
            return season
    raise ProductDataError(f"无法从上市时间识别季节：{text or '空'}")


def _attribute_number(
    rows: Sequence[Mapping[str, Any]],
    code: str,
    name: str,
) -> Decimal | None:
    # 库存汇总是可选的提示字段，格式不正确时返回 None 而不是阻断 SKU 解析。
    matches = _find_attribute_rows(rows, code, name)
    if not matches:
        return None
    value = _attribute_scalar(matches[0])
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    return result if result.is_finite() else None


def _build_title(source_name: str, brand: str, source_category: str) -> TitleParts:
    # 标题字段必须拆开填入页面，因此先识别适用人群和类目，再从商品名中
    # 去除已经被单独使用的部分，把剩余文本作为卖点。
    audience = next(
        (value for value in ("男女同款", "男款", "女款", "儿童款") if value in source_name),
        "通用",
    )
    category = _category_segment(source_name, source_category)

    # 下面按后缀剥离品牌、适用人群和类目，尽量避免结构化标题重复出现同一段文字。
    candidate = source_name.strip()
    if brand and candidate.startswith(brand):
        candidate = candidate[len(brand) :].strip()
    if audience != "通用" and candidate.endswith(audience):
        candidate = candidate[: -len(audience)].strip()
    if category and candidate.endswith(category):
        candidate = candidate[: -len(category)].strip()

    # 页面标题总长度留出少量余量，避免后续品牌或字段规范化后超过平台上限。
    max_total = 58
    max_selling_point = max(2, max_total - len(brand) - len(category) - len(audience))
    selling_point = candidate[: min(24, max_selling_point)].strip(" ，,、")
    if len(selling_point) < 2:
        selling_point = source_name[: min(24, max_selling_point)].strip()
    return TitleParts(selling_point=selling_point, category=category, audience=audience)


def _category_segment(source_name: str, source_category: str) -> str:
    # 先从商品名和来源类目中匹配常见服饰类别；没有命中时再清理“男士/女士”前缀。
    known_categories = (
        "羽绒服",
        "冲锋衣",
        "夹克",
        "卫衣",
        "毛衣",
        "针织衫",
        "衬衫",
        "T恤",
        "裤",
        "裙",
        "鞋",
    )
    for category in known_categories:
        if category in source_name or category in source_category:
            return category
    cleaned = source_category.replace("男士", "").replace("女士", "").strip()
    return cleaned or source_category


def _build_dewu_attributes(
    source_name: str,
    release_season: str,
    grouped: Mapping[str, tuple[str, ...]],
    rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, tuple[str, ...]], tuple[str, ...]]:
    # 来源属性名称与得物表单名称并不完全一致，这里集中做字段映射和可解释的推导。
    # inferred 用来记录哪些字段不是来源原值，而是根据商品名规则推出来的。
    attributes: dict[str, tuple[str, ...]] = {}
    inferred: list[str] = []

    def copy(source: str, target: str | None = None) -> None:
        # 对名称相同或仅需改名的来源属性做浅拷贝，保留来源给出的多选值。
        values = grouped.get(source)
        if values:
            attributes[target or source] = values

    copy("领型")
    copy("风格")
    copy("穿着方式", "衣门襟")
    copy("材质", "面料")

    # 材质含量由材质名称和 subValueNumber 组成一个页面文本字段。
    material_rows = _find_attribute_rows(rows, "caizhi", "材质")
    if material_rows:
        material = str(_attribute_scalar(material_rows[0]) or "").strip()
        percentage = material_rows[0].get("subValueNumber")
        if material and percentage not in (None, ""):
            attributes["成分含量"] = (f"{material}{percentage}%",)

    # 来源的“商品类型与品牌”子值对应得物页面的“设计元素”，可能有多行。
    design_element_rows = _find_attribute_rows(rows, "leixing-pinpai", "商品类型与品牌")
    design_elements = tuple(
        str(row.get("subValueText") or "").strip()
        for row in design_element_rows
        if str(row.get("subValueText") or "").strip()
    )
    if design_elements:
        attributes["设计元素"] = tuple(dict.fromkeys(design_elements))

    # 上市时间已在 _release_season 中归一化为平台可接受的季节值。
    attributes["适用季节"] = (release_season,)

    # 以下字段来源通常没有独立标准值，因此按商品标题中的明确关键词推导。
    if "长袖" in source_name:
        attributes["袖长"] = ("长袖",)
        inferred.append("袖长")
    elif "短袖" in source_name:
        attributes["袖长"] = ("短袖",)
        inferred.append("袖长")

    if "短款" in source_name:
        attributes["衣长"] = ("短款",)
    elif "中长款" in source_name:
        attributes["衣长"] = ("中长款",)
    elif "长款" in source_name:
        attributes["衣长"] = ("长款",)
    else:
        attributes["衣长"] = ("常规款",)
    inferred.append("衣长")

    lowered_name = source_name.casefold()
    if "宽松" in source_name or "oversize" in lowered_name or "cleanfit" in lowered_name:
        attributes["版型"] = ("宽松",)
    elif "修身" in source_name:
        attributes["版型"] = ("修身",)
    else:
        attributes["版型"] = ("合身",)
    inferred.append("版型")

    if "加厚" in source_name or "厚款" in source_name:
        attributes["厚度"] = ("加厚",)
    elif "薄款" in source_name or "轻薄" in source_name:
        attributes["厚度"] = ("薄款",)
    else:
        attributes["厚度"] = ("适中",)
    inferred.append("厚度")

    attributes["是否加绒"] = ("加绒" if "加绒" in source_name and "不加绒" not in source_name else "不加绒",)
    inferred.append("是否加绒")

    return attributes, tuple(dict.fromkeys(inferred))


def _build_skus(
    raw_skus: Any,
    *,
    code: str,
    release_price: Decimal,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[SkuData, ...], tuple[str, ...]]:
    # SKU 是最严格的数据边界：既要保证每行结构正确，也要保证颜色×尺码组合完整。
    # 任何缺失、重复或负库存都会在进入浏览器前失败，避免页面半填状态。
    if not isinstance(raw_skus, Sequence) or isinstance(raw_skus, (str, bytes)):
        raise ProductDataError("skus 必须是数组")
    if release_price <= 0:
        raise ProductDataError("吊牌价必须大于 0")

    colors: list[str] = []
    sizes: list[str] = []
    skus: list[SkuData] = []
    seen: set[tuple[str, str]] = set()
    warnings: list[str] = []

    for index, row in enumerate(raw_skus):
        # 每个来源 SKU 先抽取颜色和尺码作为唯一键，再解析编码、价格和库存。
        if not isinstance(row, Mapping):
            raise ProductDataError(f"skus[{index}] 必须是对象")
        attributes = row.get("attributes")
        if not isinstance(attributes, Sequence) or isinstance(attributes, (str, bytes)):
            raise ProductDataError(f"skus[{index}].attributes 必须是数组")
        color = _sku_attribute(attributes, "颜色", index)
        size = _sku_attribute(attributes, "尺码", index)
        variant = (color, size)
        if variant in seen:
            raise ProductDataError(f"SKU 规格重复：{color}/{size}")
        seen.add(variant)
        if color not in colors:
            colors.append(color)
        if size not in sizes:
            sizes.append(size)

        product_code = str(row.get("code") or "").strip()
        auxiliary_code = str(row.get("supplierSkuCode") or product_code).strip()
        if not product_code:
            raise ProductDataError(f"SKU {color}/{size} 缺少 code")

        raw_source_price = row.get("price")
        source_price = _decimal(
            0 if raw_source_price in (None, "") else raw_source_price,
            f"SKU {color}/{size} price",
        )
        # 来源 SKU price 只保留作诊断；页面上的每条 SKU 始终填写完整吊牌价。
        offer_amount = release_price
        inventory_value = _decimal(row.get("stock", 0), f"SKU {color}/{size} stock")
        if inventory_value != inventory_value.to_integral_value():
            raise ProductDataError(f"SKU {color}/{size} 库存必须是整数：{inventory_value}")
        if inventory_value < 0:
            raise ProductDataError(f"SKU {color}/{size} 来源库存不能为负数")
        # 来源 stock 不参与页面填写，每条 SKU 按业务规则固定为 1000。
        inventory = FIXED_SKU_INVENTORY

        # 编码格式不一致只给出警告，因为来源编码仍然是页面应填写的权威值。
        expected_fragment = f"{code}-{color}{size}"
        if expected_fragment not in product_code:
            warnings.append(
                f"SKU {color}/{size} 编码“{product_code}”不含标准片段“{expected_fragment}”，"
                "仍按来源原值填写"
            )

        skus.append(
            SkuData(
                color=color,
                size=size,
                product_code=product_code,
                auxiliary_code=auxiliary_code,
                source_price=source_price,
                offer_amount=offer_amount,
                inventory=inventory,
            )
        )

    if not colors or not sizes or not skus:
        raise ProductDataError("来源商品必须至少包含一个颜色、尺码和 SKU")

    # 页面会按完整笛卡尔积生成销售规格；来源必须覆盖所有颜色和尺码组合。
    expected_variants = {(color, size) for color in colors for size in sizes}
    missing_variants = expected_variants - seen
    if missing_variants:
        missing_text = "、".join(
            f"{color}/{size}"
            for color in colors
            for size in sizes
            if (color, size) in missing_variants
        )
        raise ProductDataError(f"来源 SKU 不是完整的颜色×尺码组合，缺少：{missing_text}")

    warnings.extend(
        (
            f"SKU 出价固定使用吊牌价 {release_price}，忽略来源 SKU price",
            f"每条 SKU 库存固定为 {FIXED_SKU_INVENTORY}，忽略来源 stock",
        )
    )

    return tuple(colors), tuple(sizes), tuple(skus), tuple(warnings)


def _sku_attribute(attributes: Sequence[Any], name: str, sku_index: int) -> str:
    # 从单个 SKU 的属性数组中读取指定维度，缺失时不能猜测，否则会错配库存或编码。
    for item in attributes:
        if isinstance(item, Mapping) and str(item.get("attributeName") or "").strip() == name:
            value = str(item.get("attributeValue") or "").strip()
            if value:
                return value
    raise ProductDataError(f"skus[{sku_index}] 缺少{name}属性")


def _decimal(value: Any, field: str) -> Decimal:
    # 统一把数值转换成有限 Decimal，供价格和库存校验复用。
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise ProductDataError(f"{field} 不是合法数字：{value}") from error
    if not result.is_finite():
        raise ProductDataError(f"{field} 必须是有限数字：{value}")
    return result


def _build_media_references(image_set: Mapping[str, Any]) -> MediaReferences:
    # 只做字段形状归一化，不在这里访问磁盘；文件是否存在由后续解压解析阶段判断。
    carousel_raw = image_set.get("colorDewuPaths") or {}
    if not isinstance(carousel_raw, Mapping):
        raise ProductDataError("colorDewuPaths 必须是颜色到图片数组的对象")
    carousel = {
        str(color).strip(): _string_tuple(paths)
        for color, paths in carousel_raw.items()
        if str(color).strip()
    }
    return MediaReferences(
        main=_string_tuple(image_set.get("mainImagePaths")),
        details=_string_tuple(image_set.get("detailImagePaths")),
        first_square=_string_tuple(image_set.get("firstSquarePaths")),
        first_long=_string_tuple(image_set.get("firstLongPaths")),
        carousel_by_color=carousel,
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    # 图片字段统一转换成去空格、去空字符串的不可变序列，便于后续稳定遍历。
    if value in (None, ""):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProductDataError(f"图片字段必须是数组，实际为 {type(value).__name__}")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _safe_extract_zip(zip_path: Path, destination: Path) -> None:
    # 解压前先检查数量、单文件大小、总大小和 CRC；解压时再次验证路径，
    # 防止压缩包中的绝对路径或“..”路径写出目标目录。
    try:
        with ZipFile(zip_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS:
                raise ProductDataError(
                    f"ZIP 文件数量过多：{len(members)}，上限 {MAX_ZIP_MEMBERS}"
                )
            total_size = sum(info.file_size for info in members if not info.is_dir())
            if total_size > MAX_ZIP_TOTAL_BYTES:
                raise ProductDataError(
                    f"ZIP 解压后总大小过大：{total_size} 字节，上限 {MAX_ZIP_TOTAL_BYTES} 字节"
                )
            oversized = next(
                (
                    info
                    for info in members
                    if not info.is_dir() and info.file_size > MAX_ZIP_MEMBER_BYTES
                ),
                None,
            )
            if oversized is not None:
                raise ProductDataError(
                    f"ZIP 单个文件过大：{oversized.filename} ({oversized.file_size} 字节)"
                )

            # testzip 会读取每个成员并校验 CRC，提前发现损坏文件而不是写出半套图片。
            bad_member = archive.testzip()
            if bad_member:
                raise ProductDataError(f"ZIP CRC 校验失败：{bad_member}")
            for info in members:
                # ZIP 内部路径使用 POSIX 分隔符，先规范化为本地 Path 再做越界检查。
                if info.flag_bits & 0x1:
                    raise ProductDataError(f"ZIP 包含加密文件，无法自动解压：{info.filename}")
                relative = Path(PurePosixPath(info.filename))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ProductDataError(f"ZIP 包含不安全路径：{info.filename}")
                target = (destination / relative).resolve()
                if destination != target and destination not in target.parents:
                    raise ProductDataError(f"ZIP 路径越界：{info.filename}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except BadZipFile as error:
        raise ProductDataError(f"图片压缩包损坏：{zip_path}") from error


def _extract_zip_atomically(zip_path: Path, product_work_root: Path) -> Path:
    # 用 ZIP 的 SHA-256 前缀作为缓存目录名；完整标记文件存在且内容匹配时可直接复用。
    digest = _file_digest(zip_path)
    destination = (product_work_root / digest[:16]).resolve()
    if destination.parent != product_work_root:
        raise ProductDataError(f"图片解压目录越界：{destination}")

    marker = destination / ".complete"
    if marker.is_file() and marker.read_text(encoding="ascii").strip() == digest:
        return destination
    if destination.exists():
        raise ProductDataError(f"发现未完成的图片解压目录，请人工检查：{destination}")

    # 先写入同一父目录下的临时目录，全部成功后再 rename，避免留下看似完整的半成品目录。
    temporary = Path(tempfile.mkdtemp(prefix=".extract-", dir=product_work_root)).resolve()
    try:
        _safe_extract_zip(zip_path, temporary)
        (temporary / ".complete").write_text(digest, encoding="ascii")
        try:
            temporary.rename(destination)
        except OSError as error:
            if marker.is_file() and marker.read_text(encoding="ascii").strip() == digest:
                shutil.rmtree(temporary)
            else:
                raise ProductDataError(f"无法完成图片解压目录切换：{destination}") from error
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return destination


def _file_digest(path: Path) -> str:
    # 分块计算哈希，避免一次性把大 ZIP 全部读入内存。
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_work_directory(work_root: Path, product_code: str) -> Path:
    # 商品编码会被拼进目录路径，只允许普通单层目录名，拒绝路径分隔符和“..”。
    code = product_code.strip()
    if not code or code in {".", ".."} or "/" in code or "\\" in code:
        raise ProductDataError(f"商品编码不能作为安全目录名：{product_code!r}")
    target = (work_root / code).resolve()
    if target.parent != work_root:
        raise ProductDataError(f"商品编码导致图片目录越界：{product_code!r}")
    return target


def _resolve_reference(root: Path, reference: str, folder_name: str) -> Path:
    # 来源可能只保存文件名或带有原始目录，因此按 basename 搜索，并要求命中对应图片区目录。
    # 只能唯一命中；零个和多个都报错，避免上传错误图片。
    basename = PurePosixPath(reference).name
    candidates = [
        path.resolve()
        for path in root.rglob(basename)
        if path.is_file() and folder_name in path.parts
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ProductDataError(
            f"ZIP 中找不到图片：目录={folder_name}，文件={basename}，来源={reference}"
        )
    raise ProductDataError(
        f"ZIP 中图片不唯一：目录={folder_name}，文件={basename}，匹配={len(candidates)}"
    )
