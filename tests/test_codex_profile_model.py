"""Agents sharing one Codex default config show the model of their own --profile."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentsmon import detect


class ProfileModel(unittest.TestCase):
    def _home(self, files):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for name, text in files.items():
            Path(d.name, name).write_text(text, encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": d.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_profile_file_names_the_model(self):
        self._home({"config.toml": 'model = "gpt-5.5"\n', "big.config.toml": 'model = "gpt-6-astra"\n'})
        self.assertEqual(detect._codex_profile_model(["/usr/bin/codex --profile big --yolo"]), "GPT-6 Astra")
        self.assertEqual(detect._codex_profile_model(["codex -p big"]), "GPT-6 Astra")
        self.assertEqual(detect._codex_profile_model(["codex --profile=big"]), "GPT-6 Astra")

    def test_profiles_table_in_config_names_the_model(self):
        self._home({"config.toml": 'model = "gpt-5.5"\n[profiles.fast]\nmodel = "gpt-5.6-sol"\n[profiles.other]\nmodel = "x"\n'})
        self.assertEqual(detect._codex_profile_model(["codex --profile fast"]), "GPT-5.6 Sol")

    def test_no_profile_or_unknown_profile_gives_nothing(self):
        self._home({"config.toml": 'model = "gpt-5.5"\n'})
        self.assertIsNone(detect._codex_profile_model(["codex --yolo"]))
        self.assertIsNone(detect._codex_profile_model(["codex --profile nope"]))
        self.assertIsNone(detect._codex_profile_model(["claude --profile big"]))

    def test_discovered_agent_label_follows_the_profile(self):
        self._home({"config.toml": 'model = "gpt-5.5"\n', "big.config.toml": 'model = "gpt-6-astra"\n'})
        cmd = "/usr/local/bin/codex --profile big"
        with mock.patch.object(detect, "_proc_table", return_value=({1: cmd}, {})), \
             mock.patch.object(detect, "tmux_sessions", return_value=[{"name": "s", "created": None}]), \
             mock.patch.object(detect, "_pane_pids", return_value=[1]), \
             mock.patch.object(detect, "_session_cwd", return_value=None), \
             mock.patch.object(detect, "_proc_age", return_value=None), \
             mock.patch.object(detect, "_run", return_value=None), \
             mock.patch.object(detect, "_codex_model", return_value="GPT-5.5"):
            agents = detect.discover_agents()
        self.assertEqual([(a["kind"], a["label"]) for a in agents], [("codex", "GPT-6 Astra")])


if __name__ == "__main__":
    unittest.main()
