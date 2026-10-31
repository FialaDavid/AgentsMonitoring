"""A hung tmux command must not abort the keepalive pass for the agents queued behind it."""
import os, subprocess, tempfile, unittest
from unittest import mock

os.environ.setdefault("AGENTSMON_STATE", tempfile.mkdtemp(prefix="agentsmon-test-state-"))
from agentsmon import keepalive  # noqa: E402


class HungCommands(unittest.TestCase):
    def test_a_timeout_on_one_agent_does_not_skip_the_next(self):
        cfg = {"tmux_bin": "tmux", "daemons": [],
               "agents": [{"name": "first", "match": "x", "restart": "echo a"},
                          {"name": "second", "match": "y", "restart": "echo b"}]}
        started = []

        def fake_run(cmd, **kwargs):
            if cmd[1] == "new-session":
                if cmd[4] == "first":
                    raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
                started.append(cmd[4])
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(keepalive.detect, "tmux_sessions", return_value=[]), \
             mock.patch.object(keepalive.detect, "_proc_table", return_value=({}, {})), \
             mock.patch.object(keepalive.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(keepalive.time, "sleep"):
            keepalive.tick(cfg)
        self.assertEqual(started, ["second"])

    def test_missing_tmux_binary_does_not_raise(self):
        with mock.patch.object(keepalive.detect, "tmux_sessions", return_value=[]), \
             mock.patch.object(keepalive.subprocess, "run", side_effect=FileNotFoundError("tmux")), \
             mock.patch.object(keepalive.time, "sleep"):
            keepalive._start({"name": "a", "restart": "echo"}, "tmux")


if __name__ == "__main__":
    unittest.main()
