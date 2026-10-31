"""Topics of an a2tt bridge appear read-only under the row of their master agent."""
import json, os, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import bridges, dashboard, detect

LANE = {'chat': -100123, 'thread': 7, 'title': 'docs', 'agent': 'claude', 'cwd': '/secret/place',
        'tmux_session': 'T 7', 'alive': True, 'status': 'live', 'busy': True,
        'last_activity': None, 'last_compact': None, 'ctx_tokens': 50000, 'ctx_window': 200000}


def bridge(root, master='Boss', **extra):
    instance = Path(root)/'bots'/'Bot'
    (instance/'state').mkdir(parents=True)
    return {'name': 'Telegram Bridge: Bot (topics)', 'kind': 'a2tt', 'master': master,
            'bridge_config': str(instance/'config.json'), **extra}


def write_status(sp, lanes, age=0):
    now = int(time.time())
    bridges.status_file(sp).write_text(json.dumps(
        {'schema': 1, 'updated_at': now-age, 'heartbeat_seconds': 15, 'lanes': lanes}))


def agents_for(cfg):
    found = [{'name': 'Boss', 'kind': 'claude-code', 'label': 'M', 'session_id': None, 'vendor': None,
              'alive': True, 'age': 1, 'resume_cmd': None, 'pids': [1]}]
    with patch.object(detect, 'discover_agents', return_value=found), patch.object(detect, 'telegram_links', return_value={}):
        return {a['name']: a for a in dashboard._agents_state(cfg)}


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sp = bridge(self.tmp.name)
        self.cfg = {'agents': [{'name': 'Boss'}], 'pinned_daemons': [], 'daemons': [self.sp]}

    def test_topics_are_attached_to_the_master_with_display_fields_only(self):
        write_status(self.sp, [{**LANE, 'last_activity': int(time.time())-120}])
        boss = agents_for(self.cfg)['Boss']
        t = boss['topics'][0]
        self.assertFalse(boss['topics_stale'])
        self.assertEqual((t['thread'], t['title'], t['agent'], t['status'], t['alive'], t['busy']),
                         (7, 'docs', 'claude', 'live', True, True))
        self.assertIn(t['last_activity_age'], (120, 121))
        self.assertIsNone(t['last_compact_age'])
        self.assertEqual((t['ctx_tokens'], t['ctx_percent']), (50000, 25.0))
        shown = json.dumps(boss['topics'])
        for private in ('secret', '-100123', 'T 7'):
            self.assertNotIn(private, shown)

    def test_percent_needs_both_context_values(self):
        write_status(self.sp, [{**LANE, 'ctx_window': None}, {**LANE, 'ctx_tokens': None}])
        self.assertEqual([t['ctx_percent'] for t in agents_for(self.cfg)['Boss']['topics']], [None, None])

    def test_hung_bridge_marks_topics_stale(self):
        write_status(self.sp, [LANE], age=500)
        self.assertTrue(agents_for(self.cfg)['Boss']['topics_stale'])

    def test_missing_status_file_gives_no_topics_and_no_error(self):
        self.assertNotIn('topics', agents_for(self.cfg)['Boss'])

    def test_unknown_master_renders_nothing_and_the_card_says_so(self):
        sp = bridge(self.tmp.name + '/x', master='Ghost')
        write_status(sp, [LANE])
        cfg = {'agents': [{'name': 'Boss'}], 'pinned_daemons': [], 'daemons': [sp]}
        self.assertNotIn('topics', agents_for(cfg)['Boss'])
        with patch.object(bridges, 'processes', return_value=[(1, {'config': sp['bridge_config'], 'kind': 'a2tt'})]):
            up, detail = bridges.health(cfg)
        self.assertTrue(up)
        self.assertIn('master Ghost not found', detail)

    def test_disabled_bridge_and_bridge_without_master_attach_nothing(self):
        write_status(self.sp, [LANE])
        self.assertNotIn('topics', agents_for({**self.cfg, 'daemons': [{**self.sp, 'enabled': False}]})['Boss'])
        no_master = {k: v for k, v in self.sp.items() if k != 'master'}
        self.assertNotIn('topics', agents_for({**self.cfg, 'daemons': [no_master]})['Boss'])


class Page(unittest.TestCase):
    def test_page_renders_topic_rows_without_actions(self):
        page = dashboard.PAGE
        self.assertIn('renderTopic', page)
        body = page[page.index('function renderTopic'):]
        body = body[:body.index('// Collapsible blocks')]
        self.assertNotIn('<button', body)
        self.assertNotIn('data-act', body)


if __name__ == '__main__':
    unittest.main()
