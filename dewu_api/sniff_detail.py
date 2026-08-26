# -*- coding: utf-8 -*-
"""监听+刷新已存在的草稿详情页，抓 queryProperties 属性模板响应。"""
import json
import time
from pathlib import Path

from DrissionPage import Chromium, ChromiumOptions

DETAIL_URL = (
    "https://stark.dewu.com/vueProduct/newProductApply/spuEdit/operation/1069223296"
    "?flag=add&fromApply=1&isHaveNewDraft=1&type=add"
)
OUT_DIR = Path("/tmp/prop_packets")
OUT_DIR.mkdir(exist_ok=True)
KEYWORDS = ("propertyId", "propertyName", "definitionId", "领型", "袖长", "面料")


def main() -> None:
    co = ChromiumOptions().set_address("127.0.0.1:9222").existing_only()
    browser = Chromium(co)
    tab = None
    for t in browser.get_tabs():
        if "spuEdit" in str(t.url):
            tab = t
            break
    if not tab:
        tab = browser.latest_tab
        tab.get(DETAIL_URL)
        time.sleep(3)
    print("tab:", str(tab.url)[:100])

    tab.listen.start()
    tab.refresh()
    time.sleep(6)

    hits = 0
    skip_ext = (".js", ".css", ".png", ".jpg", ".gif", ".woff", ".svg", ".ico")
    for packet in tab.listen.steps(timeout=30):
        try:
            url = packet.url or ""
            if url.split("?")[0].endswith(skip_ext):
                continue
            body = packet.response.body
            if isinstance(body, bytes):
                body = body.decode("utf-8", "ignore")
            text = str(body)
            if any(k in text for k in KEYWORDS) or "propert" in url.lower():
                hits += 1
                safe = (url.split("?")[0].rstrip("/").split("/")[-1] or f"api{hits}")[:40]
                (OUT_DIR / f"qp_{hits:02d}_{safe}.json").write_text(
                    json.dumps({"url": url, "body": text}, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
                print("HIT", hits, url[:110], "len=", len(text))
        except Exception as exc:  # noqa: BLE001
            print("err", exc)
    print("TOTAL", hits)


if __name__ == "__main__":
    main()
