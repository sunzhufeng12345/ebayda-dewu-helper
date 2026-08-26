"""得物 API 常驻 Worker：认领 BPMS 批次任务 → 调得物开放平台建草稿。

======================================================================
一、定位
======================================================================
    部署在生产服务器（/www/wwwroot/dewu-worker/）的 systemd 常驻进程，
    取代本地 Windows 助手（Chrome 自动化）：

        1. 轮询 POST /api/automation/devices/next 认领批次执行项；
        2. 下载商品 JSON / 图片 ZIP / 尺码表 xlsx（JobToken 鉴权）；
        3. 从 BPMS 取店铺得物 access_token（WorkerToken 鉴权，BPMS 负责刷新）；
        4. 复用 dewu_dp 现有链路（models / config_loader / payload_builder）
           组装报文，调用 /dop/api/v2/nps/create（create_type=2 仅草稿）；
        5. 上报终态 draft_saved / failed（DeviceToken 鉴权）。

======================================================================
二、与 BPMS 的接口契约
======================================================================
    POST /api/automation/devices/next
        Authorization: DeviceToken <token>
        ← {"commands":[{"item_id","type"}], "item":{job_id,shop_id,action,
           job_token,product_json_url,images_zip_url,size_chart_url}}
    POST /api/automation/batch-items/:item_id/events        （JobToken，中间态）
    POST /api/automation/devices/batch-items/:item_id/final-event（DeviceToken，终态）
    POST /api/automation/devices/termination-ack             （DeviceToken，终止确认）
    GET  /api/dewu/tokens/:shop_id
        Authorization: WorkerToken <token>
        ← {"code":200,"data":{"shop_id","access_token","access_expires_at"}}

======================================================================
三、配置（环境变量，可用同目录 worker.env 兜底）
======================================================================
    BPMS_BASE_URL      必填，如 https://www.ebayda.com
    BPMS_DEVICE_TOKEN  必填，店铺绑定后由 bind_shops.py 写入 worker.env
    BPMS_WORKER_TOKEN  必填，与 BPMS 后端 DEWU_WORKER_TOKEN 一致
    DEWU_BRAND_ID      可选，品牌 id（默认 1048353，2026-08 实查）
    DEWU_ARTICLE_SUFFIX 可选，货号追加后缀（测试去重用）
    WORKER_POLL_INTERVAL 可选，空闲轮询秒数（默认 10）
    WORKER_STATE_DIR   可选，下载/解压/报文缓存目录（默认 worker_state/）
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR / "dewu_api"))
sys.path.insert(0, str(_THIS_DIR))

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

# 与后端 maxEventMessageRunes=500 对齐，留出前缀余量。
MAX_MESSAGE_RUNES = 400
DOWNLOAD_TIMEOUT = 120


class WorkerConfigError(RuntimeError):
    """配置缺失或非法。"""


class BpmsApiError(RuntimeError):
    """BPMS 接口非 2xx。"""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


class BpmsConflict(BpmsApiError):
    """409：任务状态已变化（典型：处理中被强制终止）。"""


def _log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def _truncate(message: str, limit: int = MAX_MESSAGE_RUNES) -> str:
    message = " ".join(str(message).split())
    return message if len(message) <= limit else message[: limit - 1] + "…"


@dataclass(frozen=True)
class WorkerContext:
    bpms: "BpmsApi"
    brand_id: int
    article_suffix: str
    poll_interval: int
    state_dir: Path


class BpmsApi:
    """BPMS worker 协议客户端（DeviceToken / JobToken / WorkerToken 三套凭据）。"""

    def __init__(self, base_url: str, device_token: str, worker_token: str):
        self.base = base_url.rstrip("/")
        self.device_token = device_token
        self.worker_token = worker_token
        # 得物/BPMS 均为国内域名，禁用系统代理，避免代理未开时全挂。
        self.session = requests.Session()
        self.session.trust_env = False

    def _request(
        self,
        method: str,
        path: str,
        *,
        credential: tuple[str, str] | None = None,
        json_body: dict | None = None,
        timeout: int = 30,
    ) -> requests.Response:
        headers = {}
        if credential:
            headers["Authorization"] = f"{credential[0]} {credential[1]}"
        resp = self.session.request(
            method,
            f"{self.base}{path}",
            headers=headers,
            json=json_body,
            timeout=timeout,
        )
        if resp.status_code == 409:
            raise BpmsConflict(409, _response_message(resp))
        if resp.status_code >= 400:
            raise BpmsApiError(resp.status_code, _response_message(resp))
        return resp

    # -- 设备侧 --------------------------------------------------------------
    def next(self) -> dict[str, Any]:
        resp = self._request(
            "POST", "/api/automation/devices/next", credential=("DeviceToken", self.device_token)
        )
        return resp.json()

    def report_final(self, item_id: str, status: str, message: str) -> None:
        self._request(
            "POST",
            f"/api/automation/devices/batch-items/{item_id}/final-event",
            credential=("DeviceToken", self.device_token),
            json_body={"status": status, "message": _truncate(message)},
        )

    def ack_termination(self, item_id: str) -> None:
        self._request(
            "POST",
            "/api/automation/devices/termination-ack",
            credential=("DeviceToken", self.device_token),
            json_body={"item_id": item_id},
        )

    # -- 执行项侧（JobToken）-------------------------------------------------
    def report_event(self, item_id: str, job_token: str, status: str, message: str) -> None:
        self._request(
            "POST",
            f"/api/automation/batch-items/{item_id}/events",
            credential=("JobToken", job_token),
            json_body={"status": status, "message": _truncate(message)},
        )

    def download(self, url: str, job_token: str, destination: Path) -> Path:
        headers = {"Authorization": f"JobToken {job_token}"}
        with self.session.get(url, headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT) as resp:
            if resp.status_code >= 400:
                raise BpmsApiError(resp.status_code, _response_message(resp))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as file:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        file.write(chunk)
        return destination

    # -- 得物 token（WorkerToken）---------------------------------------------
    def dewu_access_token(self, shop_id: str) -> str:
        resp = self._request(
            "GET",
            f"/api/dewu/tokens/{shop_id}",
            credential=("WorkerToken", self.worker_token),
        )
        payload = resp.json()
        data = payload.get("data") or {}
        token = str(data.get("access_token") or "")
        if not token:
            raise BpmsApiError(resp.status_code, f"店铺 {shop_id} 未返回 access_token：{payload}")
        return token


def _response_message(resp: requests.Response) -> str:
    try:
        payload = resp.json()
        return str(payload.get("message") or payload.get("msg") or resp.text[:200])
    except Exception:  # noqa: BLE001
        return resp.text[:200]


def parse_size_chart_xlsx(path: Path) -> dict[str, dict[str, str]]:
    """把 BPMS 生成的得物官方尺码表 xlsx 解析为 {尺码: {测量名: 值}}。

    官方模板 A-J 列：尺码 | EU欧码 | 分隔列 | 肩宽(cm) | 适合臂长(cm) |
    适合肩宽(cm) | 衣长(cm) | 胸围(cm) | 袖长(cm) | 温馨提示。
    空表头列（分隔列）与空值单元格跳过。
    """
    import openpyxl

    workbook = openpyxl.load_workbook(path, data_only=True)
    try:
        sheet = workbook.active
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    if not rows:
        return {}
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    chart: dict[str, dict[str, str]] = {}
    for row in rows[1:]:
        if not row or row[0] is None:
            continue
        size = str(row[0]).strip()
        if not size:
            continue
        measures: dict[str, str] = {}
        for index, name in enumerate(header):
            if index == 0 or not name or index >= len(row):
                continue
            value = row[index]
            if value is None:
                continue
            text = str(value).strip()
            if text:
                measures[name] = text
        if measures:
            chart[size] = measures
    return chart


def _build_client(ctx: WorkerContext, shop_id: str) -> DewuClient:
    """构造得物客户端：凭证来自 dewu_api/.env，token 来自 BPMS（服务端无 tokens.json）。"""
    client = DewuClient(env="prod")
    client.access_token = ctx.bpms.dewu_access_token(shop_id)
    client._refresh_token_value = ""  # noqa: SLF001 - token 刷新统一收敛在 BPMS 后端
    return client


def _resolve_guidance_files(product) -> tuple[Any, Any]:
    """尺码推荐/试穿报告：按商品首尾尺码在 配置文件/ 下匹配 Excel。"""
    size_recommend = None
    try_on = None
    try:
        import main as legacy

        size_recommend_path = legacy._resolve_size_guidance_file(  # noqa: SLF001
            product.sizes, _THIS_DIR / "配置文件", "尺码推荐"
        )
        if size_recommend_path:
            size_recommend = build_size_recommend(size_recommend_path)
            _log(f"尺码推荐 <- {Path(size_recommend_path).name}")
        try_on_path = legacy._resolve_size_guidance_file(  # noqa: SLF001
            product.sizes, _THIS_DIR / "配置文件", "试穿报告"
        )
        if try_on_path:
            try_on = build_try_on_report(try_on_path)
            _log(f"试穿报告 <- {Path(try_on_path).name}")
    except Exception as exc:  # noqa: BLE001 - 辅助文件缺失不阻断主流程
        _log(f"⚠ 尺码推荐/试穿报告解析失败，跳过：{exc}")
    return size_recommend, try_on


def _upload_carousel(client: DewuClient, media_files) -> dict[str, list[str]]:
    """按颜色逐张上传得物平铺图（严禁整包传 ZIP）。"""
    img_keys_by_color: dict[str, list[str]] = {}
    for color, paths in media_files.carousel_by_color.items():
        keys: list[str] = []
        for path in paths:
            key = client.upload_media(path)
            keys.append(key)
            _log(f"  上传 {color}/{Path(path).name} -> {key}")
        if keys:
            img_keys_by_color[color] = keys
    return img_keys_by_color


def process_item(ctx: WorkerContext, item: dict[str, Any]) -> bool:
    """处理单个批次执行项；返回是否成功建草稿。"""
    item_id = str(item["job_id"])
    shop_id = str(item["shop_id"])
    job_token = str(item["job_token"])
    item_dir = ctx.state_dir / item_id
    item_dir.mkdir(parents=True, exist_ok=True)

    def event(status: str, message: str) -> None:
        try:
            ctx.bpms.report_event(item_id, job_token, status, message)
        except BpmsConflict:
            _log(f"[{item_id}] 状态已变化（可能被取消/终止），事件 {status} 跳过")

    def final(status: str, message: str) -> None:
        try:
            ctx.bpms.report_final(item_id, status, message)
        except BpmsConflict:
            # 处理期间被网站强制终止：先确认终止，再按“结果未知”收尾。
            try:
                ctx.bpms.ack_termination(item_id)
            except BpmsApiError as exc:
                _log(f"[{item_id}] 终止确认失败：{exc}")
            try:
                ctx.bpms.report_final(item_id, "terminated_unknown", message)
            except BpmsApiError as exc:
                _log(f"[{item_id}] 终态上报失败：{exc}")

    _log(f"[{item_id}] 开始处理：shop_id={shop_id} action={item.get('action')}")
    try:
        event("preparing", "下载商品数据")
        product_json_path = ctx.bpms.download(item["product_json_url"], job_token, item_dir / "product.json")
        images_zip_path = ctx.bpms.download(item["images_zip_url"], job_token, item_dir / "images.zip")
        size_chart_path = ctx.bpms.download(item["size_chart_url"], job_token, item_dir / "size_chart.xlsx")
        _log(f"[{item_id}] 数据下载完成")

        event("running", "调用得物 API 建草稿")

        import models

        product = models.load_product(product_json_path)
        media_files = models.extract_and_resolve_media(product, images_zip_path, ctx.state_dir / "work")
        _log(
            f"[{item_id}] 商品 {product.source_name} | 品牌 {product.brand} | "
            f"颜色 {product.colors} × 尺码 {len(product.sizes)} 码"
        )

        import config_loader

        cfg = config_loader.load_config(_THIS_DIR / "配置文件")
        size_chart = parse_size_chart_xlsx(size_chart_path)
        if size_chart:
            cfg.size_chart = size_chart
        else:
            _log("⚠ 尺码表 xlsx 为空，沿用 配置.xlsx 默认尺码表")

        client = _build_client(ctx, shop_id)

        mappings = load_mappings()
        mappings["brands"][product.brand] = {"brand_id": ctx.brand_id}
        category_path = config_loader.resolve_category(cfg, product)
        category_key = ">>".join(category_path)
        flat = sync_category_mappings(client, ctx.brand_id)
        if category_key not in flat:
            nearby = [p for p in flat if p.split(">>")[-1] == category_key.split(">>")[-1]][:5]
            raise RuntimeError(f"类目「{category_key}」不在品牌类目树中，相近路径：{nearby}")
        mappings["categories"][category_key] = {"category_id": flat[category_key]}
        fetch_props_template_api(client, flat[category_key], category_key=category_key)
        _log(f"[{item_id}] 类目 {category_key} -> {flat[category_key]}")

        img_keys_by_color = _upload_carousel(client, media_files)
        detail_section_keys = upload_detail_images(client, media_files)
        _log(
            f"[{item_id}] 图片上传完成：轮播 {sum(len(v) for v in img_keys_by_color.values())} 张，"
            f"详情区块 {dict((k, len(v)) for k, v in detail_section_keys.items())}"
        )

        first_color = product.colors[0] if product.colors else ""
        detail_images = build_detail_images(detail_section_keys, first_color) if detail_section_keys else None
        size_recommend, try_on = _resolve_guidance_files(product)

        payload = build_create_payload(
            product,
            cfg,
            mappings,
            img_keys_by_color,
            allow_missing=True,
            detail_images=detail_images,
            size_recommend=size_recommend,
            try_on_report=try_on,
        )
        for warning in payload["_warnings"]:
            _log(f"[{item_id}] ⚠ {warning}")
        api_body = {k: v for k, v in payload.items() if not k.startswith("_")}
        if ctx.article_suffix:
            api_body["article_number"] = str(api_body["article_number"]) + ctx.article_suffix
        (item_dir / "payload.json").write_text(
            json.dumps(api_body, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        result = client.request("/dop/api/v2/nps/create", api_body)
        (item_dir / "resp.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        data = result.get("data") or {}
        draft_id = data.get("draft_id")
        if not isinstance(draft_id, int):
            raise RuntimeError(f"建草稿未返回 draft_id：{json.dumps(result, ensure_ascii=False)[:300]}")
        message = f"草稿已创建 draft_id={draft_id}"
        if payload["_warnings"]:
            message += f"；警告：{'；'.join(payload['_warnings'][:3])}"
        final("draft_saved", message)
        _log(f"[{item_id}] ✅ draft_id={draft_id}")
        return True
    except DewuApiError as exc:
        final("failed", f"得物 API 失败：{exc}")
        _log(f"[{item_id}] ❌ 得物 API 失败：{exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - 任何异常都收敛为任务失败终态
        final("failed", f"{type(exc).__name__}: {exc}")
        _log(f"[{item_id}] ❌ {type(exc).__name__}: {exc}")
        return False


def _handle_commands(ctx: WorkerContext, commands: list[dict[str, Any]]) -> set[str]:
    """确认终止指令；返回仍处于终止待确认的 item_id 集合。"""
    terminated: set[str] = set()
    for command in commands:
        item_id = str(command.get("item_id") or "")
        if not item_id:
            continue
        try:
            ctx.bpms.ack_termination(item_id)
            _log(f"[{item_id}] 已确认强制终止")
            terminated.add(item_id)
        except BpmsConflict:
            # 已不在 termination_pending（先被终态上报处理掉），无需确认。
            _log(f"[{item_id}] 终止指令已失效（任务已终态）")
        except BpmsApiError as exc:
            _log(f"[{item_id}] 终止确认失败：{exc}")
    return terminated


def load_worker_config() -> WorkerContext:
    env = _load_env_file(_THIS_DIR / "worker.env")

    def get(name: str, default: str = "") -> str:
        return os.environ.get(name) or env.get(name) or default

    base_url = get("BPMS_BASE_URL").rstrip("/")
    device_token = get("BPMS_DEVICE_TOKEN")
    worker_token = get("BPMS_WORKER_TOKEN")
    missing = [
        name
        for name, value in (
            ("BPMS_BASE_URL", base_url),
            ("BPMS_DEVICE_TOKEN", device_token),
            ("BPMS_WORKER_TOKEN", worker_token),
        )
        if not value
    ]
    if missing:
        raise WorkerConfigError(f"缺少配置 {'、'.join(missing)}（环境变量或 worker.env）")
    try:
        brand_id = int(get("DEWU_BRAND_ID", "1048353"))
    except ValueError as exc:
        raise WorkerConfigError(f"DEWU_BRAND_ID 非法：{exc}") from exc
    try:
        poll_interval = int(get("WORKER_POLL_INTERVAL", "10"))
    except ValueError as exc:
        raise WorkerConfigError(f"WORKER_POLL_INTERVAL 非法：{exc}") from exc
    state_dir = Path(get("WORKER_STATE_DIR", str(_THIS_DIR / "worker_state"))).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    return WorkerContext(
        bpms=BpmsApi(base_url, device_token, worker_token),
        brand_id=brand_id,
        article_suffix=get("DEWU_ARTICLE_SUFFIX", ""),
        poll_interval=poll_interval,
        state_dir=state_dir,
    )


def main() -> int:
    try:
        ctx = load_worker_config()
    except WorkerConfigError as exc:
        _log(f"配置错误：{exc}")
        return 2
    _log(
        f"Worker 启动：BPMS={ctx.bpms.base} 品牌={ctx.brand_id} "
        f"轮询={ctx.poll_interval}s 状态目录={ctx.state_dir}"
    )
    while True:
        try:
            result = ctx.bpms.next()
            commands = result.get("commands") or []
            if commands:
                _handle_commands(ctx, commands)
            item = result.get("item")
            if item:
                process_item(ctx, item)
                continue
        except BpmsApiError as exc:
            if exc.status == 401:
                _log(f"设备凭据无效（DeviceToken/WorkerToken 配置错误），退出：{exc}")
                return 1
            _log(f"BPMS 接口异常：{exc}")
        except Exception as exc:  # noqa: BLE001 - 主循环兜底，防进程退出
            _log(f"主循环异常：{type(exc).__name__}: {exc}")
        time.sleep(ctx.poll_interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
