# -*- coding: utf-8 -*-
"""连 9222 Chrome 自动走新品申请起始页（main.py 同款选择器），
创建成功后在详情页抓 queryProperties 属性模板接口完整响应，
并自动转换入库 mappings_props_template.json（按类目路径分键）。

用法：
  python sniff_prop_template.py                 # 默认 服装 上衣 卫衣
  python sniff_prop_template.py 服装 上衣 T恤   # 抓任意类目
前提：9222 Chrome 已登录 stark.dewu.com（商家后台账号）。
"""
import ast
import json
import sys
import time
from pathlib import Path

from DrissionPage import Chromium, ChromiumOptions

_THIS_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = _THIS_DIR / "mappings_props_template.json"

START_URL = "https://stark.dewu.com/vueProduct/newProductApply/start?noLayout=1"
FIRST_SQUARE = "/tmp/p2430_imgs/W62261/第一张方图/未标题-1_0000_PB26XY01LJB-CM22-5 TH196 拷贝.jpg"
OUT_DIR = Path("/tmp/prop_packets")
OUT_DIR.mkdir(exist_ok=True)
CATEGORY = tuple(sys.argv[1:]) or ("服装", "上衣", "卫衣")
KEYWORDS = ("propertyId", "propertyName", "definitionId", "领型", "袖长", "面料")


def visible(tab, xpath, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        for el in tab.eles(f"xpath:{xpath}"):
            try:
                if el.states.is_displayed:
                    return el
            except Exception:  # noqa: BLE001
                continue
        time.sleep(0.3)
    return None


def form_item(tab, label):
    return visible(
        tab,
        '//form//*[contains(concat(" ",normalize-space(@class)," ")," el-form-item ")]'
        f'[.//label[contains(normalize-space(.),"{label}")]]',
        timeout=8,
    )


def item_input(tab, label):
    fi = form_item(tab, label)
    return fi.ele("tag:input") if fi else None


def wait_value(tab, label, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        inp = item_input(tab, label)
        if inp and str(inp.attr("value") or "").strip():
            return str(inp.attr("value"))
        time.sleep(0.5)
    return ""


def dump(idx, url, text):
    safe = (url.split("?")[0].rstrip("/").split("/")[-1] or f"api{idx}")[:40]
    (OUT_DIR / f"qp_{idx:02d}_{safe}.json").write_text(
        json.dumps({"url": url, "body": text}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def convert_and_save(query_props_body: str) -> int:
    """queryProperties 响应（Python repr 或 JSON 字符串）→ 模板条目，按类目入库。"""
    try:
        body = json.loads(query_props_body)
    except ValueError:
        body = ast.literal_eval(query_props_body)
    props = body.get("data") or []
    items = [
        {
            "name": p["name"],
            "property_id": p["id"],
            "definition_id": p.get("definitionId") or 0,
            "required": bool(p.get("isRequired")),
            "value_type": p.get("valueType", 0),
            "property_type": p.get("propertyType", 0),
            "source_id": p.get("sourceId"),
            "value_options": [v for v in (p.get("valueOptions") or "").split(",") if v],
        }
        for p in props
    ]
    data = {}
    if TEMPLATE_PATH.is_file():
        raw = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, list):  # 旧格式
            data = {"服装>>上衣>>卫衣": raw}
    key = ">>".join(CATEGORY)
    data[key] = items
    TEMPLATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已入库 {key}: {len(items)} 个属性 -> {TEMPLATE_PATH.name}")
    return len(items)


def main() -> None:
    co = ChromiumOptions().set_address("127.0.0.1:9222").existing_only()
    browser = Chromium(co)
    tab = browser.latest_tab
    tab.get(START_URL)
    time.sleep(4)
    if "login" in str(tab.url) or tab.ele("text:密码登录", timeout=2):
        print("NOT_LOGGED_IN", tab.url)
        return

    # 品牌
    inp = item_input(tab, "商品品牌")
    if not inp:
        print("NO_BRAND_INPUT")
        return
    inp.click()
    time.sleep(1)
    opt = visible(tab, '//body//li[contains(@class,"el-select-dropdown__item")]')
    if not opt:
        print("NO_BRAND_OPTION")
        return
    opt.click(by_js=True)
    time.sleep(1)
    print("brand:", wait_value(tab, "商品品牌"))

    # 类目
    cat = item_input(tab, "商品类目")
    if not cat:
        print("NO_CATEGORY_INPUT")
        return
    cat.click()
    time.sleep(1)
    for value in CATEGORY:
        node = visible(
            tab,
            f'//body//li[contains(@class,"el-cascader-node")][normalize-space(.)="{value}"]',
        )
        if not node:
            print("NO_CASCADER", value)
            return
        node.click(by_js=True)
        time.sleep(1)
    print("category:", wait_value(tab, "商品类目"))

    # 适用人群
    aud = item_input(tab, "适用人群")
    if aud:
        aud.click()
        time.sleep(1)
        opt = visible(
            tab,
            '//body//li[contains(@class,"el-select-dropdown__item")]'
            '[normalize-space(.)="通用"]',
        )
        if opt:
            opt.click(by_js=True)
        time.sleep(1)
        print("audience:", wait_value(tab, "适用人群"))

    # 上传第一张方图
    fi = form_item(tab, "商品图片")
    file_input = fi.ele('xpath:.//input[@type="file"]') if fi else None
    if not file_input:
        print("NO_FILE_INPUT")
        return
    file_input.input(FIRST_SQUARE)
    uploaded = False
    for _ in range(30):
        items = fi.eles('xpath:.//ul[contains(@class,"el-upload-list")]//li')
        if items and "is-success" in str(items[0].attr("class") or ""):
            uploaded = True
            break
        time.sleep(1)
    print("uploaded:", uploaded)
    if not uploaded:
        return

    # 创建
    btn = visible(tab, '//button[normalize-space(.)="创建新品发布申请"]')
    if not btn:
        print("NO_CREATE_BTN")
        return
    start_urls = {str(t.url) for t in browser.get_tabs()}
    tab.listen.start()
    btn.click()

    detail = None
    for _ in range(20):
        for t in browser.get_tabs():
            url = str(t.url)
            if "spuEdit" in url and url not in start_urls:
                detail = t
                break
        if detail:
            break
        time.sleep(1.5)
    print("detail:", detail.url if detail else "NOT_FOUND")
    if not detail:
        tab.get_screenshot(path=str(OUT_DIR), name="fail.png")
        return
    detail.set.activate()
    time.sleep(3)

    # 详情页刷新重放属性模板请求
    detail.listen.start()
    detail.refresh()
    time.sleep(5)

    hits = 0
    qp_body = ""
    skip_ext = (".js", ".css", ".png", ".jpg", ".gif", ".woff", ".svg", ".ico")
    for packet in detail.listen.steps(timeout=40):
        try:
            url = packet.url or ""
            if url.split("?")[0].endswith(skip_ext):
                continue
            body = packet.response.body
            if isinstance(body, bytes):
                body = body.decode("utf-8", "ignore")
            text = str(body)
            if "queryProperties" in url and len(text) > len(qp_body):
                qp_body = text
            if any(k in text for k in KEYWORDS) or "propert" in url.lower():
                hits += 1
                dump(hits, url, text)
                print("HIT", hits, url[:120], "len=", len(text))
        except Exception as exc:  # noqa: BLE001
            print("packet err", exc)
    print("TOTAL_HITS", hits)
    if qp_body:
        convert_and_save(qp_body)
    else:
        print("未捕获 queryProperties 响应，不入库")


if __name__ == "__main__":
    main()
