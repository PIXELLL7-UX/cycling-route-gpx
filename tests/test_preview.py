import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import route_gpx as r
import route_preview as p
import credentials as c


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.cache = self.folder / "preview.json"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def preview(self, provider="amap", name="A", path=None):
        field = "polyline" if provider == "amap" else "path"
        path = path or {"distance":1000, "steps":[{"road_name":"测试绿道", "distance":1000,
                    "step_distance":1000, field:"116.3,39.9;116.31,39.9"}]}
        cls = r.Amap if provider == "amap" else r.Baidu
        args = r.arguments([provider+"-preview", "--point",name+"|116.3,39.9",
                            "--point","B|116.31,39.9","--coordinates","gcj02","--output",str(self.cache)])
        with patch.object(r,"map_key",return_value="synthetic-secret"),patch.object(cls,"bike_path",return_value=path):
            return r.run(args)

    def test_both_providers_preview_without_gpx_and_without_secret(self):
        for provider in ("amap","baidu"):
            with self.subTest(provider=provider):
                self.cache=self.folder/(provider+".json")
                result=self.preview(provider)
                self.assertEqual(result["mode"],"preview")
                self.assertTrue(self.cache.with_suffix(".html").exists())
                self.assertFalse(list(self.folder.glob("*.gpx")))
                for f in (self.cache,self.cache.with_suffix(".html")):
                    self.assertNotIn("synthetic-secret",f.read_text())

    def test_export_uses_exact_cache_without_network_or_key(self):
        result=self.preview()
        output=self.folder/"route.gpx"
        args=r.arguments(["export-preview",str(self.cache),"--confirmed-sha256",result["sha256"],"--output",str(output)])
        with patch.object(r,"map_key",side_effect=AssertionError("key requested")),patch.object(r,"fetch",side_effect=AssertionError("network requested")):
            exported=r.run(args)
        self.assertEqual(exported["preview_sha256"],result["sha256"])
        data=json.loads(self.cache.read_text())
        points=ET.fromstring(output.read_bytes()).findall(f".//{{{r.GPX}}}trkpt")
        for actual,original in zip(points,data["segments"][0],strict=True):
            expected=r.gcj02_to_wgs84(*original)
            self.assertAlmostEqual(float(actual.attrib["lon"]),expected[0],places=7)
            self.assertAlmostEqual(float(actual.attrib["lat"]),expected[1],places=7)

    def test_export_rejects_changed_cache(self):
        result=self.preview()
        data=json.loads(self.cache.read_text());data["segments"][0][-1][0]+=.1
        self.cache.write_text(json.dumps(data))
        output=self.folder/"route.gpx"
        with self.assertRaisesRegex(r.RouteError,"重新展示"):
            r.run(r.arguments(["export-preview",str(self.cache),"--confirmed-sha256",result["sha256"],"--output",str(output)]))
        self.assertFalse(output.exists())

    def test_export_requires_confirmation_identifier(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            r.arguments(["export-preview",str(self.cache)])

    def test_render_is_offline_and_keeps_cache_bytes(self):
        result=self.preview();original=self.cache.read_bytes()
        with patch.object(r,"fetch",side_effect=AssertionError("network")),patch.object(r,"map_key",side_effect=AssertionError("key")):
            rendered=r.run(r.arguments(["render-preview",str(self.cache),"--output",str(self.folder/"again.html")]))
        self.assertEqual(rendered["sha256"],result["sha256"])
        self.assertEqual(self.cache.read_bytes(),original)

    def test_html_escapes_route_names_and_script_termination(self):
        self.preview(name='</script><img src=x onerror=alert(1)>__DATA__')
        page=self.cache.with_suffix('.html').read_text()
        self.assertNotIn('<img src=x',page)
        self.assertIn('\\u003c/script\\u003e',page)
        self.assertIn('&lt;/script&gt;',page)

    def test_preview_refuses_overwrite_before_network(self):
        self.preview()
        with patch.object(r,"map_key",side_effect=AssertionError("key")),self.assertRaises(r.RouteError):
            r.run(r.arguments(["amap-preview","--point","A","--point","B","--output",str(self.cache)]))

    def test_disconnected_steps_never_write_preview_or_gpx(self):
        path={"distance":20000,"steps":[{"polyline":"116.3,39.9;116.31,39.9"},
                                         {"polyline":"116.5,39.9;116.51,39.9"}]}
        with self.assertRaisesRegex(r.RouteError,"断点"):
            self.preview(path=path)
        self.assertEqual(list(self.folder.iterdir()),[])

    def test_disconnected_baidu_steps_stop(self):
        path={"distance":20000,"steps":[{"path":"116.3,39.9;116.31,39.9"},
                                         {"path":"116.5,39.9;116.51,39.9"}]}
        with self.assertRaisesRegex(r.RouteError,"断点"):
            self.preview("baidu",path=path)

    def test_sub_two_meter_step_rounding_keeps_geometry(self):
        points,meters=r.route_points({"distance":2000,"steps":[
            {"polyline":"116.3,39.9;116.31,39.9"},
            {"polyline":"116.310001,39.9;116.32,39.9"}]},"polyline","高德")
        self.assertEqual(len(points),4)
        self.assertEqual(meters,2000)

    def test_invalid_cache_coordinates_are_rejected(self):
        result=self.preview();data=json.loads(self.cache.read_text())
        data['segments'][0][0]=[999,39]
        self.cache.write_text(json.dumps(data))
        with self.assertRaises(r.RouteError):
            p.load_cache(self.cache,r)

    def test_incomplete_cache_never_writes_partial_export(self):
        self.preview();data=json.loads(self.cache.read_text())
        del data['warnings'];self.cache.write_text(json.dumps(data))
        output=self.folder/'bad.gpx'
        with self.assertRaises(r.RouteError):
            r.run(r.arguments(['export-preview',str(self.cache),'--confirmed-sha256',
                               p.digest(self.cache.read_bytes()),'--output',str(output)]))
        self.assertFalse(output.exists())


class CredentialTests(unittest.TestCase):
    def test_locked_backend_is_not_missing(self):
        backend=Mock();backend.get_password.side_effect=RuntimeError("private backend detail")
        with patch.object(c,'_keyring',return_value=backend):
            with self.assertRaisesRegex(c.CredentialError,"解锁") as error:c.read_other_platform('amap')
        self.assertNotIn('private backend detail',str(error.exception))

    def test_no_record_is_missing(self):
        backend=Mock();backend.get_password.return_value=None
        with patch.object(c,'_keyring',return_value=backend):self.assertEqual(c.read_other_platform('amap'),'')

    def test_backend_setup_error_preserved(self):
        with patch.object(c,'_keyring',side_effect=c.CredentialError('未检测到受支持的系统凭证库')):
            with self.assertRaisesRegex(c.CredentialError,'未检测到'):c.read_other_platform('baidu')

    def test_check_key_does_not_leak_key(self):
        with patch.object(r,'map_key',return_value='synthetic-secret'):
            result=r.run(r.arguments(['check-key','amap']))
        self.assertTrue(result['readable'])
        self.assertNotIn('synthetic-secret',json.dumps(result))


if __name__ == '__main__':unittest.main()
