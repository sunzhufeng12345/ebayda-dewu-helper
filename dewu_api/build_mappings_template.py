"""生成/更新 类目名→id 映射表模板 dewu_api/mappings.json（可重复运行，幂等）。

数据源：项目根的 类目词表_服装.txt / 类目词表_女装.txt（每行“服装/上衣/卫衣”）。
- 首次运行：全部条目 category_id=null，即“缺口清单”本体；
- 人工补全 id 后再运行：已填的 id 保留，只追加新类目行；
- 品牌：brands 区块留空，按实际经营的店铺名手工补 brand_id。

缓存策略：mappings.json 就是本地缓存；若后续确认开放平台提供类目树/品牌
查询接口，可在此脚本里加“从接口刷新”分支，人工 id 仍优先。
"""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_THIS_DIR = Path(__file__).resolve().parent
WORDLISTS = [_ROOT / "类目词表_服装.txt", _ROOT / "类目词表_女装.txt"]
OUT = _THIS_DIR / "mappings.json"


def main() -> None:
    old = {}
    if OUT.is_file():
        old = json.loads(OUT.read_text(encoding="utf-8"))
    old_categories = old.get("categories", {})
    old_brands = old.get("brands", {})

    categories: dict[str, dict] = {}
    for wl in WORDLISTS:
        if not wl.is_file():
            continue
        for line in wl.read_text(encoding="utf-8").splitlines():
            path = ">>".join(p.strip() for p in line.strip().split("/") if p.strip())
            if not path:
                continue
            # 保留已人工补全的 id；新行补 null 占位
            categories[path] = old_categories.get(path, {"category_id": None})

    missing = sorted(k for k, v in categories.items() if not v.get("category_id"))
    data = {
        "_说明": "category_id/brand_id 为得物数字 id，null=待人工补全；改完无需重跑本脚本",
        "categories": dict(sorted(categories.items())),
        "brands": old_brands,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"mappings.json 已生成：类目 {len(categories)} 条，其中缺 id {len(missing)} 条")
    print(f"品牌区块现有 {len(old_brands)} 条（按店铺名手工补）")
    if missing:
        preview = "、".join(missing[:10])
        print(f"缺口示例：{preview}{' …' if len(missing) > 10 else ''}")


if __name__ == "__main__":
    main()
