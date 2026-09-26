import tempfile
import unittest
from pathlib import Path

from igscrape_app.egress_config import describe, load_yaml_egresses


class EgressConfigTests(unittest.TestCase):
    def load(self, content, suffix=".yaml"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("egresses" + suffix)
            path.write_text(content, encoding="utf-8")
            return load_yaml_egresses(path)

    def test_example_expands_isolated_tor_lanes(self):
        path = Path(__file__).resolve().parents[1] / "workspace-egresses.example.yaml"
        specs = load_yaml_egresses(path)
        self.assertEqual(len(specs), 36)
        self.assertEqual(len({s.name for s in specs}), 36)
        self.assertTrue(all(s.tor and s.url.startswith("socks5://") for s in specs))
        self.assertTrue(all(s.start_rpm == 10 and s.max_rpm == 15 for s in specs))

    def test_proxy_host_keeps_socks_scheme(self):
        specs = self.load("egresses:\n  - type: socks5\n    host: localhost\n    port: 1080\n")
        self.assertEqual(specs[0].url, "socks5://localhost:1080")

    def test_malformed_structures_fail_closed(self):
        for content in ("egresses: direct\n", "other: []\n", "- direct\n- 123\n",
                        "egresses:\n  - typo\n",
                        "egresses:\n  - type: proxy\n    host: localhost\n",
                        "egresses:\n  - type: tor\n    circuits: 0\n",
                        "egresses:\n  - tor:127.0.0.1:9152:999999\n"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.load(content)

    def test_duplicate_cooldown_identity_is_rejected(self):
        for content in (
            "egresses:\n  - type: tor\n    circuits: 2\n  - type: tor\n    circuits: 2\n",
            "egresses:\n  - name: same\n    type: proxy\n    url: http://one:8000\n  - name: same\n    type: proxy\n    url: http://two:8000\n",
        ):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "duplicate identity"):
                self.load(content)

    def test_errors_and_display_hide_proxy_credentials(self):
        secret = "topsecret"
        specs = self.load(f"egresses:\n  - name: proxy-one\n    type: proxy\n    url: http://alice:{secret}@example.test:8080\n")
        self.assertNotIn(secret, str(describe(specs)))
        with self.assertRaises(ValueError) as error:
            self.load(f"egresses:\n  - type: proxy\n    url: http://alice:{secret}@example.test:badport\n")
        self.assertNotIn(secret, str(error.exception))


if __name__ == "__main__":
    unittest.main()
