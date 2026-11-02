import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from agentsmon import bridges, dashboard
from agentsmon.dashboard import _attach_command


class RowAttachTest(unittest.TestCase):
    def test_agent_row_has_quoted_attach_command(self):
        row = {'name': 'Scratch – agent · 2', 'kind': 'codex', 'alive': True}
        with patch.object(dashboard.detect, 'configured_agents', return_value=[row]), \
             patch.object(dashboard.detect, 'pinned_agents', return_value=[]), \
             patch.object(dashboard.detect, 'telegram_links', return_value={}), \
             patch.object(bridges, 'master_specs', return_value=[]), \
             patch.dict(os.environ, {'TMUX_TMPDIR': '/tmp'}):
            found = dashboard._agents_state({'agents': [], 'pinned_daemons': []})[0]
        expected_socket = str(Path('/tmp') / f'tmux-{os.getuid()}' / 'default')
        self.assertEqual(found['attach_command'], _attach_command(expected_socket, row['name']))

    def test_command_matches_exact_session_from_login_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket = str(Path(tmp) / 'tmux.sock')
            session = 'Scratch – topic 1 · 2'
            subprocess.run(['env', '-u', 'TMUX', 'tmux', '-S', socket,
                            'new-session', '-d', '-s', session], check=True)
            self.addCleanup(lambda: subprocess.run(['env', '-u', 'TMUX', 'tmux', '-S', socket,
                                                     'kill-server'], capture_output=True))
            command = _attach_command(socket, session)
            subprocess.run(['sh', '-lc', command.replace(' attach -t ', ' has-session -t ')], check=True)
            self.assertTrue(command.startswith('env -u TMUX tmux -S '))

    def test_topic_row_command_and_nonattachable_topics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'bot'
            root.mkdir()
            status = root / 'status.json'
            session = 'Scratch – topic 1 · 2'
            payload = {'tmux_tmpdir': str(Path(tmp) / 'tmux'), 'updated_at': 9999999999,
                       'heartbeat_seconds': 15, 'lanes': [
                           {'thread': 12, 'tmux_session': session, 'alive': True, 'status': 'live'},
                           {'thread': 13, 'tmux_session': None, 'alive': True, 'status': 'live'},
                           {'thread': 14, 'tmux_session': session, 'alive': False, 'status': 'live'}]}
            status.write_text(json.dumps(payload))
            spec = {'master': 'master', 'bridge_config': str(root / 'config.json'),
                    'status_file': str(status), 'kind': 'a2tt', 'enabled': True}
            cfg = {'agents': [], 'pinned_daemons': [], 'daemons': [spec]}
            with patch.object(dashboard.detect, 'configured_agents', return_value=[
                    {'name': 'master', 'kind': 'codex', 'alive': True}]), \
                 patch.object(dashboard.detect, 'pinned_agents', return_value=[]), \
                 patch.object(dashboard.detect, 'telegram_links', return_value={}):
                rows = dashboard._agents_state(cfg)[0]['topics']
            self.assertEqual(rows[0]['attach_command'], _attach_command(
                str(Path(tmp) / 'tmux' / f'tmux-{os.getuid()}' / 'default'), session))
            self.assertNotIn('attach_command', rows[1])
            self.assertNotIn('attach_command', rows[2])


if __name__ == '__main__':
    unittest.main()
