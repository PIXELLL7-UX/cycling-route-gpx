"""Offline regression tests: python3 -B -m unittest discover -s tests -v."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, parse_qs, urlsplit
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import route_gpx as r


def amap_url(**overrides):
    params = dict(t="3", rideType="bike", dev="0", slon="116.3", slat="39.9",
                  dlon="116.3", dlat="39.9", sname="A", dname="A", vian="2",
                  vialons="116.4|116.5", vialats="39.91|39.92", vianames="B|C")
    params.update(overrides)
    return "https://www.amap.com/share?" + urlencode({
        "schema": "amapuri://route/plan/?" + urlencode(params),
        "share_bizParams": json.dumps({"naviDistance": "8000"}),
    })


def baidu_url(**overrides):
    # Fictional points in the observed cycle/sn/en wire format; no user location.
    params = dict(c="131", sn="1$$$$12950000,4800000$$测试起点$$0$$$$",
                  en="1$$$$12952000,4802000$$测试终点$$0$$$$")
    params.update(overrides)
    return "https://map.baidu.com/?" + urlencode({"newmap": "1", "s": "cycle&" + urlencode(params)})


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(r.shutil, "which", return_value="/usr/bin/curl"))
        self.stack.enter_context(patch.object(r.time, "sleep"))
        self.stack.enter_context(patch.object(r, "REQUEST_RETRIES", 2))
        self.stderr = self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def fake_curl(self, responses):
        sequence = iter(responses)

        def run(command, **kwargs):
            code, status, body = next(sequence)
            if code == "timeout":
                raise subprocess.TimeoutExpired(command, 30)
            Path(command[command.index("--output") + 1]).write_bytes(body)
            return subprocess.CompletedProcess(command, code, f"{status}\n\n", "private error text")
        return self.stack.enter_context(patch.object(r.subprocess, "run", side_effect=run))

    def test_timeout_then_success_does_not_keep_partial_body(self):
        run = self.fake_curl([(28, 0, b"partial"), (0, 200, b"complete")])
        self.assertEqual(r.fetch("https://api.map.baidu.com/test")[2], b"complete")
        self.assertEqual(run.call_count, 2)

    def test_timeout_has_bounded_attempts_and_readable_error(self):
        run = self.fake_curl([("timeout", 0, b"")] * 3)
        with self.assertRaisesRegex(r.RouteError, "超时.*已尝试 3 次"):
            r.fetch("https://restapi.amap.com/test")
        self.assertEqual(run.call_count, 3)

    def test_server_error_retries_but_forbidden_does_not(self):
        run = self.fake_curl([(0, 503, b"bad"), (0, 200, b"ok"), (0, 403, b"denied")])
        self.assertEqual(r.fetch("https://restapi.amap.com/test")[0], 200)
        self.assertEqual(r.fetch("https://restapi.amap.com/test")[0], 403)
        self.assertEqual(run.call_count, 3)

    def test_certificate_error_not_retried(self):
        run = self.fake_curl([(60, 0, b"")])
        with self.assertRaisesRegex(r.RouteError, "证书"):
            r.fetch("https://restapi.amap.com/test")
        self.assertEqual(run.call_count, 1)

    def test_key_never_appears_in_command_or_error(self):
        run = self.fake_curl([(6, 0, b"")] * 3)
        with self.assertRaises(r.RouteError) as error:
            r.fetch("https://api.map.baidu.com/test?ak=synthetic-secret")
        self.assertNotIn("synthetic-secret", str(error.exception) + self.stderr.getvalue())
        self.assertNotIn("synthetic-secret", " ".join(run.call_args.args[0]))
        self.assertIn("synthetic-secret", run.call_args.kwargs["input"])

    def test_missing_curl(self):
        with patch.object(r.shutil, "which", return_value=None):
            with self.assertRaisesRegex(r.RouteError, "缺少 curl"):
                r.fetch("https://restapi.amap.com/test")


class ShareTests(unittest.TestCase):
    def test_amap_preserves_closed_loop_and_order(self):
        stops, distance = r.parse_share(amap_url())
        self.assertEqual([s.name for s in stops], ["A", "B", "C", "A"])
        self.assertEqual(stops[0].point, stops[-1].point)
        self.assertEqual(distance, 8000)

    def test_amap_rejects_waypoint_count_mismatch(self):
        for overrides in (dict(vian="0"), dict(vian="3"), dict(vian="-1")):
            with self.subTest(overrides=overrides), self.assertRaises(r.RouteError):
                r.parse_share(amap_url(**overrides))

    def test_amap_rejects_other_transport(self):
        for changes in (dict(t="0"), dict(rideType="ebike")):
            with self.subTest(changes=changes), self.assertRaisesRegex(r.RouteError, "自行车"):
                r.parse_share(amap_url(**changes))

    def test_share_text_and_relative_redirect(self):
        target = amap_url().replace("https://www.amap.com", "")
        with patch.object(r, "fetch", return_value=(302, target, b"")) as fetch:
            stops, _ = r.parse_share("我分享了骑行路线 https://www.amap.com/short 。")
        self.assertEqual(len(stops), 4)
        self.assertEqual(fetch.call_count, 1)

    def test_redirect_rejects_foreign_host_before_fetch(self):
        with patch.object(r, "fetch", return_value=(302, "https://example.org/", b"")) as fetch:
            with self.assertRaises(r.RouteError):
                r.parse_baidu_share("https://j.map.baidu.com/test")
        self.assertEqual(fetch.call_count, 1)

    def test_baidu_real_format_without_private_location(self):
        with patch.object(r, "fetch", return_value=(302, baidu_url(), b"")):
            stops, _ = r.parse_baidu_share("https://j.map.baidu.com/test")
        self.assertEqual([s.name for s in stops], ["测试起点", "测试终点"])
        self.assertEqual(stops[0].point, (12950000, 4800000))
        self.assertEqual(stops[0].coordinate_system, "bd09mc")

    def test_baidu_unknown_waypoints_never_silently_disappear(self):
        with self.assertRaisesRegex(r.RouteError, "途经点"):
            r.parse_baidu_share(baidu_url(vps="unknown-encoded-waypoints"))

    def test_baidu_uri_preserves_repeated_via_points(self):
        uri = "baidumap://map/bikenavi?" + urlencode({
            "origin": "39.9,116.3", "destination": "39.9,116.3", "coord_type": "gcj02",
            "viaPoints": json.dumps({"viaPoints": [
                {"name": "B", "lng": 116.4, "lat": 39.91},
                {"name": "B", "lng": 116.4, "lat": 39.91}]}),
        })
        stops, _ = r.parse_baidu_share(uri)
        self.assertEqual([s.name for s in stops], ["起点", "B", "B", "终点"])

    def test_baidu_poi_page_is_not_a_route(self):
        with patch.object(r, "fetch", return_value=(200, "", b"<html>POI</html>")):
            with self.assertRaisesRegex(r.RouteError, "baidu-plan"):
                r.parse_baidu_share("https://map.baidu.com/poi")


class ProviderTests(unittest.TestCase):
    def test_baidu_requests_bicycle_gcj_track_and_lat_lon_order(self):
        client = r.Baidu("synthetic-secret")
        start = r.Stop("A", 12950000, 4800000, "bd09mc")
        end = r.Stop("B", 12952000, 4802000, "bd09mc")
        response = {"result": {"routes": [{"distance": 3000, "steps": [
            {"path": "116.3,39.9;116.4,39.91"}]}]}}
        with patch.object(client, "get", return_value=response) as get:
            points, length = client.bike_leg(start, end)
        self.assertEqual(get.call_args.args[0], "/directionlite/v1/riding")
        self.assertEqual(get.call_args.kwargs["origin"], "4800000.000000,12950000.000000")
        self.assertEqual(get.call_args.kwargs["riding_type"], 0)
        self.assertEqual(get.call_args.kwargs["coord_type"], "bd09mc")
        self.assertEqual(get.call_args.kwargs["ret_coordtype"], "gcj02")
        self.assertEqual(points[0], (116.3, 39.9))
        self.assertEqual(length, 3000)

    def test_amap_uses_bicycle_api(self):
        client = r.Amap("synthetic-secret")
        with patch.object(client, "get", return_value={"route": {"paths": [{"distance": 100,
                "steps": [{"polyline": "116.3,39.9;116.301,39.9"}]}]}}) as get:
            client.bike_leg(r.Stop("A",116.3,39.9),r.Stop("B",116.301,39.9))
        self.assertEqual(get.call_args.args[0], "/v5/direction/bicycling")

    def test_rate_limit_retries_but_amap_daily_quota_does_not(self):
        with patch.object(r.time,"sleep"), contextlib.redirect_stderr(io.StringIO()):
            with patch.object(r,"fetch",side_effect=[(200,"",b'{"status":401}'),(200,"",b'{"status":0,"result":{}}')]) as fetch:
                self.assertEqual(r.Baidu("key").get("/test")["status"],0)
                self.assertEqual(fetch.call_count,2)
            with patch.object(r,"fetch",return_value=(200,"",b'{"status":"0","infocode":"10003"}')) as fetch:
                with self.assertRaisesRegex(r.RouteError,"配额"):
                    r.Amap("key").get("/test")
                self.assertEqual(fetch.call_count,1)

    def test_baidu_search_returns_gcj_and_requires_city(self):
        client = r.Baidu("synthetic-secret")
        with self.assertRaisesRegex(r.RouteError, "--city"):
            client.search("公园", None)
        response = {"results": [{"name": "公园", "location": {"lng":116.3,"lat":39.9}, "city":"北京"}]}
        with patch.object(client, "get", return_value=response) as get:
            candidates = client.search("公园", "北京")
        self.assertEqual(get.call_args.kwargs["ret_coordtype"], "gcj02ll")
        self.assertEqual(candidates[0]["location"], "116.3,39.9")

    def test_baidu_permission_and_quota_errors_do_not_retry(self):
        for code, message in ((302,"配额"),(210,"IP"),(240,"权限")):
            with self.subTest(code=code), patch.object(r.time,"sleep"), patch.object(r,"fetch",return_value=(200,"",json.dumps({"status":code}).encode())) as fetch:
                with self.assertRaisesRegex(r.RouteError,message):
                    r.Baidu("key").get("/test")
                self.assertEqual(fetch.call_count,1)

    def test_non_json_response_is_readable(self):
        with patch.object(r.time,"sleep"), patch.object(r,"fetch",return_value=(200,"",b"<html>bad gateway</html>")):
            with self.assertRaisesRegex(r.RouteError,"无法解析"):
                r.Amap("key").get("/test")

    def test_ambiguous_poi_requires_selection(self):
        client = r.Amap("key")
        items=[{"name":"公园","location":loc,"address":"北京"} for loc in ("116.3,39.9","116.4,39.9")]
        with patch.object(client,"search",return_value=items):
            with self.assertRaisesRegex(r.RouteError,"请确认"):
                r.resolve_stop(client,"公园","北京",None)

    def test_noninteractive_key_uses_environment(self):
        with patch.dict(os.environ,{"BAIDU_MAP_AK":"synthetic-secret"}):
            self.assertEqual(r.map_key("baidu",False),"synthetic-secret")
        with patch.object(r.sys.stdin,"isatty",return_value=False):
            with self.assertRaisesRegex(r.RouteError,"BAIDU_MAP_AK"):
                r.map_key("baidu",True)

    def test_missing_credentials_guide_correct_provider_without_reusing_other_key(self):
        for provider, other_env, guide, env_name in (
            ("amap", "BAIDU_MAP_AK", r.AMAP_KEY_GUIDE, "AMAP_WEBSERVICE_KEY"),
            ("baidu", "AMAP_WEBSERVICE_KEY", r.BAIDU_KEY_GUIDE, "BAIDU_MAP_AK"),
        ):
            with self.subTest(provider=provider), \
                 patch.dict(os.environ, {other_env: "synthetic-other-secret"}, clear=True), \
                 patch.object(r.subprocess, "run", return_value=subprocess.CompletedProcess([], 44, "", "")), \
                 patch.object(r, "read_other_platform", return_value=""), \
                 patch.object(r.getpass, "getpass") as prompt, patch.object(r, "fetch") as fetch:
                with self.assertRaises(r.RouteError) as error:
                    r.map_key(provider, False)
                message = str(error.exception)
                self.assertIn(guide, message)
                self.assertIn(env_name, message)
                self.assertNotIn("synthetic-other-secret", message)
                prompt.assert_not_called()
                fetch.assert_not_called()

    def test_first_input_saved_and_next_use_reads_without_prompt(self):
        for provider in ("amap", "baidu"):
            with self.subTest(provider=provider), patch.object(r.sys, "platform", "darwin"), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(r.sys.stdin, "isatty", return_value=True), \
                 patch.object(r.getpass, "getpass", return_value="synthetic-secret") as prompt, \
                 patch.object(r, "save_key") as save, \
                 patch.object(r.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "synthetic-secret\n", "")) as read, \
                 contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(r.map_key(provider, True), "synthetic-secret")
                save.assert_called_once_with(provider, "synthetic-secret")
                self.assertEqual(r.map_key(provider, False), "synthetic-secret")
                self.assertEqual(prompt.call_count, 1)
                self.assertEqual(read.call_args.args[0][-1], provider)
                self.assertTrue(read.call_args.args[0][-2].endswith("credentials.py"))
                self.assertNotIn("synthetic-secret", stderr.getvalue())

    def test_save_failure_does_not_claim_remembered(self):
        with patch.object(r.sys.stdin, "isatty", return_value=True), \
             patch.object(r.getpass, "getpass", return_value="synthetic-secret"), \
             patch.object(r, "save_key", side_effect=r.CredentialError("凭证库锁定")), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(r.RouteError) as error:
                r.map_key("baidu", True)
            self.assertIn("未记住", str(error.exception))
            self.assertNotIn("synthetic-secret", str(error.exception) + stderr.getvalue())
            self.assertNotIn("已保存", stderr.getvalue())

    def test_environment_takes_priority_without_persisting_or_prompting(self):
        with patch.dict(os.environ, {"AMAP_WEBSERVICE_KEY": "synthetic-env"}), \
             patch.object(r, "save_key") as save, patch.object(r.getpass, "getpass") as prompt, \
             patch.object(r.subprocess, "run") as read:
            self.assertEqual(r.map_key("amap", False), "synthetic-env")
            save.assert_not_called()
            prompt.assert_not_called()
            read.assert_not_called()

    def test_non_macos_reads_system_credential_backend(self):
        with patch.object(r.sys, "platform", "linux"), patch.dict(os.environ, {}, clear=True), \
             patch.object(r, "read_other_platform", return_value="synthetic-stored") as read:
            self.assertEqual(r.map_key("baidu", False), "synthetic-stored")
            read.assert_called_once_with("baidu")

    def test_inaccessible_keychain_reports_access_error_not_missing_key(self):
        with patch.object(r.sys, "platform", "darwin"), patch.dict(os.environ, {}, clear=True), \
             patch.object(r.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "denied")), \
             patch.object(r.getpass, "getpass") as prompt:
            with self.assertRaisesRegex(r.RouteError, "无法读取.*无需重新申请"):
                r.map_key("amap", False)
            prompt.assert_not_called()


class ExportTests(unittest.TestCase):
    def test_standalone_place_search_commands_are_not_available(self):
        for provider in ("amap", "baidu"):
            with self.subTest(provider=provider), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    r.arguments([provider + "-search", "国家体育场", "--city", "北京市"])
                self.assertEqual(error.exception.code, 2)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.stack=contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def test_disconnected_legs_remain_separate_and_loop_is_preserved(self):
        a,b=r.Stop("A",116.3,39.9),r.Stop("B",116.4,39.9)
        client=r.Amap("key")
        legs=[]
        with patch.object(client,"bike_leg",side_effect=[([a.point,b.point],8000), ([(116.41,39.9),a.point],9000)]):
            segments,meters=r.plan_track(client,[a,b,a],legs)
        root=ET.fromstring(r.gpx_bytes("loop","",segments))
        self.assertEqual(len(root.findall(f"{{{r.GPX}}}trk/{{{r.GPX}}}trkseg")),2)
        self.assertEqual(meters,17000)
        self.assertEqual(len(legs),2)

    def test_join_deduplication_keeps_original_endpoint_diagnostics(self):
        a,b,c=[r.Stop(n,lon,39.9) for n,lon in (("A",116.3),("B",116.4),("C",116.5))]
        client=r.Amap("key")
        legs=[]
        with patch.object(client,"bike_leg",side_effect=[([a.point,b.point],8000),([b.point,c.point],8000)]):
            segments,_=r.plan_track(client,[a,b,c],legs)
        self.assertEqual(segments,[[a.point,b.point,c.point]])
        self.assertEqual(legs[1]["start_offset_m"],0)
        self.assertEqual(legs[1]["track_points"],2)

    def test_later_leg_failure_does_not_write_partial_gpx(self):
        output=self.directory/"failed.gpx"
        args=r.arguments(["amap-plan","--point","116.3,39.9","--point","116.4,39.9","--point","116.5,39.9","--coordinates","gcj02","--output",str(output)])
        with patch.object(r,"map_key",return_value="key"), patch.object(r.Amap,"bike_leg",side_effect=[([(116.3,39.9),(116.4,39.9)],8000),r.RouteError("限流")]):
            with self.assertRaisesRegex(r.RouteError,"第 2 段"):
                r.run(args)
        self.assertFalse(output.exists())

    def test_existing_output_never_overwritten(self):
        path=self.directory/"existing.gpx"
        path.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            r.save_file(path,b"replacement")
        self.assertEqual(path.read_bytes(),b"original")
        self.assertEqual(list(self.directory.iterdir()),[path])

    def test_baidu_share_to_gpx_uses_returned_road_coordinates(self):
        path=self.directory/"baidu.gpx"
        args=r.arguments(["baidu-share",baidu_url(),"--output",str(path)])
        payload={"result":{"routes":[{"distance":2300,"steps":[{"path":"116.3,39.9;116.31,39.91"}]}]}}
        with patch.object(r,"map_key",return_value="key"),patch.object(r.Baidu,"get",return_value=payload):
            result=r.run(args)
        self.assertEqual(result["provider"],"百度")
        self.assertEqual(result["stops"][0]["x"],12950000)
        self.assertNotIn("longitude",result["stops"][0])
        root=ET.fromstring(path.read_bytes())
        point=root.find(f"{{{r.GPX}}}trk/{{{r.GPX}}}trkseg/{{{r.GPX}}}trkpt")
        self.assertAlmostEqual(float(point.attrib["lon"]),r.gcj02_to_wgs84(116.3,39.9)[0],places=7)

    def test_baidu_incomplete_geometry_does_not_write_gpx(self):
        path=self.directory/"bad.gpx"
        args=r.arguments(["baidu-share",baidu_url(),"--output",str(path)])
        payload={"result":{"routes":[{"distance":2300,"steps":[{"path":"116.3,39.9;116.31,39.91"},{}]}]}}
        with patch.object(r,"map_key",return_value="key"),patch.object(r.Baidu,"get",return_value=payload):
            with self.assertRaisesRegex(r.RouteError,"缺少轨迹"):
                r.run(args)
        self.assertFalse(path.exists())

    def test_default_names_avoid_collisions(self):
        original=Path.cwd()
        try:
            os.chdir(self.directory)
            first=r.save_output(None,"route",b"one")
            second=r.save_output(None,"route",b"two")
            self.assertNotEqual(first,second)
            self.assertEqual(first.read_bytes(),b"one")
            self.assertEqual(second.read_bytes(),b"two")
        finally:
            os.chdir(original)

    def test_complete_result_reports_provider_stops_and_distance_difference(self):
        path=self.directory/"route.gpx"
        args=r.arguments(["amap-share",amap_url(),"--output",str(path)])
        with patch.object(r,"map_key",return_value="key"), patch.object(r.Amap,"bike_leg",return_value=([(116.3,39.9),(116.4,39.9)],4000)):
            result=r.run(args)
        self.assertEqual(result["provider"],"高德")
        self.assertEqual(result["mode"],"replanned")
        self.assertEqual(result["distance_difference_percent"],50)
        self.assertEqual([s["name"] for s in result["stops"]],["A","B","C","A"])
        ET.fromstring(path.read_bytes())

    def test_igpsport_error_categories(self):
        for status,body,hint in ((401,b"","登录"),(404,b"","未找到"),(403,b'{"code":5007}',"编号"),(200,b'{"code":5007}',"编号")):
            with self.subTest(status=status), patch.object(r,"fetch",return_value=(status,"",body)):
                with self.assertRaisesRegex(r.RouteError,hint):
                    r.download_igpsport("123","cn")

    def test_auto_source_recognition(self):
        for source,command in (("123","igpsport"),(amap_url(),"amap-share"),(baidu_url(),"baidu-share")):
            args=r.arguments(["auto",source])
            r.identify_source(args)
            self.assertEqual(args.command,command)

    def test_gpx_coordinates_are_converted_to_wgs84(self):
        root=ET.fromstring(r.gpx_bytes("route","",[[(116.4,39.9),(116.41,39.9)]]))
        point=root.find(f"{{{r.GPX}}}trk/{{{r.GPX}}}trkseg/{{{r.GPX}}}trkpt")
        self.assertAlmostEqual(float(point.attrib["lon"]),r.gcj02_to_wgs84(116.4,39.9)[0],places=7)


if __name__ == "__main__":
    unittest.main()
