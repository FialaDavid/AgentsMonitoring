import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import bridges, dashboard


class BridgeRows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.status = Path(self.tmp.name) / 'status.json'
        self.spec = {'name': 'Bridge', 'kind': 'a2tt', 'bridge_config': '/bots/Bridge/config.json',
                     'status_file': str(self.status), 'restart': 'echo restart', 'enabled': True}
        self.cfg = {'agents': [], 'pinned_daemons': [], 'daemons': [self.spec]}

    def rows(self, state='running', master=None, label='GPT-6.1 Sol High', vendor='openai'):
        self.spec.update(label=label, vendor=vendor)
        if master:
            self.spec['master'] = master
        with patch.object(dashboard.detect, 'configured_agents', return_value=[]), \
             patch.object(dashboard.detect, 'pinned_agents', return_value=[]), \
             patch.object(dashboard.detect, 'telegram_links', return_value={}), \
             patch.object(bridges, 'states', return_value=[(self.spec, state, 'heartbeat late' if state == 'stale' else '')]), \
             patch.object(bridges, 'topics', return_value={'items': [{'thread': 9, 'close_ref': 'ref',
                 '_tmux_session': None, 'alive': False}], 'stale': state == 'stale', 'tmux_tmpdir': '/tmp'}):
            return dashboard._agents_state(self.cfg)

    def test_masterless_bridge_row_carries_badge_topics_and_actions(self):
        row = self.rows()[0]
        self.assertEqual((row['name'], row['label'], row['vendor']), ('Bridge', 'GPT-6.1 Sol High', 'openai'))
        self.assertEqual(row['status'], 'Running')
        self.assertTrue(row['alive'])
        self.assertEqual(row['topics'][0]['close_ref'], 'ref')
        self.assertTrue(row['bridge_row'])

    def test_unlabeled_row_uses_default_badge(self):
        row = self.rows(label=None, vendor=None)[0]
        self.assertEqual((row['label'], row['vendor']), ('—', 'other'))

    def test_process_states_map_to_row_state(self):
        for state, expected, alive in [('running', 'Running', True), ('stale', 'Stale', False), ('down', 'Down', False)]:
            with self.subTest(state=state):
                row = self.rows(state)[0]
                self.assertEqual((row['status'], row['alive']), (expected, alive))

    def test_existing_master_keeps_topics_under_master(self):
        self.spec['master'] = 'Agent'
        with patch.object(dashboard.detect, 'configured_agents', return_value=[{'name': 'Agent', 'kind': 'codex', 'alive': True}]), \
             patch.object(dashboard.detect, 'pinned_agents', return_value=[]), \
             patch.object(dashboard.detect, 'telegram_links', return_value={}), \
             patch.object(bridges, 'states', return_value=[(self.spec, 'running', '')]), \
             patch.object(bridges, 'topics', return_value={'items': [{'thread': 9, 'close_ref': 'ref', '_tmux_session': None}], 'stale': False}):
            rows = dashboard._agents_state(self.cfg)
        self.assertEqual([r['name'] for r in rows], ['Agent'])
        self.assertEqual(rows[0]['topics'][0]['close_ref'], 'ref')

    def test_missing_master_falls_back_to_bridge_row(self):
        rows = self.rows(master='Missing')
        self.assertEqual([r['name'] for r in rows], ['Bridge'])
        self.assertEqual(rows[0]['topics'][0]['close_ref'], 'ref')

    def test_restart_uses_managed_daemon_action_and_stop_is_rejected(self):
        from agentsmon import managed
        completed = type('Completed', (), {'returncode': 0})()
        with patch.object(dashboard.config, 'load', return_value=self.cfg), \
             patch.object(dashboard, '_set_enabled'), \
             patch.object(managed, 'discover', return_value=None), \
             patch.object(managed, 'process_ids', return_value=[]), \
             patch.object(managed.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(dashboard._agent_action('Bridge', 'restart'), (True, 'Bridge: restart command exited 0.'))
            run.assert_called_once_with('echo restart', shell=True, capture_output=True, timeout=30)
        with patch.object(dashboard.config, 'load', return_value=self.cfg):
            self.assertFalse(dashboard._agent_action('Bridge', 'stop')[0])

    def test_bridge_with_configured_master_keeps_daemon_action_path(self):
        from agentsmon import managed
        self.spec['master'] = 'Agent'
        self.cfg['agents'] = [{'name': 'Agent'}]
        with patch.object(dashboard.config, 'load', return_value=self.cfg), \
             patch.object(dashboard, '_set_enabled'), \
             patch.object(managed, 'discover', return_value=None), \
             patch.object(managed, 'action', return_value=(True, 'stopped')) as action:
            self.assertEqual(dashboard._agent_action('Bridge', 'stop'), (True, 'stopped'))
            action.assert_called_once_with(self.spec, 'stop')


if __name__ == '__main__':
    unittest.main()
