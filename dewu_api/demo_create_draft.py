"""全链路示例：来源 JSON + 配置 Excel → /dop/api/v2/nps/create（create_type=2 草稿）。

用法（在 dewu_api/ 目录下）：
  python demo_create_draft.py                          # dry-run：完整组装入参并打印（不联网）
  python demo_create_draft.py --json ../p2430.json     # 指定商品 JSON
  python demo_create_draft.py --send                   # 真实调用沙箱（需 .env 已填 Secret + IP 白名单）
  python demo_create_draft.py --send --env prod        # 切生产
  python demo_create_draft.py --sign-style=underscore  # 签名算法切换（联调纠偏用）

dry-run 不需要凭证：图片用占位 img_key、映射缺 id 用 0 占位，缺口全部打进
_warnings，等价于一份“待人工补全清单”。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))            # dewu_api_client / payload_builder
sys.path.insert(0, str(_THIS_DIR.parent))     # models / config_loader

from dewu_api_client import DewuApiError, DewuClient  # noqa: E402
from payload_builder import (  # noqa: E402
    PayloadError,
    build_create_payload,
    load_mappings,
    upload_carousel_images,
)


def _placeholder_images(product) -> dict[str, list[str]]:
    return {
        color: [f"DRYRUN-{color}-{i + 1}" for i, _ in enumerate(paths)]
        for color, paths in product.media_references.carousel_by_color.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default=str(_THIS_DIR.parent / "p2430.json"), help="商品来源 JSON 路径")
    parser.add_argument("--config", default=str(_THIS_DIR.parent / "配置文件"), help="配置 Excel 目录")
    parser.add_argument("--send", action="store_true", help="真实调用（默认 dry-run 不联网）")
    parser.add_argument("--env", default="sandbox", choices=["sandbox", "prod"])
    args = parser.parse_args()

    import config_loader
    import models

    cfg = config_loader.load_config(Path(args.config))
    product = models.load_product(Path(args.json))
    mappings = load_mappings()

    print(f"商品：{product.source_name}")
    print(f"品牌：{product.brand}｜类目：{'>'.join(cfg.category_fallback)}｜人群：{cfg.applicable_crowd}")
    print(f"颜色×尺码：{len(product.colors)} 色 × {len(product.sizes)} 码，吊牌价 {product.release_price} 元")
    print()

    client = None
    if args.send:
        try:
            client = DewuClient(env=args.env)
        except ValueError as exc:
            print(f"[凭证缺失] {exc}")
            return 2

    # 图片链路：真实调用时解压 ZIP → 逐张 upload_media；dry-run 用占位 key
    upload_warnings: list[str] = []
    if client is not None:
        zip_path = _THIS_DIR.parent / "work" / "input.zip"  # TODO: 换成任务实际套图 ZIP 路径
        if not zip_path.is_file():
            print(f"[缺图片 ZIP] {zip_path} 不存在；真实调用需提供套图 ZIP（接口严禁整包上传，仅本地解压用）")
            return 2
        media = models.extract_and_resolve_media(
            product, zip_path, _THIS_DIR.parent / "work"
        )
        img_keys_by_color, upload_warnings = upload_carousel_images(client, media)
    else:
        img_keys_by_color = _placeholder_images(product)

    try:
        payload = build_create_payload(
            product, cfg, mappings, img_keys_by_color,
            allow_missing=client is None,  # dry-run 允许缺口占位
        )
    except PayloadError as exc:
        print(f"[入参构造失败] {exc}")
        return 1

    for w in [*cfg.warnings, *upload_warnings, *payload["_warnings"]]:
        print("⚠ ", w)

    api_body = {k: v for k, v in payload.items() if not k.startswith("_")}
    print("=== create 入参 ===")
    print(json.dumps(api_body, ensure_ascii=False, indent=2))

    if client is None:
        print("\ndry-run 结束（未联网）。补齐上方[缺口]并填好 .env 后加 --send 真实调用。")
        return 0

    print(f"\n=== 真实调用 {args.env} ===")
    try:
        result = client.request("/dop/api/v2/nps/create", api_body)
    except DewuApiError as exc:
        print("调用失败：", exc)
        print("若为签名错误：对照官方「签名规则与事例」换 --sign-style 重试。")
        return 1
    data = result.get("data") or {}
    print("success :", data.get("success"))
    print("draft_id:", data.get("draft_id"))
    print("spu_id  :", data.get("spu_id"))
    print("完整返回:", json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if data.get("draft_id") else 1


if __name__ == "__main__":
    sys.exit(main())
