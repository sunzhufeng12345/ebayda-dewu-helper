# -*- coding: utf-8 -*-
"""续跑脚本：挂到已开的草稿详情页，清掉尺码表默认行后复用 main.py 的
DewuAutomation 从尺码表步骤续跑到保存草稿，并监听保存/详情请求抓真实报文。

用法：.venv/bin/python dewu_api/resume_fill.py 1069270536
"""
import json
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from DrissionPage import Chromium, ChromiumOptions  # noqa: E402

import config_loader  # noqa: E402
import main as m  # noqa: E402
from models import extract_and_resolve_media, load_product  # noqa: E402

DRAFT_ID = sys.argv[1] if len(sys.argv) > 1 else "1069270536"
OUT = Path("/tmp/save_packets")


def main() -> int:
    cfg = config_loader.load_config(m.CONFIG_ROOT)
    m._apply_config_to_globals(cfg)
    product = load_product(ROOT / "p2430.json")
    if cfg.applicable_crowd != product.audience:
        from dataclasses import replace
        product = replace(product, audience=cfg.applicable_crowd)
    media = extract_and_resolve_media(product, ROOT / "p2430.zip", ROOT / ".dewu_work")
    size_rec = m._resolve_size_guidance_file(product.sizes, m.CONFIG_ROOT, "尺码推荐")
    try_on = m._resolve_size_guidance_file(product.sizes, m.CONFIG_ROOT, "试穿报告")
    print("尺码推荐:", size_rec, "| 试穿报告:", try_on)

    settings = m.RunSettings(
        debugger_port=9222,
        offer_type=cfg.offer_type,
        price_proof_source=cfg.price_proof_source,
        release_proof_source=cfg.release_proof_source,
        external_link=product.external_link,
        save_draft=True,  # 本次要真保存：抓保存请求的完整报文
        skip_size_chart=False,
        timeout=12.0,
        size_recommendation_file=size_rec,
        try_on_report_file=try_on,
    )
    result = m.RunResult(warnings=m._effective_warnings(product, media))

    co = ChromiumOptions().set_address("127.0.0.1:9222").existing_only()
    browser = Chromium(co)
    tab = None
    for t in browser.get_tabs():
        if DRAFT_ID in str(t.url):
            tab = t
            break
    if tab is None:
        print("NO_TAB", DRAFT_ID)
        return 1
    tab.set.activate()
    time.sleep(2)

    # 监听保存/详情请求（后台线程持续收包）
    OUT.mkdir(exist_ok=True)
    tab.listen.start()

    auto = m.DewuAutomation(tab, product, media, settings, result)

    # ① 清掉尺码表弹窗默认脚手架行的尺码名（XS-2XL → 空），绕开冲突保护。
    # 仅当整表恰为默认脚手架且无测量值时才清；已确认过的表不动（幂等重跑安全）。
    # 弹窗重开时 DOM 会整体重渲染，读取可能 ElementLost，重试即可。
    from DrissionPage.errors import ElementLostError

    for attempt in range(3):
        try:
            modal = auto._open_size_modal()
            table = auto._size_table(modal)
            rows = auto._size_rows(table)

            def _row_cells(row):
                return [i for i in row.eles("xpath:.//input") if m._is_displayed(i)]

            names_now = tuple(
                m._element_value(_row_cells(r)[0]) if _row_cells(r) else "" for r in rows
            )
            measurements_empty = all(
                not any(m._element_value(i) for i in _row_cells(r)[1:])
                for r in rows
                if _row_cells(r)
            )
            break
        except ElementLostError:
            if attempt == 2:
                raise
            time.sleep(2)
    if names_now == m.DEFAULT_SIZE_SCAFFOLD and measurements_empty:
        cleared = 0
        for row in auto._size_rows(auto._size_table(auto._open_size_modal())):
            inputs = _row_cells(row)
            if inputs and m._element_value(inputs[0]):
                inputs[0].clear()
                cleared += 1
        print("已清空脚手架尺码行:", cleared, "/", len(rows))
    else:
        print("非默认脚手架（当前行名:", names_now, "），跳过清空")

    # ② 从尺码表续跑到保存（run 的后半段，避开已完成的 basic/title/colors）
    auto._fill_sizes()
    auto._upload_size_guidance()
    auto._fill_skus()
    result.completed_sections.append("sales_info")
    auto._upload_carousel()
    auto._upload_detail_sections()
    result.completed_sections.append("media")
    auto._fill_external_link()
    result.completed_sections.append("supplement")
    errors = auto._read_visible_errors()
    if errors:
        result.validation_errors.extend(errors)
        print("页面错误:", errors)
    auto._save_draft()
    result.status = "draft_saved"

    # ③ 收包：保存请求的报文是本次目标
    idx = 0
    for packet in tab.listen.steps(timeout=15):
        try:
            url = packet.url or ""
            if url.split("?")[0].endswith((".js", ".css", ".png", ".jpg", ".woff")):
                continue
            body = packet.response.body
            if isinstance(body, bytes):
                body = body.decode("utf-8", "ignore")
            text = str(body)
            if any(k in url for k in ("save", "draft", "spu", "nps")) or "sizeTable" in text or "premiumImages" in text:
                idx += 1
                safe = (url.split("?")[0].rstrip("/").split("/")[-1])[:40]
                (OUT / f"save_{idx:02d}_{safe}.json").write_text(
                    json.dumps({"url": url, "body": text}, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
                print("PKT", idx, url[:110], "len=", len(text))
        except Exception as exc:  # noqa: BLE001
            print("packet err", exc)

    print(json.dumps(asdict(result), ensure_ascii=False, indent=1)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
