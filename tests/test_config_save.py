"""Saving the config must never leave a half-written file behind."""
import json, os, stat, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentsmon import config


class AtomicSave(unittest.TestCase):
    def test_failed_write_keeps_the_previous_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            config.save({"agents": [{"name": "old"}]}, path)
            real = Path.write_text

            def crash_midway(self, data, *a, **kw):
                real(self, data[:10], *a, **kw)   # a torn write, then the process dies
                raise OSError("disk full")
            with mock.patch.object(Path, "write_text", crash_midway):
                with self.assertRaises(OSError):
                    config.save({"agents": [{"name": "new"}]}, path)
            self.assertEqual(json.loads(path.read_text("utf-8"))["agents"][0]["name"], "old")

    def test_saved_file_is_private_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            config.save({"agents": []}, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(os.listdir(d), ["config.json"])


if __name__ == "__main__":
    unittest.main()
