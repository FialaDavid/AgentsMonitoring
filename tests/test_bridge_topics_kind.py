"""Agent2TelegramTopic (a2tt) bridges next to Agent2Telegram (a2t): detection, state, outage."""
import io, json, os, shlex, tempfile, time, unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agentsmon import bridges, config, detect

PY = '/venv/bin/python'


def spec(root, bot='Bot', **extra):
    instance = Path(root)/bot
    (instance/'state').mkdir(parents=True, exist_ok=True)
    return {'name': bot, 'bridge_config': str(instance/'config.json'), 'kind': 'a2tt', **extra}


def write_status(sp, **fields):
    data = {'schema': 1, 'updated_at': int(time.time()), 'heartbeat_seconds': 15, 'lanes': [], **fields}
    bridges.status_file(sp).write_text(json.dumps(data))


class Detection(unittest.TestCase):
    def test_topic_bridge_command_is_recognised_with_its_config(self):
        with patch.object(bridges, 'process_env', return_value={}):
            info = bridges.process_info(1, PY+' -m agent2telegramtopic run --config /tmp/bots/Bot/config.json')
            plain = bridges.process_info(2, PY+' -m agent2telegram run --config /tmp/one.json')
            other = bridges.process_info(3, PY+' -m agent2telegramtopic hook --config /tmp/x.json')
        self.assertEqual((info['kind'], info['config']), ('a2tt', '/tmp/bots/Bot/config.json'))
        self.assertEqual(plain['kind'], 'a2t')
        self.assertIsNone(other)

    def test_topic_bridge_needs_an_explicit_or_env_config(self):
        with patch.object(bridges, 'process_env', return_value={}):
            self.assertIsNone(bridges.process_info(1, PY+' -m agent2telegramtopic run'))
        with patch.object(bridges, 'process_env', return_value={'AGENT2TELEGRAMTOPIC_CONFIG': '/tmp/e.json'}):
            self.assertEqual(bridges.process_info(1, PY+' -m agent2telegramtopic run')['config'], '/tmp/e.json')

    def test_registered_topic_bridge_round_trips(self):
        with tempfile.TemporaryDirectory() as d, patch.object(config, 'state_dir', return_value=Path(d)):
            item = bridges.entry('/tmp/bots/Bot/config.json', PY, kind='a2tt')
            self.assertEqual(item['kind'], 'a2tt')
            self.assertEqual(item['name'], 'Telegram Bridge: Bot (topics)')
            self.assertIn('agent2telegramtopic', shlex.split(item['restart']))
            self.assertIn('AGENT2TELEGRAMTOPIC_CONFIG=/tmp/bots/Bot/config.json', shlex.split(item['restart']))
            self.assertNotIn('kind', bridges.entry('/tmp/one.json', PY))   # old configs stay as they were
            with self.assertRaises(ValueError):
                bridges.entry('/tmp/bots/Bot/config.json', PY, state='/tmp/s', kind='a2tt')
            cfg = {}
            bridges.register(cfg, item)
            bridges.register(cfg, item)
            self.assertEqual(len(cfg['daemons']), 1)

    def test_cli_registers_a_topic_bridge(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'config.json'
            args = Namespace(bridge_config='/tmp/bots/Bot/config.json', bridge_python=PY, bridge_pythonpath='',
                             bridge_state='', bridge_kind='a2tt')
            with patch.object(config, 'DEFAULT_PATH', path), patch.object(config, 'state_dir', return_value=Path(d)), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(bridges.add_noninteractive(args), 0)
            self.assertEqual(json.loads(path.read_text())['daemons'][0]['kind'], 'a2tt')

    def test_a2t_and_a2tt_with_the_same_config_path_are_told_apart(self):
        a2t = {'name': 'A', 'bridge_config': '/tmp/x.json'}
        a2tt = {**a2t, 'kind': 'a2tt'}
        procs = [(1, {'config': '/tmp/x.json', 'kind': 'a2t'}), (2, {'config': '/tmp/x.json', 'kind': 'a2tt'})]
        with patch.object(bridges, 'processes', return_value=procs):
            self.assertEqual(bridges.running_pids(a2t), [1])
            self.assertEqual(bridges.running_pids(a2tt), [2])


class State(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sp = spec(self.tmp.name)

    def running(self):
        return patch.object(bridges, 'running_pids', return_value=[7])

    def test_default_status_file_follows_the_instance_layout(self):
        self.assertEqual(bridges.status_file({'bridge_config': '/x/bots/Bot/config.json'}),
                         Path('/x/bots/Bot/state/Bot.status.json'))
        self.assertEqual(bridges.status_file({'bridge_config': '/x/c.json', 'status_file': '/y/s.json'}), Path('/y/s.json'))

    def test_fresh_heartbeat_is_running_old_heartbeat_is_stale(self):
        now = int(time.time())
        with self.running():
            write_status(self.sp, updated_at=now-60)          # limit is 3*15+30 = 75 s
            self.assertEqual(bridges.state(self.sp, now)[0], 'running')
            write_status(self.sp, updated_at=now-76)
            self.assertEqual(bridges.state(self.sp, now)[0], 'stale')
            write_status(self.sp, updated_at=now-100, heartbeat_seconds=30)   # limit 120 s
            self.assertEqual(bridges.state(self.sp, now)[0], 'running')

    def test_missing_or_unusable_status_file_is_judged_by_the_process_alone(self):
        with self.running():
            self.assertEqual(bridges.state(self.sp)[0], 'running')
            bridges.status_file(self.sp).write_text('not json')
            self.assertEqual(bridges.state(self.sp)[0], 'running')
            bridges.status_file(self.sp).write_text('{"schema": 2}')
            self.assertEqual(bridges.state(self.sp)[0], 'running')

    def test_no_process_is_down_even_with_a_fresh_file(self):
        write_status(self.sp)
        with patch.object(bridges, 'running_pids', return_value=[]):
            self.assertEqual(bridges.state(self.sp)[0], 'down')

    def test_a2t_ignores_a_status_file(self):
        old = {**self.sp, 'kind': None}
        write_status(old, updated_at=1)
        with self.running():
            self.assertEqual(bridges.state(old)[0], 'running')


class Health(unittest.TestCase):
    A2T = {'name': 'Old', 'bridge_config': '/tmp/old.json'}

    def test_only_one_kind_configured_is_no_outage_of_the_other(self):
        with tempfile.TemporaryDirectory() as d:
            topic = spec(d)
            procs = [(1, {'config': topic['bridge_config'], 'kind': 'a2tt'})]
            with patch.object(bridges, 'processes', return_value=procs):
                self.assertEqual(bridges.health({'daemons': [topic]}), (True, '1/1 bridges running'))
            procs = [(2, {'config': '/tmp/old.json'})]
            with patch.object(bridges, 'processes', return_value=procs):
                self.assertEqual(bridges.health({'daemons': [self.A2T]}), (True, '1/1 bridges running'))

    def test_both_kinds_together_each_with_its_own_state(self):
        with tempfile.TemporaryDirectory() as d:
            topic = spec(d)
            write_status(topic, updated_at=int(time.time())-500)
            procs = [(1, {'config': topic['bridge_config'], 'kind': 'a2tt'}), (2, {'config': '/tmp/old.json'})]
            cfg = {'daemons': [self.A2T, topic]}
            with patch.object(bridges, 'processes', return_value=procs):
                up, detail = bridges.health(cfg)
                rows = {r['name']: (r['up'], r['detail'].split(' ')[0]) for r in bridges.rows(cfg)}
            self.assertFalse(up)
            self.assertEqual(detail, '1/2 bridges running; stale: Bot')
            self.assertEqual(rows, {'Old': (True, 'running'), 'Bot': (False, 'stale')})

    def test_disabled_bridge_is_not_counted(self):
        with tempfile.TemporaryDirectory() as d:
            topic = spec(d, enabled=False)
            with patch.object(bridges, 'processes', return_value=[]):
                self.assertIsNone(bridges.health({'daemons': [topic]}))


    def test_close_ref_and_command_reuse_bridge_python_and_environment(self):
        with tempfile.TemporaryDirectory() as d:
            sp = spec(d)
            sp['restart'] = bridges.entry(sp['bridge_config'], PY, '/modules', kind='a2tt')['restart']
            write_status(sp, lanes=[{'thread': 17, 'status': 'parked'}])
            topic = bridges.topics(sp)['items'][0]
            self.assertEqual(topic['close_ref'], bridges.close_ref(sp, 17))
            self.assertNotIn('bridge_config', topic)
            argv, env = bridges.close_command(sp, 17)
            self.assertEqual(argv, [PY, '-m', 'agent2telegramtopic', 'close', '--config',
                                    sp['bridge_config'], '--thread', '17'])
            self.assertEqual(env['PYTHONPATH'], '/modules')
            self.assertEqual(bridges.close_command(sp, 17, 'restart')[0],
                             [PY, '-m', 'agent2telegramtopic', 'restart', '--config',
                              sp['bridge_config'], '--thread', '17'])

if __name__ == '__main__':
    unittest.main()
