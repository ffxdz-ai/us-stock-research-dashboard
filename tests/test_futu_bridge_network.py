import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from futu_cloud_bridge import request_bridge


class BridgeNetworkTests(unittest.TestCase):
    def test_long_running_uploader_refreshes_proxy_settings_for_each_request(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
        with patch("futu_cloud_bridge.bridge_endpoint", return_value="https://worker.example/futu/snapshot"), \
             patch("futu_cloud_bridge.bridge_headers", return_value={}), \
             patch("urllib.request.getproxies", side_effect=[{"https": "http://127.0.0.1:8001"}, {"https": "http://127.0.0.1:8002"}]), \
             patch("urllib.request.build_opener", return_value=opener) as build:
            self.assertTrue(request_bridge("GET")["ok"])
            self.assertTrue(request_bridge("GET")["ok"])
        self.assertEqual(build.call_args_list[0].args[0].proxies["https"], "http://127.0.0.1:8001")
        self.assertEqual(build.call_args_list[1].args[0].proxies["https"], "http://127.0.0.1:8002")


if __name__ == "__main__":
    unittest.main()
