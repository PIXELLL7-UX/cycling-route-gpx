"""导出 iGPSPORT 路书，或用高德 / 百度官方自行车接口规划并导出 GPX。"""

import argparse
from dataclasses import dataclass
import getpass
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import parse_qs, urlencode, urlsplit, urljoin
import xml.etree.ElementTree as ET

from coordinates import gcj02_to_wgs84, wgs84_to_gcj02
from credentials import CredentialError, read_other_platform, save_key


GPX = "http://www.topografix.com/GPX/1/1"
GPX_10 = "http://www.topografix.com/GPX/1/0"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
AMAP = "https://restapi.amap.com"
AMAP_KEY_GUIDE = "https://lbs.amap.com/api/webservice/create-project-and-key"
BAIDU = "https://api.map.baidu.com"
BAIDU_KEY_GUIDE = "https://lbsyun.baidu.com/apiconsole/key"
IGPSPORT = {"cn": "prod.zh.igpsport.com", "intl": "prod.en.igpsport.com"}
RATE_ERRORS = {"10014", "10015", "10019", "10020", "10021", "10022", "10023"}
RETRY_HTTP = {408, 429, 500, 502, 503, 504}
RETRY_CURL = {5, 6, 7, 18, 28, 35, 52, 55, 56, 92}
REQUEST_TIMEOUT = 25
REQUEST_RETRIES = 2
COORDINATE = r"[-+]?\d+(?:\.\d+)?\s*[,，]\s*[-+]?\d+(?:\.\d+)?"


class RouteError(Exception):
    pass


@dataclass(frozen=True)
class Stop:
    name: str
    lon: float
    lat: float
    coordinate_system: str = "gcj02"

    @property
    def point(self):
        return self.lon, self.lat


def lon_lat(value):
    lon, lat = map(float, value.replace("，", ",").split(","))
    if not (-180 <= lon < 180 and -90 <= lat <= 90):
        raise RouteError("坐标超出范围。请使用经度、纬度顺序。")
    return lon, lat


def distance_m(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2
    h += math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742018 * math.asin(math.sqrt(min(1, h)))


def fetch(url, *, follow=False):
    # Pass URLs through stdin so API keys do not appear as process arguments.
    if any(char in url for char in ('"', "\\", "\r", "\n")):
        raise RouteError("链接包含无法处理的字符。")
    if not shutil.which("curl"):
        raise RouteError("运行环境缺少 curl，请安装后重试。")
    with tempfile.TemporaryDirectory() as folder:
        body = Path(folder) / "response"
        command = [
            "curl", "--disable", "--silent", "--show-error",
            "--connect-timeout", str(min(10, REQUEST_TIMEOUT)), "--max-time", str(REQUEST_TIMEOUT),
            "--proto", "=https", "--proto-redir", "=https", "--max-filesize", "30000000",
            "--output", str(body), "--write-out", "%{http_code}\n%{redirect_url}\n",
        ]
        if follow:
            command.extend(["--location", "--max-redirs", "5"])
        for attempt in range(REQUEST_RETRIES + 1):
            body.unlink(missing_ok=True)
            try:
                result = subprocess.run(
                    [*command, "--config", "-"], input=f'url = "{url}"\n',
                    capture_output=True, text=True, check=False, timeout=REQUEST_TIMEOUT + 5,
                )
                error_code = result.returncode
            except subprocess.TimeoutExpired:
                error_code = 28
            if error_code:
                if error_code in RETRY_CURL and attempt < REQUEST_RETRIES:
                    retry_notice(attempt, "网络连接暂时失败")
                    continue
                reasons = {
                    5: "无法解析代理地址，请检查代理设置。",
                    6: "无法解析服务地址，请检查网络、DNS 和 Agent 的联网权限。",
                    7: "无法连接服务，请检查网络或代理后重试。",
                    28: "连接或下载超时，请检查网络；也可增加 --timeout 秒数。",
                    35: "HTTPS 连接握手失败，请检查网络或代理。",
                    60: "HTTPS 证书校验失败，请检查系统时间和证书配置。",
                    63: "返回文件超过 30 MB，已停止下载。",
                }
                raise RouteError(f"{reasons.get(error_code, '下载连接中断，请稍后重试。')}（curl {error_code}，已尝试 {attempt + 1} 次）")
            fields = result.stdout.split("\n")
            if len(fields) < 2 or not fields[0].isdigit():
                raise RouteError("下载工具未返回有效的 HTTP 状态，请检查 curl 版本。")
            code, redirect = int(fields[0]), fields[1]
            if code in RETRY_HTTP and attempt < REQUEST_RETRIES:
                retry_notice(attempt, f"服务暂时不可用（HTTP {code}）")
                continue
            return code, redirect, body.read_bytes() if body.exists() else b""


def retry_notice(attempt, reason):
    print(f"{reason}，正在重试 {attempt + 1}/{REQUEST_RETRIES}…", file=sys.stderr)
    time.sleep(min(2 ** attempt, 4))


def response_json(body, provider):
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        raise RouteError(f"{provider}返回内容无法解析，请稍后重试。") from None
    if not isinstance(result, dict):
        raise RouteError(f"{provider}返回格式异常，请稍后重试。")
    return result


def http_error(status, provider):
    if status in (401, 403):
        return RouteError(f"{provider}拒绝访问（HTTP {status}），请检查密钥、接口权限或登录状态。")
    if status == 429:
        return RouteError(f"{provider}请求过于频繁（HTTP 429），请稍后重试。")
    return RouteError(f"{provider}服务返回 HTTP {status}，请稍后重试。")


def map_key(provider, prompt):
    label = "高德 Web 服务 Key" if provider == "amap" else "百度服务端 AK"
    env_name = "AMAP_WEBSERVICE_KEY" if provider == "amap" else "BAIDU_MAP_AK"
    guide = AMAP_KEY_GUIDE if provider == "amap" else BAIDU_KEY_GUIDE
    if prompt:
        if not sys.stdin.isatty():
            raise RouteError(f"--prompt-key 需要交互式终端；当前环境请通过 {env_name} 配置密钥。")
        key = getpass.getpass(f"{label}（不回显）: ").strip()
        if key:
            try:
                save_key(provider, key)
            except CredentialError as error:
                raise RouteError(f"{error} 未记住密钥；也可通过 {env_name} 临时使用。") from None
            print(f"{label}已保存到系统凭证库；同一用户和运行环境下次自动读取。", file=sys.stderr)
    else:
        key = os.getenv(env_name, "").strip()
        if not key and sys.platform == "darwin":
            try:
                result = subprocess.run(
                    [sys.executable, "-B", str(Path(__file__).with_name("credentials.py")), provider],
                    capture_output=True, text=True, check=False, timeout=12,
                )
                if result.returncode == 0:
                    key = result.stdout.strip()
                elif result.returncode != 44:  # errSecItemNotFound modulo 256
                    raise RouteError("当前无法读取 macOS 钥匙串，请检查解锁状态和执行环境权限；无需重新申请密钥。")
            except (OSError, subprocess.TimeoutExpired):
                raise RouteError("读取 macOS 钥匙串失败或超时，请检查系统权限；无需重新申请密钥。") from None
        elif not key:
            try:
                key = read_other_platform(provider).strip()
            except CredentialError as error:
                raise RouteError(str(error)) from None
    if not key:
        setup = (
            "创建应用并添加 Key，平台选择 Web 服务，检查骑行规划和地点查询权限。"
            if provider == "amap" else
            "创建服务端应用，启用骑行路线规划（轻量）；按地名规划还需地点检索 3.0。\n"
            "请求校验选择 IP 白名单校验，填写运行环境的公网出口 IP；暂不支持 SN 签名。"
        )
        raise RouteError(
            f"尚未配置{label}。本 Skill 不附带密钥，请先申请自己的凭证：{guide}\n"
            f"{setup}\n"
            f"运行 configure-key {provider} 安全保存，或在原命令末尾加 --prompt-key 输入并记住；也可配置 {env_name}。\n"
            "详细步骤见 references/usage.md。不要把密钥写入 Skill 文件或仓库。"
        )
    return key


class Amap:
    name = "高德"
    def __init__(self, key):
        self.key = key
        self.last_request = -math.inf

    def get(self, endpoint, **params):
        url = f"{AMAP}{endpoint}?{urlencode({**params, 'key': self.key})}"
        for attempt in range(REQUEST_RETRIES + 1):
            time.sleep(max(0, 1.05 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            status, _, body = fetch(url)
            if status != 200:
                raise http_error(status, self.name)
            response = response_json(body, self.name)
            if str(response.get("status")) == "1":
                return response
            code = str(response.get("infocode", "未知"))
            if code in RATE_ERRORS | {"10016"} and attempt < REQUEST_RETRIES:
                retry_notice(attempt, "高德限流或服务繁忙")
                continue
            break
        hints = {
            "10001": "Key 无效或过期，请检查 Web 服务 Key。",
            "10009": "Key 的平台类型不匹配，请使用 Web 服务 Key。",
            "10002": "未开通对应接口或接口不可用，请检查应用的服务权限。",
            "10012": "接口权限不足，请检查应用权限。",
            "10003": "今日调用配额已用完，请等待额度恢复或调整配额。",
            "10005": "IP 白名单不匹配，请检查实际运行环境的出口 IP。",
            "10007": "签名校验失败，当前脚本不支持额外的签名校验配置。",
            "10013": "Key 已删除，请配置新的 Web 服务 Key。",
        }
        if code in hints:
            raise RouteError(f"高德：{hints[code]}（{code}）")
        if code in RATE_ERRORS:
            raise RouteError(f"高德请求频率超过限制（{code}），稍后重试。")
        raise RouteError(f"高德 API 返回错误 {code}。")

    def search(self, keyword, city):
        params = {"keywords": keyword, "page_size": 12}
        if city:
            params.update(region=city, city_limit="true")
        response = self.get("/v5/place/text", **params)
        return [
            {
                "name": poi["name"], "location": poi["location"],
                "address": " ".join(str(poi[field]) for field in ("cityname", "adname", "address")
                                    if isinstance(poi.get(field), str) and poi[field]),
            }
            for poi in response.get("pois", [])
            if poi.get("name") and poi.get("location")
        ]

    def bike_path(self, start, end):
        response = self.get(
            "/v5/direction/bicycling",
            origin=f"{start.lon:.6f},{start.lat:.6f}",
            destination=f"{end.lon:.6f},{end.lat:.6f}",
            show_fields="polyline",
        )
        paths = response.get("route", {}).get("paths") or []
        if not paths:
            raise RouteError("高德没有返回骑行方案。")
        return paths[0]

    track_field = "polyline"

    def bike_leg(self, start, end):
        return route_points(self.bike_path(start, end), self.track_field, self.name)


def route_points(path, field, provider):
    points = []
    steps = path.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RouteError(f"{provider}没有返回完整的道路轨迹。")
    for step in steps:
        polyline = step.get(field) if isinstance(step, dict) else None
        if not isinstance(polyline, str) or not polyline:
            raise RouteError(f"{provider}返回的路段缺少轨迹坐标。")
        step_points = [lon_lat(pair) for pair in polyline.split(";")]
        if points and distance_m(points[-1], step_points[0]) > 2:
            raise RouteError(f"{provider}同一路段的步骤轨迹存在断点，已停止；请调整点位或重新规划，不会用直线补齐。")
        for point in step_points:
            if not points or point != points[-1]:
                points.append(point)
    length = float(path["distance"])
    if len(points) < 2 or not math.isfinite(length) or length <= 0:
        raise RouteError(f"{provider}返回的轨迹点或里程无效。")
    return points, length


class Baidu(Amap):
    name = "百度"

    def get(self, endpoint, **params):
        url = f"{BAIDU}{endpoint}?{urlencode({**params, 'ak': self.key, 'output': 'json'})}"
        for attempt in range(REQUEST_RETRIES + 1):
            time.sleep(max(0, 1.05 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            status, _, body = fetch(url)
            if status != 200:
                raise http_error(status, self.name)
            response = response_json(body, self.name)
            code = str(response.get("status", "未知"))
            if code == "0":
                return response
            if code in {"1", "401", "402"} and attempt < REQUEST_RETRIES:
                retry_notice(attempt, "百度服务暂时繁忙或并发受限")
                continue
            break
        hints = {
            "2": "请求参数无效，请检查地点、坐标和坐标类型。",
            "7": "未找到骑行路线，请调整起终点或途经点。",
            "101": "AK 或服务权限有误，请检查控制台配置。",
            "200": "AK 不存在，请检查服务端 AK。",
            "201": "应用已禁用，请检查控制台。",
            "203": "AK 类型不匹配，请使用服务端 AK。",
            "210": "IP 校验失败，请检查运行环境的出口 IP 与应用白名单。",
            "211": "签名校验失败，当前脚本不支持 SN 签名模式。",
            "240": "接口服务未开通，请检查 AK 的服务权限。",
            "302": "当天调用配额已用完，请等待额度恢复或调整配额。",
            "401": "并发受限，请稍后重试。",
            "402": "并发受限，请稍后重试。",
        }
        raise RouteError(f"百度：{hints.get(code, '接口调用失败，请在百度控制台核对错误码及服务权限。')}（{code}）")

    def search(self, keyword, city):
        if not city:
            raise RouteError("百度按地点规划需要 --city 城市名称，以免选错同名地点。")
        response = self.get("/place/v3/region", query=keyword, region=city,
                            city_limit="true", ret_coordtype="gcj02ll", page_size=10)
        return [
            {"name": poi["name"], "location": f"{poi['location']['lng']},{poi['location']['lat']}",
             "address": " ".join(str(poi[field]) for field in ("city", "area", "address")
                                 if isinstance(poi.get(field), str) and poi[field])}
            for poi in response.get("results", [])
            if isinstance(poi, dict) and poi.get("name") and isinstance(poi.get("location"), dict)
            and "lng" in poi["location"] and "lat" in poi["location"]
        ]

    track_field = "path"

    def bike_path(self, start, end):
        if start.coordinate_system != end.coordinate_system:
            raise RouteError("百度坐标与地点名称混用时，请将显式坐标统一为 gcj02 或 wgs84。")
        response = self.get("/directionlite/v1/riding",
                            origin=f"{start.lat:.6f},{start.lon:.6f}",
                            destination=f"{end.lat:.6f},{end.lon:.6f}",
                            coord_type=start.coordinate_system, ret_coordtype="gcj02",
                            riding_type=0, steps_info=1)
        paths = response.get("result", {}).get("routes") or []
        if not paths:
            raise RouteError("百度没有返回自行车骑行方案。")
        return paths[0]


def extract_link(text):
    links = re.findall(r"(?:https?://|baidumap://)[^\s<>\"“”]+", text)
    if len(links) != 1:
        raise RouteError("请提供一条完整的地图路线分享链接。")
    return links[0].rstrip("，。；！）)]}")


def check_share_url(url, provider):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    hosts = ("amap.com", "gaode.com") if provider == "高德" else ("map.baidu.com",)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or not any(host == base or host.endswith("." + base) for base in hosts)):
        raise RouteError(f"需要{provider}官方 HTTPS 路线链接，不能跟随其他网站或不安全的跳转。")
    return parsed


def parse_share(url):
    # Amap app shares normally redirect to an HTTPS landing URL with a schema query.
    meta = {}
    url = extract_link(url)
    for _ in range(6):
        parsed = check_share_url(url, "高德")
        query = parse_qs(parsed.query)
        if "schema" in query:
            app_link = urlsplit(query["schema"][0])
            if app_link.scheme != "amapuri" or app_link.netloc != "route" or app_link.path.rstrip("/") != "/plan":
                raise RouteError("这是高德分享链接，但其中没有可以规划的路线。")
            params = {key: values[0] for key, values in parse_qs(app_link.query).items()}
            try:
                meta = json.loads(query.get("share_bizParams", ["{}"])[0])
            except json.JSONDecodeError:
                pass
            break
        status, target, _ = fetch(url)
        if status not in (301, 302, 303, 307, 308) or not target:
            raise RouteError("分享页没有提供路线参数。请从高德 App 的骑行路线详情复制链接。")
        url = urljoin(url, target)
    else:
        raise RouteError("高德分享链接跳转次数过多。")
    if params.get("t") != "3" or params.get("rideType", "bike") != "bike":
        raise RouteError("这个分享链接没有指定自行车路线。")
    if params.get("dev") != "0":
        raise RouteError("这个高德分享链接的坐标格式尚未支持。")
    if not all(params.get(field) for field in ("slon", "slat", "dlon", "dlat")):
        raise RouteError("高德分享链接缺少起终点坐标，请重新分享自行车路线。")
    start = Stop(params.get("sname", "起点"), *lon_lat(f"{params['slon']},{params['slat']}"))
    end = Stop(params.get("dname", "终点").rstrip("."), *lon_lat(f"{params['dlon']},{params['dlat']}"))
    count = int(params.get("vian", "0"))
    if count < 0 or (count == 0 and any(params.get(field) for field in ("vialons", "vialats", "vianames"))):
        raise RouteError("分享链接中的途经点数量与内容不一致，不能忽略途经点导出。")
    stops = [start]
    if count:
        names = params.get("vianames", "").split("|")
        coordinates = list(zip(params["vialons"].split("|"), params["vialats"].split("|"), strict=True))
        if len(coordinates) != count:
            raise RouteError("分享链接中的途经点数量与坐标不一致。")
        for index, (lon, lat) in enumerate(coordinates):
            name = names[index] if index < len(names) and names[index] else f"途经点 {index + 1}"
            stops.append(Stop(name, *lon_lat(f"{lon},{lat}")))
    stops.append(end)
    raw_distance = meta.get("naviDistance") if isinstance(meta, dict) else None
    try:
        shared_distance = float(raw_distance)
        if not math.isfinite(shared_distance) or shared_distance <= 0:
            shared_distance = None
    except (ValueError, TypeError):
        shared_distance = None
    return stops, shared_distance


def parse_baidu_share(text):
    """Read documented bikenavi coordinates; never infer undocumented App payloads."""
    url = extract_link(text)
    # Legacy map short links are often copied with http; request HTTPS instead.
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    for _ in range(6):
        parsed = urlsplit(url)
        if parsed.scheme == "baidumap":
            if parsed.netloc != "map" or parsed.path.rstrip("/") != "/bikenavi":
                raise RouteError("百度链接不是可解析的自行车导航链接，请提供有序地点列表。")
            params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            return baidu_uri_stops(params), None
        parsed = check_share_url(url, "百度")
        query = parse_qs(parsed.query)
        if "s" in query:
            if any(key in query for key in ("viaPoints", "waypoints", "via", "via_points")):
                raise RouteError("百度链接含有未支持的途经点组合，请提供有序地点列表。")
            return baidu_web_stops(query["s"][0]), None
        nested = [query[key][0] for key in ("url", "schema") if key in query]
        if len(nested) == 1 and nested[0].startswith("baidumap://map/bikenavi?"):
            url = nested[0]
            continue
        status, redirect, _ = fetch(url)
        if status in (301, 302, 303, 307, 308) and redirect:
            url = urljoin(url, redirect)
            continue
        if status >= 400:
            raise http_error(status, "百度分享页")
        raise RouteError("这个百度分享页没有提供可解析的自行车导航参数。请提供城市及按顺序排列的起点、途经点、终点，使用 baidu-plan；不会忽略途经点或改用其他地图。")
    raise RouteError("百度分享链接跳转次数过多，请提供地点列表。")


def baidu_web_stops(route):
    # Format observed from a real j.map.baidu.com cycling share, not a POI link.
    mode, _, query = route.partition("&")
    if mode != "cycle":
        raise RouteError("百度分享链接不是自行车路线，请从骑行路线页面重新分享。")
    params = parse_qs(query, keep_blank_values=True)
    if set(params) - {"c", "sn", "en", "sc", "ec"}:
        raise RouteError("百度分享路线含有尚未支持的参数，可能包含途经点；请提供完整的有序地点列表。")
    stops = []
    for field, label in (("sn", "起点"), ("en", "终点")):
        values = params.get(field, [])
        if len(values) != 1:
            raise RouteError("百度分享链接缺少唯一的起终点信息。")
        fields = values[0].split("$$")
        if len(fields) < 5 or not re.fullmatch(COORDINATE, fields[2]):
            raise RouteError("百度分享链接没有可解析的起终点坐标，请提供地点列表。")
        x, y = map(float, fields[2].replace("，", ",").split(","))
        if not (math.isfinite(x) and math.isfinite(y) and abs(x) <= 21000000 and abs(y) <= 13000000):
            raise RouteError("百度分享中的墨卡托坐标超出支持范围。")
        stops.append(Stop(fields[3] or label, x, y, "bd09mc"))
    return stops


def baidu_uri_stops(params):
    system = params.get("coord_type", "bd09ll")
    if system not in ("bd09ll", "gcj02", "wgs84"):
        raise RouteError("暂不支持此百度链接的坐标类型，请改用地点列表或经纬度坐标。")
    if any(key in params for key in ("waypoints", "via", "via_points")):
        raise RouteError("此百度链接含有未支持的途经点参数，请提供有序地点列表。")

    def make_stop(name, lon, lat):
        lon, lat = lon_lat(f"{lon},{lat}")
        if system == "wgs84":
            lon, lat = wgs84_to_gcj02(lon, lat)
        return Stop(name, lon, lat, "gcj02" if system == "wgs84" else system)

    def endpoint(field, name):
        value = params.get(field, "")
        if not re.fullmatch(COORDINATE, value):
            raise RouteError("百度自行车导航链接缺少明确的起终点经纬度，请提供地点列表。")
        lat, lon = value.replace("，", ",").split(",")
        return make_stop(name, lon, lat)

    stops = [endpoint("origin", "起点")]
    if "viaPoints" in params:
        try:
            payload = json.loads(params["viaPoints"])
            vias = payload["viaPoints"]
            if not isinstance(vias, list):
                raise ValueError()
            for i, via in enumerate(vias, 1):
                stops.append(make_stop(via.get("name") or f"途经点 {i}", via["lng"], via["lat"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RouteError("百度链接中的途经点格式不完整，请提供有序地点列表。") from None
    stops.append(endpoint("destination", "终点"))
    return stops


def normalize_name(name):
    return "".join(char for char in unicodedata.normalize("NFKC", name).casefold() if char.isalnum())


def resolve_stop(amap, text, city, coordinate_system):
    name, separator, position = text.rpartition("|")
    if separator or re.fullmatch(COORDINATE, text):
        if not coordinate_system:
            raise RouteError("请用 --coordinates 指明坐标类型：gcj02、wgs84，或百度的 bd09ll。")
        lon, lat = lon_lat(position if separator else text)
        if coordinate_system == "wgs84":
            lon, lat = wgs84_to_gcj02(lon, lat)
        if coordinate_system == "bd09ll" and not isinstance(amap, Baidu):
            raise RouteError("高德规划请使用 gcj02 或 wgs84 坐标。")
        return Stop(name or text, lon, lat, "bd09ll" if coordinate_system == "bd09ll" else "gcj02")
    candidates = amap.search(text, city)
    normalized = normalize_name(text)
    exact = {item["location"]: item for item in candidates if normalize_name(item["name"]) == normalized}
    if city and not exact:
        qualified = normalize_name(city.removesuffix("市")) + normalized
        exact = {item["location"]: item for item in candidates if normalize_name(item["name"]) == qualified}
    if len(exact) == 1:
        choice = next(iter(exact.values()))
        return Stop(choice["name"], *lon_lat(choice["location"]))
    if not candidates:
        raise RouteError(f"{amap.name}找不到「{text}」。请补充城市、区或入口名。")
    options = list(exact.values()) if exact else candidates
    display = "\n".join(f"- {item['name']} · {item['address']} · {item['location']}" for item in options[:5])
    raise RouteError(f"请确认「{text}」对应的地点：\n{display}\n选定后用 --point '名称|经度,纬度' --coordinates gcj02 重试。")


def plan_track(amap, stops, diagnostics=None):
    segments = []
    total = 0.0
    for index, (start, end) in enumerate(zip(stops, stops[1:]), 1):
        if start.point == end.point and start.coordinate_system == end.coordinate_system:
            if diagnostics is not None:
                diagnostics.append({"leg": index, "start": start.name, "end": end.name,
                                    "distance_km": 0, "skipped_identical_points": True})
            continue
        print(f"正在规划第 {index}/{len(stops) - 1} 段：{start.name} → {end.name}", file=sys.stderr)
        try:
            points, length = amap.bike_leg(start, end)
        except RouteError as error:
            raise RouteError(f"第 {index} 段（{start.name} → {end.name}）：{error}") from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RouteError(f"第 {index} 段（{start.name} → {end.name}）：地图返回的轨迹数据不完整。") from None
        leg_start, leg_end, leg_count = points[0], points[-1], len(points)
        if not segments or distance_m(segments[-1][-1], points[0]) > 2:
            segments.append(points)
        else:
            if segments[-1][-1] == points[0]:
                points = points[1:]
            segments[-1].extend(points)
        total += length
        if diagnostics is not None:
            detail = {"leg": index, "start": start.name, "end": end.name,
                      "distance_km": round(length / 1000, 3), "track_points": leg_count}
            if start.coordinate_system == end.coordinate_system == "gcj02":
                detail["start_offset_m"] = round(distance_m(start.point, leg_start), 1)
                detail["end_offset_m"] = round(distance_m(end.point, leg_end), 1)
            diagnostics.append(detail)
    if not segments:
        raise RouteError("起点、途经点和终点重合，无法生成线路。")
    return segments, total


def gpx_bytes(name, description, segments):
    ET.register_namespace("", GPX)
    node = lambda tag: f"{{{GPX}}}{tag}"
    root = ET.Element(node("gpx"), version="1.1", creator="cycling-route-gpx")
    track = ET.SubElement(root, node("trk"))
    ET.SubElement(track, node("name")).text = name
    ET.SubElement(track, node("desc")).text = description
    for points in segments:
        segment = ET.SubElement(track, node("trkseg"))
        for lon, lat in points:
            lon, lat = gcj02_to_wgs84(lon, lat)
            ET.SubElement(segment, node("trkpt"), lat=f"{lat:.8f}", lon=f"{lon:.8f}")
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def normalize_igpsport(body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise RouteError("iGPSPORT 没有返回 GPX。请核对路书编号、站点和访问权限。") from None
    versions = {f"{{{GPX}}}gpx": GPX, f"{{{GPX_10}}}gpx": GPX_10}
    namespace = versions.get(root.tag)
    if namespace is None:
        raise RouteError("iGPSPORT 返回了无法识别的 GPX 文件。")
    ns = {"g": namespace}
    tracks = root.findall("g:trk", ns)
    routes = root.findall("g:rte", ns)
    title = next((item for node in [*tracks, *routes, root] if
                  (item := node.findtext("g:name", namespaces=ns))), None)
    groups = [segment.findall("g:trkpt", ns) for segment in root.findall("g:trk/g:trkseg", ns)]
    groups += [route.findall("g:rtept", ns) for route in routes]
    total = points = 0
    for group in groups:
        coordinates = [lon_lat(f"{point.attrib['lon']},{point.attrib['lat']}") for point in group]
        points += len(coordinates)
        total += sum(distance_m(a, b) for a, b in zip(coordinates, coordinates[1:]))
    if not any(len(group) >= 2 for group in groups):
        raise RouteError("iGPSPORT 返回的文件没有可用的路线轨迹。")
    changed = False
    if namespace == GPX:
        if "schemaLocation" in root.attrib:
            root.attrib.setdefault(f"{{{XSI}}}schemaLocation", root.attrib.pop("schemaLocation"))
            changed = True
        metadata = root.find("g:metadata", ns)
        if metadata is not None:
            order = ["name", "desc", "author", "copyright", "link", "time", "keywords", "bounds", "extensions"]
            rank = {f"{{{GPX}}}{tag}": index for index, tag in enumerate(order)}
            sorted_nodes = sorted(metadata, key=lambda node: rank.get(node.tag, len(order)))
            if list(metadata) != sorted_nodes:
                metadata[:] = sorted_nodes
                changed = True
    if changed:
        ET.register_namespace("", namespace)
        ET.register_namespace("xsi", XSI)
        body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return body, title, points, total


def download_igpsport(route_id, region):
    if not re.fullmatch(r"[0-9]+", route_id):
        raise RouteError("iGPSPORT 路书编号需要是数字。")
    url = f"https://{IGPSPORT[region]}/service/web/api/Routes/ExportRoutesWeb?routeId={route_id}&fileType=3"
    status, _, body = fetch(url, follow=True)
    if status == 401:
        raise RouteError("这条路书需要网页会话或访问权限，请在 iGPSPORT 官网页面登录后导出。")
    if status == 404:
        raise RouteError("未找到这条 iGPSPORT 路书（HTTP 404），请核对编号和国内站/国际站。")
    if status in (200, 400, 403):
        try:
            error = json.loads(body)
        except ValueError:
            error = None
        if isinstance(error, dict) and str(error.get("code")) == "5007":
            raise RouteError("iGPSPORT 未识别该路书编号（5007）。请核对编号及国内站或国际站。")
        if status == 403:
            raise RouteError("iGPSPORT 拒绝下载（HTTP 403）。请核对站点和路书访问权限。")
    if status != 200:
        raise http_error(status, "iGPSPORT")
    return normalize_igpsport(body)


def save_file(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        draft = Path(temp.name)
        try:
            temp.write(body)
            temp.close()
            os.link(draft, path)
        finally:
            draft.unlink(missing_ok=True)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure-key", help="安全输入并记住地图密钥；再次运行可更新")
    configure.add_argument("provider", choices=("amap", "baidu"))
    configure.set_defaults(timeout=25, retries=2, format="json")
    check = commands.add_parser("check-key", help="检查已有凭证是否可读取；不回显密钥")
    check.add_argument("provider", choices=("amap", "baidu"))
    check.add_argument("--verify-api", action="store_true", help="同时验证地点查询接口权限")
    check.set_defaults(timeout=25, retries=0, format="json")
    render = commands.add_parser("render-preview", help="从预览缓存重新生成路线图，不调用地图接口")
    render.add_argument("preview", type=Path)
    render.add_argument("--output", type=Path, required=True)
    export = commands.add_parser("export-preview", help="用户确认后从原预览缓存导出 GPX，不重新规划")
    export.add_argument("preview", type=Path)
    export.add_argument("--confirmed-sha256", required=True)
    export.add_argument("--output", type=Path)
    for command in (render, export):
        command.set_defaults(timeout=25, retries=0, format="json")
    download = commands.add_parser("igpsport", help="按路书编号下载 iGPSPORT GPX")
    download.add_argument("route_id")
    download.add_argument("--region", choices=IGPSPORT, default="cn")
    auto = commands.add_parser("auto", help="自动识别路书编号或高德/百度分享链接")
    auto.add_argument("source")
    auto.add_argument("--region", choices=IGPSPORT, default="cn")
    auto.add_argument("--prompt-key", action="store_true")
    all_commands, exports = [download, auto], [download, auto]
    for provider, label in (("amap", "高德"), ("baidu", "百度")):
        share = commands.add_parser(f"{provider}-share", help=f"解析{label}骑行链接并重新规划")
        share.add_argument("url")
        plan = commands.add_parser(f"{provider}-plan", help=f"按有序地点规划{label}自行车路线")
        plan.add_argument("--point", action="append", required=True)
        plan.add_argument("--city")
        plan.add_argument("--coordinates", choices=["gcj02", "wgs84", "bd09ll"] if provider == "baidu" else ["gcj02", "wgs84"])
        preview = commands.add_parser(f"{provider}-preview", help=f"规划{label}路线并保存预览 JSON 和 HTML，不生成 GPX")
        preview.add_argument("--point", action="append", required=True)
        preview.add_argument("--city")
        preview.add_argument("--coordinates", choices=["gcj02", "wgs84", "bd09ll"] if provider == "baidu" else ["gcj02", "wgs84"])
        preview.add_argument("--output", type=Path, required=True, help="预览 .json 路径，同时生成同名 .html")
        preview.add_argument("--note", action="append", default=[], help="路线要求或待确认部分，可重复")
        preview.add_argument("--prompt-key", action="store_true")
        for command in (share, plan):
            command.add_argument("--prompt-key", action="store_true")
        all_commands.extend((share, plan, preview))
        exports.extend((share, plan))
    for command in exports:
        command.add_argument("--output", type=Path, help="GPX 路径；省略时自动命名保存到当前目录的 outputs/")
    for command in all_commands:
        command.add_argument("--timeout", type=int, default=25, choices=range(1, 121), metavar="1-120", help="单次网络请求的超时秒数，默认 25")
        command.add_argument("--retries", type=int, default=2, choices=range(0, 4), metavar="0-3", help="暂时性错误的额外重试次数，默认 2")
        command.add_argument("--format", choices=["json", "text"], default="json", help="输出格式，默认 json；text 为中文摘要")
    return parser.parse_args(argv)


def identify_source(args):
    if args.command != "auto":
        return
    source = args.source.strip()
    if re.fullmatch(r"[0-9]+", source):
        args.command, args.route_id = "igpsport", source
        return
    link = extract_link(source)
    parsed = urlsplit(link)
    host = parsed.hostname or ""
    if parsed.scheme == "baidumap" or host == "map.baidu.com" or host.endswith(".map.baidu.com"):
        args.command = "baidu-share"
    elif any(host == base or host.endswith("." + base) for base in ("amap.com", "gaode.com")):
        args.command = "amap-share"
    else:
        raise RouteError("暂不支持这个链接，请提供 iGPSPORT 路书编号、高德或百度骑行分享。")
    args.url = link


def save_output(requested, name, body):
    if requested is not None:
        save_file(requested, body)
        return requested
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(". ")[:80] or "cycling-route"
    for index in range(10000):
        suffix = f"-{index + 1}" if index else ""
        path = Path("outputs") / f"{stem}{suffix}.gpx"
        try:
            save_file(path, body)
            return path
        except FileExistsError:
            continue
    raise RouteError("自动命名遇到过多同名文件，请用 --output 指定新文件名。")


def run(args):
    identify_source(args)
    if getattr(args, "output", None) and args.output.exists():
        raise RouteError("输出文件已存在，请更换 --output 文件名；省略 --output 可自动命名。")
    if args.command == "check-key":
        key = map_key(args.provider, False)
        if args.verify_api:
            client = (Amap if args.provider == "amap" else Baidu)(key)
            client.search("人民广场", "上海市")
        env_name = "AMAP_WEBSERVICE_KEY" if args.provider == "amap" else "BAIDU_MAP_AK"
        return {"mode":"credentials", "provider":args.provider, "readable":True,
                "source":"environment" if os.getenv(env_name, "").strip() else "system_store",
                "api_verified":bool(args.verify_api)}
    if args.command in {"amap-preview", "baidu-preview", "render-preview", "export-preview"}:
        import route_preview
        handler = route_preview.preview if args.command in {"amap-preview", "baidu-preview"} else (
            route_preview.render if args.command == "render-preview" else route_preview.export)
        return handler(args, sys.modules[__name__])
    shared_distance = None
    gaps = 0
    waypoint_count = None
    warnings, legs, stops = [], [], []
    if args.command == "igpsport":
        body, name, count, meters = download_igpsport(args.route_id, args.region)
        name = (name or "").strip() or f"iGPSPORT 路书 {args.route_id}"
        provider, mode = "iGPSPORT", "download"
    else:
        provider_id = args.command.split("-")[0]
        if args.command == "amap-share":
            stops, shared_distance = parse_share(args.url)
        elif args.command == "baidu-share":
            stops, shared_distance = parse_baidu_share(args.url)
        if args.command.endswith("-plan"):
            if len(args.point) < 2:
                raise RouteError("至少需要起点和终点。")
        amap = (Amap if provider_id == "amap" else Baidu)(map_key(provider_id, args.prompt_key))
        provider = amap.name
        if args.command.endswith("-plan"):
            stops = [resolve_stop(amap, text, args.city, args.coordinates) for text in args.point]
        segments, meters = plan_track(amap, stops, legs)
        count = sum(map(len, segments))
        gaps = len(segments) - 1
        waypoint_count = len(stops) - 2
        name = f"{stops[0].name} → {stops[-1].name}"
        mode = "replanned" if args.command.endswith("-share") else "planned"
        if mode == "replanned":
            name += f"（{provider}重新规划）"
            warnings.append(f"按分享点位使用{provider}自行车接口重新规划，可能与 App 原方案不同。")
        description = f"{provider}自行车路径规划。坐标由 GCJ-02 近似转换为 WGS84。"
        if waypoint_count:
            description += "途经：" + " → ".join(stop.name for stop in stops[1:-1])
        body = gpx_bytes(name, description, segments)
        if gaps:
            warnings.append(f"有 {gaps} 处路段未衔接，GPX 已保留分段。")
        for leg in legs:
            if max(leg.get("start_offset_m", 0), leg.get("end_offset_m", 0)) > 100:
                warnings.append(f"第 {leg['leg']} 段的道路轨迹与指定点位相距超过 100 米，请核对入口或坐标。")
    output = save_output(args.output, args.route_id if args.command == "igpsport" else name, body)
    result = {
        "file": str(output.resolve()), "name": name, "track_points": count,
        "distance_km": round(meters / 1000, 3), "provider": provider, "mode": mode,
        "warnings": warnings,
    }
    if waypoint_count is not None:
        result.update(waypoints=waypoint_count, disconnected_joins=gaps, legs=legs,
                      stops=[{"name": stop.name, "coordinates": stop.coordinate_system,
                              **({"x": stop.lon, "y": stop.lat} if stop.coordinate_system == "bd09mc"
                                 else {"longitude": stop.lon, "latitude": stop.lat})} for stop in stops])
    if shared_distance is not None:
        result["shared_distance_km"] = round(shared_distance / 1000, 3)
        result["distance_difference_km"] = round((meters - shared_distance) / 1000, 3)
        result["distance_difference_percent"] = round((meters - shared_distance) / shared_distance * 100, 1)
        if abs(meters - shared_distance) > max(500, shared_distance * 0.1):
            warnings.append("重新规划里程与分享里程相差超过 10% 且超过 500 米，请核对路线。")
    return result


def display_result(result, output_format):
    if output_format == "json" or result.get("mode") in {"preview", "credentials"}:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"已导出：{result['name']}\n来源：{result['provider']}\n里程：{result['distance_km']} 公里 · {result['track_points']} 个轨迹点\n文件：{result['file']}")
        if "shared_distance_km" in result:
            print(f"分享里程：{result['shared_distance_km']} 公里 · 差值：{result['distance_difference_km']:+} 公里")
        for warning in result["warnings"]:
            print(f"提示：{warning}")


def main():
    global REQUEST_TIMEOUT, REQUEST_RETRIES
    args = arguments()
    REQUEST_TIMEOUT, REQUEST_RETRIES = args.timeout, args.retries
    try:
        if args.command == "configure-key":
            map_key(args.provider, True)
            return 0
        display_result(run(args), args.format)
    except KeyboardInterrupt:
        print("已取消导出。", file=sys.stderr)
        return 130
    except FileExistsError:
        print("导出失败：文件已存在，请更换输出文件名。", file=sys.stderr)
        return 1
    except (RouteError, OSError) as error:
        print(f"导出失败：{error}", file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        print("导出失败：路线参数或服务返回数据不完整，未生成 GPX。请检查链接或地点后重试。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
