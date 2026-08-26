"""得物开放平台 API 客户端（新品来样服务 /dop/api/v2/nps/*）。

凭证管理：
- AppKey/AppSecret 从同目录 .env 读取（.env 已 gitignore，绝不入库）；
- 环境切换：sandbox（默认）/ prod，各读各的凭证。

签名算法（官方「签名规则与事例」doc 1016729196，2026-08-25 实录）：
1. 签名基底 = 全部请求参数（app_key + timestamp + 业务参数）；
   移除 key 为 secret 的项与值为 None 的项（空字符串保留）；
2. 值序列化：bool→"true"/"false"；数字→str；str 原样；
   list→各元素（str 原样、其余序列化为紧凑 JSON）用 "," 拼接；
   dict→紧凑 JSON（key 排序、递归去 null 值字段）；
3. 全部 key 按字典序排序；
4. key 与 value 分别做 Java URLEncoder 风格编码：
   空格→"+"，"*"不编码，"~"→%7E，字母数字与 -_. 不编码，其余 %XX 大写；
5. 拼 "k1=v1&k2=v2&..."，末尾直接追加 APP_SECRET（不编码）；
6. UTF-8 字节 MD5，转大写十六进制。

请求形态：参数整体（app_key/timestamp/sign + 业务参数）作为 JSON body 发送，
body 的 key 同样按排序序列化（与签名基底保持同序，规避服务端按到达顺序
重放签名的歧义）。若网关改从 query 读取，仅需调整 request() 的发送位置。

2026-08-25 无凭证探测实证（沙箱网关）：
- POST /dop/api/v2/nps/create 路径真实存在（缺签名返回业务错误而非 404）；
- 认证失败统一返回 {"code":403,"msg":"签名验证失败，请检查签名方法是否
  正确以及使用的【appKey,secret】是否为沙箱【appKey,secret】"}；
- 出参结构确认为 {code,msg,data}。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests

_HOSTS = {
    "sandbox": "https://openapi-sandbox.dewu.com",
    "prod": "https://openapi.dewu.com",
}
_TIMEOUT = 30


def _load_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析（KEY=VALUE，忽略注释与空行），避免额外依赖。"""
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


def _compact_json(obj: Any) -> str:
    """紧凑 JSON（无空格、非 ASCII 保留原字符），dict key 排序、递归去 null。"""
    def strip_null(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: strip_null(iv) for k, iv in v.items() if iv is not None}
        if isinstance(v, list):
            return [strip_null(iv) for iv in v]
        return v
    return json.dumps(strip_null(obj), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _value_to_string(v: Any) -> str:
    """按官方规则把参数值转成参与签名的字符串。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return ",".join(item if isinstance(item, str) else _compact_json(item) for item in v)
    return _compact_json(v)


def _url_encode_java(text: str) -> str:
    """对齐 java URLEncoder.encode(s, "UTF-8")：空格→+、*保留、~→%7E、%XX 大写。"""
    encoded = quote_plus(text, safe="*")
    return encoded.replace("~", "%7E")


def create_sign(params: dict[str, Any], app_secret: str) -> tuple[str, str]:
    """生成签名。返回 (sign, 待签名串)——待签名串用于联调排查。"""
    base = {k: v for k, v in params.items() if v is not None and k != "secret"}
    pairs = [
        f"{_url_encode_java(str(k))}={_url_encode_java(_value_to_string(v))}"
        for k, v in sorted(base.items())
    ]
    sign_str = "&".join(pairs) + app_secret
    sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()
    return sign, sign_str


class DewuApiError(RuntimeError):
    """接口返回非 200 时的错误，携带 code/msg 与 preCheckResponse 明细。"""

    def __init__(self, code: Any, msg: str, pre_check: str = ""):
        self.code = code
        self.msg = msg
        self.pre_check = pre_check
        super().__init__(f"[{code}] {msg}" + (f"\n预审明细:\n{pre_check}" if pre_check else ""))


class DewuClient:
    # 得物为国内域名，直连（绕过本机 Clash 等代理，否则代理未开时全挂）
    _session = requests.Session()
    _session.trust_env = False

    def __init__(self, env: str = "sandbox"):
        if env not in _HOSTS:
            raise ValueError(f"未知环境 {env!r}，可选: {list(_HOSTS)}")
        self.env = env
        self.host = _HOSTS[env]

        env_values = _load_env_file(Path(__file__).resolve().parent / ".env")
        prefix = f"DEWU_{env.upper()}_"
        self.app_key = env_values.get(prefix + "APP_KEY", "")
        self.app_secret = env_values.get(prefix + "APP_SECRET", "")
        missing = [n for n, v in (("APP_KEY", self.app_key), ("APP_SECRET", self.app_secret)) if not v]
        if missing:
            raise ValueError(
                f"缺少 {prefix}{'/'.join(missing)}，请在 dewu_api/.env 中填写（参考 .env.example）"
            )

        # ISV 身份调生产必须带商家授权 access_token（自研商家可忽略）。
        # tokens.json 由 OAuth code 换取生成；过期时用 refresh_token 自动刷新。
        self.access_token = ""
        self._refresh_token_value = ""
        tokens_path = Path(__file__).resolve().parent / "tokens.json"
        if env == "prod" and tokens_path.is_file():
            tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
            self.access_token = tokens.get("access_token", "")
            self._refresh_token_value = tokens.get("refresh_token", "")

    def _save_tokens(self, data: dict) -> None:
        tokens_path = Path(__file__).resolve().parent / "tokens.json"
        tokens_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def refresh_access_token(self) -> str:
        """用 refresh_token 换新 access_token 并回写 tokens.json。"""
        if not self._refresh_token_value:
            raise DewuApiError(-1, "无 refresh_token，需商家重新走 OAuth 授权")
        resp = self._session.post(
            "https://open.dewu.com/api/v1/h5/passport/v1/oauth2/refresh_token",
            json={
                "client_id": self.app_key,
                "client_secret": self.app_secret,
                "refresh_token": self._refresh_token_value,
            },
            timeout=_TIMEOUT,
        )
        data = (resp.json() or {}).get("data") or {}
        if not data.get("access_token"):
            raise DewuApiError(-1, f"刷新 token 失败: {resp.text[:300]}")
        self.access_token = data["access_token"]
        self._refresh_token_value = data.get("refresh_token", self._refresh_token_value)
        self._save_tokens(data)
        return self.access_token

    # -- 请求构造 ------------------------------------------------------------
    def build_request(self, path: str, biz_params: dict) -> dict:
        """组装请求（dry-run 可直接打印本函数返回值核对）。"""
        params = {"app_key": self.app_key, "timestamp": int(time.time() * 1000)}
        if self.access_token:
            params["access_token"] = self.access_token
        params.update(biz_params)
        sign, sign_str = create_sign(params, self.app_secret)
        body = {**params, "sign": sign}
        return {
            "url": f"{self.host}{path}",
            # body 整体按 key 排序序列化，与签名基底同序
            "body_json": json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            "sign": sign,
            "sign_str": sign_str,
        }

    def request(self, path: str, biz_params: dict) -> dict:
        req = self.build_request(path, biz_params)
        resp = self._session.post(
            req["url"],
            data=req["body_json"].encode("utf-8"),
            headers={"Content-Type": "application/json;charset=utf-8"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 200:
            raise DewuApiError(
                result.get("code"),
                result.get("msg", "unknown"),
                _format_pre_check(result),
            )
        return result

    # -- 图片上传 ------------------------------------------------------------
    def upload_media(self, image_path: str | Path, media_type: int = 1) -> str:
        """逐张上传图片，返回 img_key。严禁整包传 ZIP。

        实测（2026-08-25 沙箱）：该接口走 JSON body 而非 multipart——
        {app_key, timestamp, sign, file_bytes: <base64>, type: N}；
        返回 data 直接是 img_key 字符串。
        type 生产实测：1=轮播图（严格校验 25:16 或 1:1、750×480+）；
        详情图等非方图需其它 type 值放行（见 mappings.json upload_media_type_notes）。
        """
        image_path = Path(image_path)
        import base64
        biz = {
            "file_bytes": base64.b64encode(image_path.read_bytes()).decode("ascii"),
            "type": media_type,
        }
        result = self.request("/dop/api/v1/nps/upload_media", biz)
        img_key = result.get("data")
        if not isinstance(img_key, str) or not img_key:
            raise DewuApiError(-1, f"upload_media 返回中未找到 img_key: {result}")
        return img_key


def _format_pre_check(result: dict) -> str:
    """把 data.preCheckResponse.resultList 展开成人能读的字段级提示。"""
    pre = (result.get("data") or {}).get("preCheckResponse") or {}
    rows = pre.get("resultList") or []
    if not rows:
        return ""
    lines = []
    for item in rows:
        name = item.get("bizRuleName", "")
        details = item.get("detailList") or []
        for d in details:
            lines.append(
                f"- [{name}] {d.get('moduleName', '')}: {d.get('value', '')} {d.get('suggest', '')}".rstrip()
            )
    return "\n".join(lines)
