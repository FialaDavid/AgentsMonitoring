import json, tempfile, threading, time, unittest, urllib.error, urllib.request
from pathlib import Path
from unittest.mock import patch

from agentsmon import dashboard


class TopicRestartHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.status = root/'status.json'
        self.spec = {'kind':'a2tt', 'bridge_config':str(root/'config.json'),
                     'status_file':str(self.status), 'restart':f'python -m agent2telegramtopic restart --config {root}/config.json'}
        self.status.write_text(json.dumps({'schema':1, 'updated_at':int(time.time()), 'lanes':[
            {'thread':17, 'status':'live'}, {'thread':18, 'status':'parked'}]}))
        self.cfg = {'daemons':[self.spec]}
        self.server = dashboard.make_server('127.0.0.1', 0, {})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def post(self, route, ref, csrf=True):
        headers = {'Content-Type':'application/json'}
        if csrf:
            headers['X-Agentsmon'] = 'action'
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}/api/topic/{route}',
                                     data=json.dumps({'ref':ref}).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read()) if error.headers.get('Content-Type') == 'application/json' else None

    def test_restart_routes_live_ref_to_restart_argv_and_keeps_close(self):
        with patch.object(dashboard.config, 'load', return_value=self.cfg), \
             patch('agentsmon.bridges.close_command', return_value=(['python','-m','agent2telegramtopic','restart','--config','cfg','--thread','17'], {'K':'V'})) as command, \
             patch.object(dashboard.subprocess, 'run', return_value=type('Result', (), {'returncode':0,'stdout':'Restarted\nmore','stderr':''})()) as run:
            restart_ref = __import__('agentsmon.bridges', fromlist=['close_ref']).close_ref(self.spec, 17)
            status, data = self.post('restart', restart_ref)
            self.assertEqual((status, data), (200, {'ok':True, 'message':'Restarted'}))
            command.assert_called_once_with(self.spec, 17, 'restart')
            run.assert_called_once_with(['python','-m','agent2telegramtopic','restart','--config','cfg','--thread','17'], env={'K':'V'}, capture_output=True, text=True, timeout=15)
            parked_ref = __import__('agentsmon.bridges', fromlist=['close_ref']).close_ref(self.spec, 18)
            self.assertEqual(self.post('restart', parked_ref)[0], 400)
            self.assertEqual(self.post('restart', 'unknown')[0], 400)
            self.assertEqual(self.post('close', parked_ref)[0], 200)

    def test_restart_requires_csrf_header_and_command_failure_is_conflict(self):
        from agentsmon.bridges import close_ref
        with patch.object(dashboard.config, 'load', return_value=self.cfg), \
             patch('agentsmon.bridges.close_command', return_value=(['cmd'], {})), \
             patch.object(dashboard.subprocess, 'run', return_value=type('Result', (), {'returncode':2,'stdout':'','stderr':'Lane unavailable\n'})()):
            ref = close_ref(self.spec, 17)
            self.assertEqual(self.post('restart', ref, csrf=False)[0], 403)
            status, data = self.post('restart', ref)
            self.assertEqual((status, data), (409, {'ok':False, 'message':'Lane unavailable'}))


if __name__ == '__main__':
    unittest.main()
