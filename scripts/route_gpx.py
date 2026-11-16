"""Download iGPSPORT roadbooks and export Amap bicycle routes as GPX."""

import argparse
from dataclasses import dataclass
import getpass
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import parse_qs, urlencode, urlsplit
import xml.etree.ElementTree as ET

from coordinates import gcj02_to_wgs84, wgs84_to_gcj02


GPX = "http://www.topografix.com/GPX/1/1"
GPX_10 = "http://www.topografix.com/GPX/1/0"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
AMAP = "https://restapi.amap.com"
AMAP_KEY_GUIDE = "https://lbs.amap.com/api/webservice/create-project-and-key"
IGPSPORT = {"cn": "prod.zh.igpsport.com", "intl": "prod.en.igpsport.com"}
RATE_ERRORS = {"10014", "10015", "10019", "10020", "10021", "10022", "10023"}
COORDINATE = r"[-+]?\d+(?:\.\d+)?\s*[,，]\s*[-+]?\d+(?:\.\d+)?"


class RouteError(Exception):
    pass


@dataclass(frozen=True)
class Stop:
    name: str
    lon: float
    lat: float

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
    with tempfile.TemporaryDirectory() as folder:
        body = Path(folder) / "response"
        command = [
            "curl", "--disable", "--silent", "--show-error", "--max-time", "25",
            "--proto", "=https", "--proto-redir", "=https", "--max-filesize", "30000000",
            "--output", str(body), "--write-out", "%{http_code}\n%{redirect_url}\n",
        ]
        if follow:
            command.extend(["--location", "--max-redirs", "5"])
        result = subprocess.run(
            [*command, "--config", "-"], input=f'url = "{url}"\n',
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise RouteError(f"网络请求失败（curl {result.returncode}）。")
        code, redirect = result.stdout.splitlines()[:2]
        return int(code), redirect, body.read_bytes() if body.exists() else b""


def amap_key(prompt):
    if prompt:
        if not sys.stdin.isatty():
            raise RouteError("请在终端中使用 --prompt-key，以便安全输入 Key。")
        key = getpass.getpass("高德 Web 服务 Key（不回显）: ").strip()
    else:
        key = os.getenv("AMAP_WEBSERVICE_KEY", "").strip()
        if not key and sys.platform == "darwin":
            result = subprocess.run(
                ["security", "find-generic-password", "-a", getpass.getuser(),
                 "-s", "cycling-route-gpx-amap", "-w"],
                capture_output=True, text=True, check=False, timeout=12,
            )
            if result.returncode == 0:
                key = result.stdout.strip()
    if not key:
        raise RouteError(
            "第一次导出高德路线，需要先在高德申请一个 Key。\n"
            f"按高德的图文步骤操作，在「添加 Key」时选「Web 服务」。{AMAP_KEY_GUIDE}\n"
            "拿到后，在原命令末尾加 --prompt-key 重新运行，按提示输入即可。"
        )
    return key


class Amap:
    def __init__(self, key):
        self.key = key
        self.last_request = -math.inf

    def get(self, endpoint, **params):
        url = f"{AMAP}{endpoint}?{urlencode({**params, 'key': self.key})}"
        for backoff in (0, 2, 4):
            time.sleep(max(backoff, 1.05 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            status, _, body = fetch(url)
            if status != 200:
                raise RouteError(f"高德 API 返回 HTTP {status}。")
            response = json.loads(body)
            if str(response.get("status")) == "1":
                return response
            code = str(response.get("infocode", "未知"))
            if code not in RATE_ERRORS:
                break
        if code == "10001":
            raise RouteError(
                "这个高德 Key 暂时用不了（10001）。请在高德检查它的服务平台是否选了「Web 服务」。\n"
                f"官方申请步骤 {AMAP_KEY_GUIDE}"
            )
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

    def bike_leg(self, start, end):
        response = self.get(
            "/v5/direction/bicycling",
            origin=f"{start.lon:.6f},{start.lat:.6f}",
            destination=f"{end.lon:.6f},{end.lat:.6f}",
            show_fields="polyline",
        )
        paths = response.get("route", {}).get("paths") or []
        if not paths:
            raise RouteError("高德没有返回骑行方案。")
        path = paths[0]
        points = []
        for step in path["steps"]:
            polyline = step.get("polyline")
            if not isinstance(polyline, str) or not polyline:
                raise RouteError("高德返回的路段缺少轨迹坐标。")
            for pair in polyline.split(";"):
                point = lon_lat(pair)
                if not points or point != points[-1]:
                    points.append(point)
        if len(points) < 2:
            raise RouteError("高德返回的轨迹不足两个点。")
        return points, float(path["distance"])


def parse_share(url):
    # Amap app shares normally redirect to an HTTPS landing URL with a schema query.
    meta = {}
    for _ in range(5):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or not parsed.hostname or parsed.port not in (None, 443)):
            raise RouteError("请输入高德 HTTPS 分享链接。")
        host = parsed.hostname
        if not (host == "amap.com" or host.endswith((".amap.com", ".gaode.com"))):
            raise RouteError("分享链接跳转到了高德以外的网站。")
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
        url = target
    else:
        raise RouteError("高德分享链接跳转次数过多。")
    if params.get("t") != "3" or params.get("rideType", "bike") != "bike":
        raise RouteError("这个分享链接没有指定自行车路线。")
    if params.get("dev") != "0":
        raise RouteError("这个高德分享链接的坐标格式尚未支持。")
    start = Stop(params.get("sname", "起点"), *lon_lat(f"{params['slon']},{params['slat']}"))
    end = Stop(params.get("dname", "终点").rstrip("."), *lon_lat(f"{params['dlon']},{params['dlat']}"))
    count = int(params.get("vian", "0"))
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
    shared_distance = float(raw_distance) if isinstance(raw_distance, (int, float)) else None
    return stops, shared_distance


def normalize_name(name):
    return "".join(char for char in unicodedata.normalize("NFKC", name).casefold() if char.isalnum())


def resolve_stop(amap, text, city, coordinate_system):
    name, separator, position = text.rpartition("|")
    if separator or re.fullmatch(COORDINATE, text):
        if not coordinate_system:
            raise RouteError("请用 --coordinates 指明这些坐标来自高德（gcj02）还是 GPS（wgs84）。")
        lon, lat = lon_lat(position if separator else text)
        if coordinate_system == "wgs84":
            lon, lat = wgs84_to_gcj02(lon, lat)
        return Stop(name or text, lon, lat)
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
        raise RouteError(f"高德找不到「{text}」。请补充城市、区或入口名。")
    options = list(exact.values()) if exact else candidates
    display = "\n".join(f"- {item['name']} · {item['address']} · {item['location']}" for item in options[:5])
    raise RouteError(f"请确认「{text}」对应的地点：\n{display}")


def plan_track(amap, stops):
    segments = []
    total = 0.0
    for index, (start, end) in enumerate(zip(stops, stops[1:]), 1):
        if start.point == end.point:
            continue
        try:
            points, length = amap.bike_leg(start, end)
        except (RouteError, ValueError, KeyError, TypeError) as error:
            raise RouteError(f"第 {index} 段（{start.name} → {end.name}）：{error}") from None
        if not segments or distance_m(segments[-1][-1], points[0]) > 2:
            segments.append(points)
        else:
            if segments[-1][-1] == points[0]:
                points = points[1:]
            segments[-1].extend(points)
        total += length
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
    if status == 403:
        try:
            error = json.loads(body)
        except ValueError:
            error = None
        if isinstance(error, dict) and str(error.get("code")) == "5007":
            raise RouteError("iGPSPORT 未识别该路书编号（5007）。请核对编号及国内站或国际站。")
        raise RouteError("iGPSPORT 拒绝下载（HTTP 403）。请核对站点和路书访问权限。")
    if status != 200:
        raise RouteError(f"iGPSPORT 返回 HTTP {status}。请核对站点和路书编号。")
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


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("igpsport", help="按路书编号下载 iGPSPORT GPX")
    download.add_argument("route_id")
    download.add_argument("--region", choices=IGPSPORT, default="cn")
    share = commands.add_parser("amap-share", help="按高德分享点位重新规划并导出")
    share.add_argument("url")
    plan = commands.add_parser("amap-plan", help="按有序地点规划骑行路线")
    plan.add_argument("--point", action="append", required=True)
    plan.add_argument("--city")
    plan.add_argument("--coordinates", choices=["gcj02", "wgs84"])
    search = commands.add_parser("amap-search", help="查找高德地点候选")
    search.add_argument("keyword")
    search.add_argument("--city")
    for command in (download, share, plan):
        command.add_argument("--output", required=True, type=Path)
    for command in (share, plan, search):
        command.add_argument("--prompt-key", action="store_true")
    return parser.parse_args()


def run(args):
    shared_distance = None
    gaps = 0
    waypoint_count = None
    if args.command == "igpsport":
        body, name, count, meters = download_igpsport(args.route_id, args.region)
        name = name or f"iGPSPORT 路书 {args.route_id}"
    else:
        if args.command == "amap-share":
            stops, shared_distance = parse_share(args.url)
        amap = Amap(amap_key(args.prompt_key))
        if args.command == "amap-search":
            return amap.search(args.keyword, args.city)
        if args.command == "amap-plan":
            if len(args.point) < 2:
                raise RouteError("至少需要起点和终点。")
            stops = [resolve_stop(amap, text, args.city, args.coordinates) for text in args.point]
        segments, meters = plan_track(amap, stops)
        count = sum(map(len, segments))
        gaps = len(segments) - 1
        waypoint_count = len(stops) - 2
        name = f"{stops[0].name} → {stops[-1].name}"
        if args.command == "amap-share":
            name += "（高德重新规划）"
        description = "高德自行车路径规划。坐标由 GCJ-02 近似转换为 WGS84。"
        if waypoint_count:
            description += "途经：" + " → ".join(stop.name for stop in stops[1:-1])
        body = gpx_bytes(name, description, segments)
    save_file(args.output, body)
    result = {
        "file": str(args.output.resolve()), "name": name, "track_points": count,
        "distance_km": round(meters / 1000, 3),
    }
    if waypoint_count is not None:
        result.update(waypoints=waypoint_count, disconnected_joins=gaps)
    if shared_distance is not None:
        result["shared_distance_km"] = round(shared_distance / 1000, 3)
    return result


def main():
    args = arguments()
    try:
        if getattr(args, "output", None) and args.output.exists():
            raise RouteError("输出文件已存在，请更换文件名。")
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
    except (RouteError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"导出失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
