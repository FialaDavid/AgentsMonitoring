import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from agentsmon import usage_plugin as usage


class AccountUsage(unittest.TestCase):
    def test_accounts_never_share_by_vendor_missing_source_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'status.json'
            p.write_text(json.dumps({'rate_limits':{'five_hour':{'used_percentage':20,'resets_at':time.time()+3000}}}))
            result=usage.collect({}, {'sources':{'Alice':{'path':str(p),'format':'claude-statusline','account':'A'},
                                                'Bob':{'path':str(p)+'-missing','format':'claude-statusline','account':'B'}}})
            self.assertEqual(result['patches'][0]['cells']['limits']['bars'][0]['percent'],20)
            self.assertEqual(result['patches'][1]['cells']['limits']['text'],'Unavailable')

    def test_expired_negative_nan_and_percentage_above_100_do_not_fake_a_limit(self):
        for pct in (-1,101,float('nan'),True,'90'):
            self.assertIsNone(usage.window('5h',pct,300,100))
        self.assertIsNone(usage.window('5h',30,99,100))
        self.assertEqual(usage.window('5h',30,'2030-01-01T00:00:00Z',100)['percent'],30)

    def test_rollout_tail_and_real_window_labels(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'rollout.jsonl'
            p.write_text(json.dumps({'timestamp':'2026-10-15T12:00:00Z','payload':{'rate_limits':{
                'primary':{'used_percent':42,'window_minutes':180,'resets_at':2_000_000_000}}}})+'\n')
            result=usage.read_source({'path':str(p),'format':'codex-rollout'},1_800_000_000)
            self.assertEqual(result['bars'][0]['label'],'3h')


class RotatingAndTopLevelSources(unittest.TestCase):
    def _rollout(self, path, pct, mtime):
        path.write_text(json.dumps({'timestamp': '2026-10-15T12:00:00Z', 'payload': {'rate_limits': {
            'primary': {'used_percent': pct, 'window_minutes': 300, 'resets_at': 2_000_000_000}}}})+'\n')
        os.utime(path, (mtime, mtime))

    def test_glob_reads_the_newest_rollout(self):
        with tempfile.TemporaryDirectory() as d:
            self._rollout(Path(d)/'rollout-old.jsonl', 10, 1000)
            self._rollout(Path(d)/'rollout-new.jsonl', 55, 2000)
            spec = {'glob': str(Path(d)/'rollout-*.jsonl'), 'format': 'codex-rollout'}
            self.assertEqual(usage.read_source(spec, 1_800_000_000)['bars'][0]['percent'], 55)

    def test_newest_rollout_without_a_reading_falls_back_to_the_previous(self):
        with tempfile.TemporaryDirectory() as d:
            self._rollout(Path(d)/'rollout-old.jsonl', 10, 1000)
            young = Path(d)/'rollout-young.jsonl'
            young.write_text('{"type":"session_meta"}\n')
            os.utime(young, (2000, 2000))
            spec = {'glob': str(Path(d)/'rollout-*.jsonl'), 'format': 'codex-rollout'}
            self.assertEqual(usage.read_source(spec, 1_800_000_000)['bars'][0]['percent'], 10)

    def test_glob_without_a_match_is_unavailable_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            result = usage.collect({}, {'sources': {'A': {'glob': str(Path(d)/'none-*.jsonl'),
                                                          'format': 'codex-rollout'}}})
            self.assertEqual(result['patches'][0]['cells']['limits']['text'], 'Unavailable')

    def test_statusline_file_with_windows_at_top_level(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'limits.json'
            now = time.time()
            p.write_text(json.dumps({'captured_at': now, 'five_hour': None,
                                     'seven_day': {'used_percentage': 40, 'resets_at': int(now)+9000}}))
            result = usage.read_source({'path': str(p), 'format': 'claude-statusline'}, now)
            self.assertEqual([(b['label'], b['percent']) for b in result['bars']], [('7d', 40)])
            self.assertEqual(result['text'], '')

    def test_captured_at_decides_staleness_not_file_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'limits.json'
            now = time.time()
            p.write_text(json.dumps({'captured_at': now-3600,
                                     'seven_day': {'used_percentage': 40, 'resets_at': int(now)+9000}}))
            self.assertEqual(usage.read_source({'path': str(p), 'format': 'claude-statusline'}, now)['text'], 'Cached')


class UsageView(unittest.TestCase):
    def test_config_view_defaults_to_column_accepts_section_rejects_junk(self):
        from agentsmon import config
        def load(up):
            with tempfile.TemporaryDirectory() as d:
                p = Path(d)/'c.json'
                p.write_text(json.dumps({'usage_plugin': up, 'modules_dir': d}))
                return config.load(p)
        self.assertNotIn('view', load({'sources': {}}).get('usage_plugin') or {})
        self.assertEqual(load({'view': 'column', 'sources': {}})['usage_plugin']['view'], 'column')
        self.assertEqual(load({'view': 'section', 'sources': {}})['usage_plugin']['view'], 'section')
        for junk in ('Section', '', None, 1, 'table'):
            with self.assertRaises(ValueError):
                load({'view': junk, 'sources': {}})

    def test_sources_report_reset_window_as_of_and_account_through_the_host(self):
        from agentsmon import plugins
        now = time.time()
        with tempfile.TemporaryDirectory() as d:
            codex = Path(d)/'rollout.jsonl'
            codex.write_text(json.dumps({'timestamp': '2026-10-15T12:00:00Z', 'payload': {'rate_limits': {
                'primary': {'used_percent': 42, 'window_minutes': 300, 'resets_at': int(now)+3000},
                'secondary': {'used_percent': 10, 'window_minutes': 10080, 'resets_at': int(now)+90000}}}})+'\n')
            claude = Path(d)/'claude.json'
            claude.write_text(json.dumps({'captured_at': now-120, 'five_hour': {'used_percentage': 5, 'resets_at': int(now)+600}}))
            result = usage.collect({}, {'sources': {
                'c': {'path': str(codex), 'format': 'codex-rollout', 'account': 'Team'},
                'k': {'path': str(claude), 'format': 'claude-statusline', 'account': 'Work'},
                'x': {'path': str(claude)+'-none', 'format': 'claude-statusline', 'account': 'Gone'}}})
            plugin = plugins.Plugin('usage', '1.0.0', {}, {})
            cells = {p['row_id']: p['cells']['usage:limits']
                     for p in plugins.validate_result(plugin, result)['patches']}
        self.assertEqual([(b['window_minutes'], b['reset']) for b in cells['c']['bars']],
                         [(300, int(now)+3000), (10080, int(now)+90000)])
        self.assertEqual(cells['c']['account'], 'Team')
        self.assertEqual(cells['c']['as_of'], usage.epoch('2026-10-15T12:00:00Z'))
        self.assertEqual(cells['k']['bars'][0]['reset'], int(now)+600)
        self.assertEqual(cells['k']['bars'][0]['window_minutes'], 300)   # five_hour names its window
        self.assertEqual(cells['k']['as_of'], int(now-120))
        self.assertEqual(cells['x']['account'], 'Gone')
        self.assertEqual(cells['x']['text'], 'Unavailable')

    def test_state_hides_usage_column_only_in_section_view(self):
        from unittest.mock import patch
        from agentsmon import config, dashboard, plugins
        extra = {k: [] for k in ('tables', 'patches', 'hidden_sessions', 'hidden_services', 'errors', 'plugins')}
        extra['columns'] = [{'id': 'usage:limits', 'title': 'Usage'}, {'id': 'other:x', 'title': 'X'}]
        for up, view, ids in ((None, 'column', ['usage:limits', 'other:x']),
                              ({'view': 'column'}, 'column', ['usage:limits', 'other:x']),
                              ({'view': 'section'}, 'section', ['other:x'])):
            with self.subTest(view=view), patch.object(config, 'load', return_value={'usage_plugin': up}), \
                    patch.object(dashboard, '_agents_state', return_value=[]), \
                    patch.object(dashboard, '_service_state', return_value=[]), \
                    patch('agentsmon.integrity.status', return_value={'state': 'fixture'}), \
                    patch.object(plugins, 'collect', return_value=extra):
                data = json.loads(dashboard._state())
                self.assertEqual(data['usage_view'], view)
                self.assertEqual([c['id'] for c in data['columns']], ids)
