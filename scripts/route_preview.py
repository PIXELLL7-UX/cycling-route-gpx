"""Portable route preview cache, HTML renderer and offline confirmed export."""
import hashlib
import html
import json
import math
from pathlib import Path


def digest(body):
    return hashlib.sha256(body).hexdigest()


def load_cache(path, r, expected=None):
    body = path.read_bytes()
    if expected is not None and digest(body) != expected:
        raise r.RouteError("预览数据已变化或确认标识不匹配，请重新展示路线并确认。")
    data = json.loads(body)
    if data.get("schema") != "cycling-route-preview/v1" or data.get("coordinate_system") != "gcj02":
        raise r.RouteError("不支持的预览文件或坐标系，请重新生成预览。")
    if data.get("provider") not in {"高德", "百度"}:
        raise r.RouteError("预览文件缺少地图来源。")
    if not isinstance(data.get("name"), str) or not data["name"]:
        raise r.RouteError("预览文件缺少路线名称。")
    distance = data.get("distance_m")
    if not isinstance(distance, (int, float)) or not math.isfinite(distance) or distance <= 0:
        raise r.RouteError("预览里程无效。")
    if not isinstance(data.get("segments"), list) or not data["segments"]:
        raise r.RouteError("预览文件没有轨迹。")
    if not isinstance(data.get("warnings"), list) or not all(isinstance(w,str) for w in data["warnings"]):
        raise r.RouteError("预览文件的待确认事项不完整。")
    for segment in data["segments"]:
        if not isinstance(segment, list) or len(segment) < 2:
            raise r.RouteError("预览轨迹分段不完整。")
        for point in segment:
            if not isinstance(point, list) or len(point) != 2:
                raise r.RouteError("预览轨迹坐标无效。")
            r.lon_lat(f"{point[0]},{point[1]}")
    return data, digest(body)


def html_bytes(data, fingerprint, r):
    # Only serialize map data, never API response URLs or credentials.
    converted = [[list(r.gcj02_to_wgs84(*p)) for p in segment] for segment in data["segments"]]
    steps = [{"name":s["name"], "distance_m":s["distance_m"],
              "points":[list(r.gcj02_to_wgs84(*p)) for p in s["points"]]} for s in data["steps"]]
    markers = [{"name":s["name"], "point":list(r.gcj02_to_wgs84(*s["point"]))} for s in data["markers"]]
    payload = {"segments":converted, "steps":steps, "markers":markers}
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = (Path(__file__).parent.parent / "assets/route-preview.html").read_text()
    fields = {"__TITLE__":html.escape(data["name"]), "__PROVIDER__":html.escape(data["provider"]),
              "__DISTANCE__":f'{data["distance_m"]/1000:.3f}', "__SHA256__":fingerprint,
              "__WARNINGS__":"".join(f"<li>{html.escape(w)}</li>" for w in data["warnings"]),
              "__DATA__":encoded}
    # Single pass: user-controlled names cannot introduce template tokens.
    import re
    return re.sub("|".join(fields), lambda m:fields[m.group()], template).encode()


def preview(args, r):
    cache = args.output
    page = cache.with_suffix(".html")
    if cache.suffix.lower() != ".json":
        raise r.RouteError("预览 --output 请指定 .json 文件；会同时生成同名 .html 路线图。")
    if cache.exists() or page.exists():
        raise r.RouteError("预览文件已存在，请使用新文件名，避免改变已展示路线。")
    if len(args.point) < 2:
        raise r.RouteError("至少需要起点和终点。")
    provider = args.command.split("-")[0]
    client = (r.Amap if provider == "amap" else r.Baidu)(r.map_key(provider, args.prompt_key))
    stops = [r.resolve_stop(client, p, args.city, args.coordinates) for p in args.point]
    steps, markers, diagnostics = [], [], []

    class Collector:
        def bike_leg(self, start, end):
            path = client.bike_path(start, end)
            points, distance = r.route_points(path, client.track_field, client.name)
            if not markers:
                markers.append({"name":start.name, "point":points[0]})
            markers.append({"name":end.name, "point":points[-1]})
            for step in path["steps"]:
                step_distance = float(step.get("step_distance", step.get("distance", 0)))
                if not math.isfinite(step_distance) or step_distance < 0:
                    raise r.RouteError("道路步骤里程无效。")
                steps.append({"name":step.get("road_name") or step.get("name") or "未命名道路",
                              "distance_m":step_distance,
                              "points":[list(r.lon_lat(p)) for p in step[client.track_field].split(";")]})
            return points, distance

    segments, meters = r.plan_track(Collector(), stops, diagnostics)
    warnings = list(args.note)
    warnings.append("绿道按接口道路名称标注，不代表已核实独立骑行道或现场通行条件。标记位于规划的道路端点。")
    if len(segments) > 1:
        warnings.append(f"有 {len(segments)-1} 处路段未衔接；图示和导出均保留分段。")
    for leg in diagnostics:
        if max(leg.get("start_offset_m",0),leg.get("end_offset_m",0)) > 100:
            warnings.append(f"第 {leg['leg']} 段道路端点偏移超过 100 米，请核对入口。")
    data = {"schema":"cycling-route-preview/v1", "coordinate_system":"gcj02",
            "name":f"{stops[0].name} → {stops[-1].name}", "provider":client.name,
            "distance_m":meters, "segments":segments, "steps":steps, "markers":markers,
            "stops":[vars(s) for s in stops], "legs":diagnostics, "warnings":warnings}
    body = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode()
    fingerprint = digest(body)
    page_body = html_bytes(data, fingerprint, r)
    r.save_file(cache, body)
    try:
        r.save_file(page, page_body)
    except Exception:
        cache.unlink()
        raise
    return {"mode":"preview", "provider":client.name, "name":data["name"],
            "preview":str(cache.resolve()), "map":str(page.resolve()), "sha256":fingerprint,
            "distance_km":meters/1000, "track_points":sum(map(len,segments)), "warnings":warnings}


def render(args, r):
    if args.output.suffix.lower() != ".html":
        raise r.RouteError("路线图 --output 请指定 .html 文件。")
    data, fingerprint = load_cache(args.preview, r)
    r.save_file(args.output, html_bytes(data, fingerprint, r))
    return {"mode":"preview", "map":str(args.output.resolve()), "preview":str(args.preview.resolve()),
            "sha256":fingerprint, "warnings":data["warnings"]}


def export(args, r):
    data, fingerprint = load_cache(args.preview, r, args.confirmed_sha256)
    description = f"{data['provider']}自行车规划；用户确认的预览轨迹；GCJ-02 近似转 WGS84。"
    body = r.gpx_bytes(data["name"], description, data["segments"])
    path = r.save_output(args.output, data["name"], body)
    return {"mode":"planned", "provider":data["provider"], "name":data["name"],
            "file":str(path.resolve()), "distance_km":data["distance_m"]/1000,
            "track_points":sum(map(len,data["segments"])), "preview_sha256":fingerprint,
            "warnings":data["warnings"]}
