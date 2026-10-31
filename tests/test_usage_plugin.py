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
            result = usage.read_source({'path': str(p), 'format': 'claude-statusline'}, now)
            self.assertEqual(result['tone'], 'muted')
            self.assertEqual(result['as_of'], int(now-3600))
            self.assertEqual(result['bars'][0]['reset'], int(now)+9000)

    def test_host_keeps_reset_and_as_of_the_page_shows(self):
        from agentsmon import plugins
        cell = plugins.cell({'text': '', 'tone': 'muted', 'as_of': 1000,
                             'bars': [{'label': '5h', 'percent': 39, 'reset': 2000}, {'label': '7d', 'percent': 3}]})
        self.assertEqual(cell['as_of'], 1000)
        self.assertEqual([b['reset'] for b in cell['bars']], [2000, None])

    def test_window_reports_reset_epoch_or_none(self):
        self.assertEqual(usage.window('5h', 39, 2_000_000_000, 100)['reset'], 2_000_000_000)
        self.assertEqual(usage.window('5h', 39, '2030-01-01T00:00:00Z', 100)['reset'], 1_893_456_000)
        self.assertIsNone(usage.window('5h', 39, None, 100)['reset'])
