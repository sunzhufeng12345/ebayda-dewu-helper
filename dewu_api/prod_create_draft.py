"""生产环境全链路建草稿（任务9）：真实数据 → upload_media → nps/create。

前提：.env 有生产凭证、tokens.json 有商家授权 access_token、IP 白名单含本机出口。
用法：python3 prod_create_draft.py [--json ../p2430.json] [--img-root DIR] [--dry-run]
     --json   商品 JSON（默认 p2430.json）
     --img-root 按颜色分目录的套图根目录（默认 p2430 的解压目录）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, str(_THIS_DIR.parent))

from dewu_api_client import DewuApiError, DewuClient  # noqa: E402
from payload_builder import (  # noqa: E402
    build_create_payload,
    build_detail_images,
    build_size_recommend,
    build_try_on_report,
    fetch_props_template_api,
    load_mappings,
    sync_category_mappings,
    upload_detail_images,
)

# 品牌 id（2026-08-25 生产 product_pool 实查：店铺在售 永言）
BRAND_ID = 1048353
DEFAULT_JSON = _THIS_DIR.parent / "p2430.json"
DEFAULT_IMG_ROOT = _THIS_DIR.parent / "work" / "TH196-W62261" / "bdbcb7197fe3ef77" / "W62261" / "颜色图_得物平铺图"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default=str(DEFAULT_JSON), help="商品 JSON 路径")
    parser.add_argument("--img-root", default=str(DEFAULT_IMG_ROOT), help="按颜色分目录的套图根目录")
    parser.add_argument("--dry-run", action="store_true", help="只上传图片并打印报文，不调 create")
    parser.add_argument("--article-suffix", default="", help="货号追加后缀（测试去重用）")
    args = parser.parse_args()

    import config_loader
    import models

    cfg = config_loader.load_config(_THIS_DIR.parent / "配置文件")
    product = models.load_product(Path(args.json))
    img_root = Path(args.img_root)

    mappings = load_mappings()
    mappings["brands"][product.brand] = {"brand_id": BRAND_ID}
    key = ">>".join(cfg.category_fallback)

    client = DewuClient(env="prod")

    # ⓪ 官方元数据接口自动解析：query_category 类目树（自动填 mappings.json）
    # + query_property 属性模板（自动缓存，取代 stark 抓包）
    flat = sync_category_mappings(client, BRAND_ID)
    if key not in flat:
        print(f"类目「{key}」不在品牌类目树中！树内相近路径：",
              [p for p in flat if p.split(">>")[-1] == key.split(">>")[-1]][:5])
        return 1
    mappings["categories"][key] = {"category_id": flat[key]}
    props_tpl = fetch_props_template_api(client, flat[key], category_key=key)
    print(f"商品 {product.source_name} | 品牌 {product.brand}→{BRAND_ID} | 类目 {key}→{flat[key]}")
    print(f"官方属性模板：{len(props_tpl)} 项（query_property 自动获取）")
    print(f"颜色 {product.colors} × 尺码 {len(product.sizes)} 码")

    # ① 按颜色上传真实图片（已解压目录，跳过 ZIP）
    img_keys_by_color: dict[str, list[str]] = {}
    for color_dir in sorted(img_root.iterdir()):
        if not color_dir.is_dir() or color_dir.name not in product.colors:
            continue
        keys = []
        for img in sorted(color_dir.glob("*.jpg")) + sorted(color_dir.glob("*.png")):
            keys.append(client.upload_media(img))
            print(f"  上传 {color_dir.name}/{img.name} -> {keys[-1]}")
        if keys:
            img_keys_by_color[color_dir.name] = keys
    print(f"图片上传完成：{ {c: len(k) for c, k in img_keys_by_color.items()} }")

    # ①b 详情图三区块（商品展示/细节呈现/穿搭效果）+ 尺码推荐/试穿报告
    media_files = None
    detail_section_keys: dict = {}
    try:
        media_files = models.extract_and_resolve_media(
            product, _THIS_DIR.parent / "p2430.zip", _THIS_DIR.parent / ".dewu_work"
        )
    except Exception as exc:  # noqa: BLE001
        print("⚠ 媒体解析失败，跳过详情图三区块：", exc)
    if media_files is not None:
        detail_section_keys = upload_detail_images(client, media_files)
        print("详情图三区块：", {k: len(v) for k, v in detail_section_keys.items()})

    size_recommend = try_on = None
    try:
        import main as m
        size_rec_path = m._resolve_size_guidance_file(product.sizes, _THIS_DIR.parent / "配置文件", "尺码推荐")
        if size_rec_path:
            size_recommend = build_size_recommend(size_rec_path)
            print(f"尺码推荐：{len(size_recommend['split_report_table'])} 行 <- {Path(size_rec_path).name}")
        try_on_path = m._resolve_size_guidance_file(product.sizes, _THIS_DIR.parent / "配置文件", "试穿报告")
        if try_on_path:
            try_on = build_try_on_report(try_on_path)
            print(f"试穿报告：{len(try_on['report_table'])} 行 <- {Path(try_on_path).name}")
    except Exception as exc:  # noqa: BLE001
        print("⚠ 尺码推荐/试穿报告解析失败，跳过：", exc)

    first_color = product.colors[0] if product.colors else ""
    detail_images = build_detail_images(detail_section_keys, first_color) if detail_section_keys else None

    # ② 组装并打印完整报文
    payload = build_create_payload(
        product, cfg, mappings, img_keys_by_color,
        allow_missing=True,
        detail_images=detail_images,
        size_recommend=size_recommend,
        try_on_report=try_on,
    )
    for w in payload["_warnings"]:
        print("⚠ ", w)
    api_body = {k: v for k, v in payload.items() if not k.startswith("_")}
    if args.article_suffix:
        api_body["article_number"] = api_body["article_number"] + args.article_suffix
    Path("last_payload.json").write_text(json.dumps(api_body, ensure_ascii=False, indent=2), encoding="utf-8")
    print("报文已存 last_payload.json")

    if args.dry_run:
        return 0

    # ③ 真实建草稿（create_type=2，仅草稿不推审）
    print("\n=== 调用 /dop/api/v2/nps/create (create_type=2) ===")
    try:
        result = client.request("/dop/api/v2/nps/create", api_body)
    except DewuApiError as exc:
        print("调用失败：", exc)
        return 1
    data = result.get("data") or {}
    print("draft_id:", data.get("draft_id"))
    print("spu_id  :", data.get("spu_id"))
    pre = data.get("preCheckResponse")
    if pre:
        print("preCheck:", json.dumps(pre, ensure_ascii=False, indent=2)[:1500])
    Path("last_resp.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if isinstance(data.get("draft_id"), int) else 1


if __name__ == "__main__":
    sys.exit(main())
