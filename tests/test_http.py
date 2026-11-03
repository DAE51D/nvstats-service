import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service"))
sys.path.insert(0, os.path.dirname(__file__))

import nvstats_httpd  # noqa: E402
from test_core_views import SAMPLE  # noqa: E402


class FakeSampler:
    def latest(self):
        return SAMPLE

    def wait_newer(self, seq, timeout):
        return SAMPLE if SAMPLE["seq"] > seq else None


class HttpTests(unittest.TestCase):
    def start(self, token=None):
        cfg = nvstats_httpd.HttpConfig("127.0.0.1", 0, token, True)
        server = nvstats_httpd.serve(FakeSampler(), cfg)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_address[1]

    def get(self, url, token=None):
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        return urllib.request.urlopen(req, timeout=5)

    def test_health_needs_no_token(self):
        base = self.start(token="t")
        self.assertEqual(json.load(self.get(base + "/health"))["status"], "ok")

    def test_sample_requires_token_when_configured(self):
        base = self.start(token="t")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(base + "/api/v1/sample")
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(json.load(self.get(base + "/api/v1/sample", "t"))["seq"], 7)

    def test_minimal_then_full(self):
        base = self.start()
        mini = json.load(self.get(base + "/api/v1/sample"))
        full = json.load(self.get(base + "/api/v1/sample?full=1"))
        self.assertNotIn("llm", mini)
        self.assertIn("llm", full)
        self.assertNotIn("s3cret", json.dumps(full))

    def test_stream_first_event(self):
        base = self.start()
        resp = self.get(base + "/api/v1/stream")
        lines = [resp.readline().decode().rstrip("\n") for _ in range(3)]
        resp.close()
        self.assertEqual(lines[0], "id: 7")
        self.assertEqual(lines[1], "event: sample")
        self.assertEqual(json.loads(lines[2][len("data: "):])["seq"], 7)

    def test_unknown_path(self):
        base = self.start()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(base + "/nope")
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
