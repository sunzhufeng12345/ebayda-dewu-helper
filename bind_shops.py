"""一键把 BPMS 得物店铺绑定到常驻 Worker 设备。

======================================================================
一、用途
======================================================================
    批量上架要求“店铺已绑定常驻助手且绑定同一设备”（ErrBatchShopNotBound /
    ErrBatchDeviceMismatch），本脚本按以下流程完成绑定：

        登录 BPMS（管理员账号）
        → 拉取全部“得物”平台启用店铺
        → 逐店创建绑定票据（POST /automation/shop-bindings/tickets）
        → 用票据认领并携带 device_token（POST /automation/shop-bindings/claim）
        → 把 device_token 持久化到 worker.env（dewu_api_worker.py 直接读取）

    注意：绑定关系挂在“登录用户 + 店铺”上。请用创建批次的管理员账号运行，
    之后该账号发起的批次才会路由到本设备。

======================================================================
二、配置（环境变量，可用同目录 worker.env 兜底）
======================================================================
    BPMS_BASE_URL   必填，如 https://www.ebayda.com
    BPMS_USERNAME   必填
    BPMS_PASSWORD   必填
    WORKER_DEVICE_ID 可选（默认 dewu-worker）
    WORKER_DEVICE_TOKEN 可选；缺失时自动生成并写回 worker.env

用法：
    python3 bind_shops.py [--include-closed]
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import requests

_THIS_DIR = Path(__file__).resolve().parent
WORKER_ENV_PATH = _THIS_DIR / "worker.env"


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


def _save_env_file(path: Path, updates: dict[str, str]) -> None:
    values = _load_env_file(path)
    values.update(updates)
    lines = [
        "# dewu worker 配置（bind_shops.py 自动维护 DEVICE_TOKEN，其余可手工调整）",
        *[f"{key}={value}" for key, value in values.items()],
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _get(name: str, env: dict[str, str], default: str = "") -> str:
    return os.environ.get(name) or env.get(name) or default


class BpmsClient:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        self.token = ""

    def login(self, username: str, password: str) -> None:
        resp = self._request("POST", "/api/auth/login", json_body={"username": username, "password": password})
        data = resp.get("data") or {}
        token = str(data.get("token") or "")
        if not token:
            raise RuntimeError(f"登录未返回 token：{resp}")
        self.token = token

    def _request(
        self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None
    ) -> Any:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        resp = self.session.request(
            method, f"{self.base}{path}", headers=headers, json=json_body, params=params, timeout=30
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path}: {resp.text[:300]}")
        payload = resp.json()
        code = payload.get("code")
        if code not in (None, 0, 200):
            raise RuntimeError(f"业务失败 {path}: code={code} message={payload.get('message')}")
        return payload

    def list_dewu_shops(self, include_closed: bool) -> list[dict[str, Any]]:
        shops: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self._request(
                "GET",
                "/api/shops",
                params={"page": page, "pageSize": 100, "platform": "得物"},
            )
            data = payload.get("data") or {}
            rows = data.get("list") or []
            if not rows:
                break
            for shop in rows:
                if include_closed or str(shop.get("status") or "active") == "active":
                    shops.append(shop)
            if len(shops) >= int(data.get("total") or 0):
                break
            page += 1
        return shops

    def create_binding_ticket(self, shop_id: int, device_id: str) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/api/automation/shop-bindings/tickets",
            json_body={"shop_id": shop_id, "device_id": device_id, "action": "bind"},
        )
        return payload.get("data") or {}

    def claim_binding(self, ticket: str, device_token: str) -> dict[str, Any]:
        resp = self.session.post(
            f"{self.base}/api/automation/shop-bindings/claim",
            headers={"Authorization": f"BindingTicket {ticket}"},
            json={"device_token": device_token},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} claim: {resp.text[:300]}")
        return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-closed", action="store_true", help="同时绑定已停用店铺")
    args = parser.parse_args()

    env = _load_env_file(WORKER_ENV_PATH)
    base_url = _get("BPMS_BASE_URL", env)
    username = _get("BPMS_USERNAME", env)
    password = _get("BPMS_PASSWORD", env)
    device_id = _get("WORKER_DEVICE_ID", env, "dewu-worker")
    device_token = _get("BPMS_DEVICE_TOKEN", env)
    missing = [
        name
        for name, value in (
            ("BPMS_BASE_URL", base_url),
            ("BPMS_USERNAME", username),
            ("BPMS_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        print(f"缺少配置 {'、'.join(missing)}（环境变量或 worker.env）")
        return 2

    token_generated = False
    if not device_token:
        device_token = secrets.token_hex(32)
        token_generated = True

    client = BpmsClient(base_url)
    print(f"登录 BPMS：{username}@{base_url}")
    client.login(username, password)

    shops = client.list_dewu_shops(args.include_closed)
    if not shops:
        print("没有可绑定的得物店铺（platform=得物）")
        return 0
    print(f"发现 {len(shops)} 家得物店铺，开始绑定到设备 {device_id}\n")

    succeeded: list[dict[str, Any]] = []
    failed: list[tuple[dict[str, Any], str]] = []
    for shop in shops:
        shop_id = int(shop.get("id") or 0)
        shop_name = str(shop.get("name") or shop_id)
        try:
            ticket = client.create_binding_ticket(shop_id, device_id)
            claim = client.claim_binding(str(ticket.get("ticket") or ""), device_token)
            # 主动携带 device_token 时后端只存 hash 不回显（仅 legacy 模式回显），
            # 成功判定以 status/device_id 为准。
            if str(claim.get("status") or "") != "bound" or str(claim.get("device_id") or "") != device_id:
                raise RuntimeError(f"认领结果异常：{json.dumps(claim, ensure_ascii=False)[:200]}")
            succeeded.append(shop)
            print(f"  ✅ #{shop_id} {shop_name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((shop, str(exc)))
            print(f"  ❌ #{shop_id} {shop_name}：{exc}")

    if token_generated and succeeded:
        _save_env_file(
            WORKER_ENV_PATH,
            {"BPMS_BASE_URL": base_url, "WORKER_DEVICE_ID": device_id, "BPMS_DEVICE_TOKEN": device_token},
        )
        print(f"\ndevice_token 已生成并写入 {WORKER_ENV_PATH}（dewu_api_worker.py 会自动读取）")

    print(f"\n绑定完成：成功 {len(succeeded)} 家，失败 {len(failed)} 家")
    if failed:
        print("失败清单：")
        for shop, reason in failed:
            print(f"  - #{shop.get('id')} {shop.get('name')}：{reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
