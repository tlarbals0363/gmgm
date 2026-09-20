import copy
import json
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from copilot.storage import Store, atomic_json
from copilot.adapters import DemoAdapter, BridgeAdapter, validate_state, fetch_json
from copilot.ai import parse_plan, demo_plan, ask
from copilot.engine import Engine, Cancelled
from examples.demo_bridge import make_server


PLAN = {'advice': '연구하세요.', 'action_id': 'research', 'confidence': 0.95, 'need_deep': False, 'memory': ''}
SETTINGS = {'fast': 'fast-model', 'deep': 'deep-model', 'fast_timeout': 1,
            'deep_timeout': 1, 'call_limit': 10, 'interval': 2}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)

    def test_crud_restore_persistent(self):
        ident = self.store.create('새 게임')
        self.store.data['profiles'][ident]['notes'] = '튜토리얼 내용'
        self.store.save()
        self.store.delete(ident)
        self.assertNotIn(ident, self.store.data['profiles'])
        self.assertEqual(self.store.restore(), ident)
        self.assertEqual(Store(self.tmp.name).data['profiles'][ident]['notes'], '튜토리얼 내용')

    def test_empty_and_pathlike_names(self):
        with self.assertRaises(ValueError):
            self.store.create(' ')
        self.store.create('../../game')
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())), 1)

    def test_corrupt_file_preserved(self):
        file = Path(self.tmp.name) / 'workspace.json'
        atomic_json(file, {'version': 99})
        with self.assertRaises(ValueError):
            Store(self.tmp.name)
        self.assertEqual(json.loads(file.read_text())['version'], 99)

    def test_edition_settings_separate(self):
        self.store.data['settings']['free'] = {'fast': 'custom'}
        self.assertEqual(self.store.settings('free')['fast'], 'custom')
        self.assertNotEqual(self.store.settings('paid')['fast'], 'custom')

    @patch('copilot.storage.crypt', side_effect=lambda b, decrypt=False: b)
    def test_key_import_no_exec(self, _):
        env_file = Path(self.tmp.name) / '.env.nvidia'
        atomic_json(Path(self.tmp.name) / 'unrelated.json', {'kept': True})
        # The filesystem fixture is created in an isolated temporary test directory.
        env_file.write_text('NVIDIA_API_KEY="nvapi-example-test-only"\nOTHER=ignored', encoding='utf-8')
        self.store.import_key('free', self.tmp.name)
        self.assertEqual(self.store.key('free'), 'nvapi-example-test-only')
        self.store.save_key('free', 'Bearer nvapi-another-test-only')
        self.assertEqual(self.store.key('free'), 'nvapi-another-test-only')
        self.assertTrue(env_file.exists())


class AdapterTests(unittest.TestCase):
    def test_demo_victory_and_idempotency(self):
        demo = DemoAdapter()
        for i in range(6):
            state = demo.observe()
            plan = demo_plan(state)
            receipt = demo.act(plan['action_id'], state['revision'], str(i))
            self.assertEqual(demo.act(plan['action_id'], state['revision'], str(i)), receipt)
        self.assertEqual(demo.observe()['state']['outcome'], '승리')
        self.assertEqual(demo.observe()['state']['turn'], 6)

    def test_reject_stale_revision(self):
        demo = DemoAdapter()
        rev = demo.observe()['revision']
        demo.act('research', rev, '1')
        with self.assertRaises(ValueError):
            demo.act('research', rev, '2')

    def test_revision_changes_on_new_session(self):
        self.assertNotEqual(DemoAdapter().observe()['revision'], DemoAdapter().observe()['revision'])

    def test_validate_wrong_game_duplicate_action(self):
        state = DemoAdapter().observe()
        with self.assertRaises(ValueError):
            validate_state(state, 'other')
        state['actions'].append(state['actions'][0])
        with self.assertRaises(ValueError):
            validate_state(state, 'demo-colony')

    def test_remote_bridge_rejected(self):
        for endpoint in ['http://example.com:8766', 'https://127.0.0.1:8766', 'http://127.0.0.1:8766/path']:
            with self.assertRaises(ValueError):
                BridgeAdapter({'endpoint': endpoint, 'game_id': 'demo-colony', 'bridge_token': 'abc'})


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.token = make_server(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def test_authentication(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            fetch_json(self.url + '/v1/state', token='wrong', local=True)
        self.assertEqual(error.exception.code, 401)

    def test_roundtrip_stale_and_duplicate(self):
        adapter = BridgeAdapter({'endpoint': self.url, 'game_id': 'demo-colony', 'bridge_token': self.token})
        state = adapter.observe()
        receipt = adapter.act('research', state['revision'], 'roundtrip-1')
        self.assertTrue(receipt['accepted'])
        self.assertEqual(adapter.act('research', state['revision'], 'roundtrip-1'), receipt)
        with self.assertRaises(urllib.error.HTTPError):
            adapter.act('research', state['revision'], 'roundtrip-2')

    def test_expired_action(self):
        state = fetch_json(self.url + '/v1/state', token=self.token, local=True)
        with self.assertRaises(urllib.error.HTTPError):
            fetch_json(self.url + '/v1/action', {'schema_version': 1, 'game_id': 'demo-colony',
                'request_id': 'expired', 'action_id': 'research', 'expected_revision': state['revision'],
                'expires_at': time.time() - 10}, self.token, local=True)


class AITests(unittest.TestCase):
    def test_parse_and_reject_invalid(self):
        self.assertEqual(parse_plan('```json\n' + json.dumps(PLAN) + '\n```'), PLAN)
        for change in [{'confidence': float('nan')}, {'confidence': True}, {'need_deep': 'false'}, {'action_id': {}}]:
            with self.assertRaises(ValueError):
                parse_plan(json.dumps({**PLAN, **change}))

    @patch('copilot.ai.fetch_json')
    def test_free_request_response(self, fetch):
        fetch.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(PLAN)}}], 'usage': {'total_tokens': 10}}
        plan, usage = ask('free', 'my-model', 'test-key', {'state': 'example'}, 2)
        self.assertEqual(plan, PLAN)
        self.assertEqual(usage['total_tokens'], 10)
        url, body, key, timeout = fetch.call_args.args
        self.assertEqual(url, 'https://integrate.api.nvidia.com/v1/chat/completions')
        self.assertNotIn('test-key', json.dumps(body))
        self.assertEqual(body['model'], 'my-model')

    @patch('copilot.ai.fetch_json')
    def test_paid_request_response(self, fetch):
        fetch.return_value = {'status': 'completed', 'output': [{'type': 'reasoning'}, {'type': 'message',
            'content': [{'type': 'output_text', 'text': json.dumps(PLAN)}]}]}
        self.assertEqual(ask('paid', 'my-model', 'test-key', {}, 2)[0], PLAN)
        self.assertFalse(fetch.call_args.args[1]['store'])
        self.assertEqual(fetch.call_args.args[0], 'https://api.openai.com/v1/responses')

    @patch('copilot.ai.fetch_json')
    def test_truncated_rejected(self, fetch):
        fetch.return_value = {'choices': [{'finish_reason': 'length', 'message': {'content': json.dumps(PLAN)}}]}
        with self.assertRaises(ValueError):
            ask('free', 'x', 'key', {}, 1)

    @patch('copilot.ai.fetch_json')
    def test_auth_error_sanitized(self, fetch):
        fetch.side_effect = urllib.error.HTTPError('https://x', 401, 'SECRET_KEY', {}, None)
        with self.assertRaises(ValueError) as e:
            ask('free', 'x', 'key', {}, 1)
        self.assertNotIn('SECRET', str(e.exception))
        self.assertIn('401', str(e.exception))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = Store(self.tmp.name)
        self.profile = next(iter(store.data['profiles'].values()))
        self.engine = Engine()
        self.engine.connect(self.profile)

    def events(self):
        values = []
        while not self.engine.events.empty():
            values.append(self.engine.events.get_nowait())
        return values

    def analyze(self, **kwargs):
        self.engine.analyze(self.profile, 'free', SETTINGS, 'test-key', **kwargs)

    def test_offline_auto_wins_without_api(self):
        with patch('copilot.ai.ask', side_effect=AssertionError('must not call')):
            for _ in range(6):
                self.analyze(offline=True, auto=True)
        self.assertEqual(self.engine.adapter.observe()['state']['outcome'], '승리')
        self.assertEqual(self.engine.calls, 0)

    @patch('copilot.ai.ask', return_value=(PLAN, {}))
    def test_normal_fast_only(self, request):
        self.analyze(auto=True)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.engine.adapter.observe()['state']['turn'], 1)

    @patch('copilot.ai.ask')
    def test_deep_only_when_needed(self, request):
        request.side_effect = [({**PLAN, 'need_deep': True}, {}), (PLAN, {})]
        self.analyze(auto=True)
        self.assertEqual([c.args[1] for c in request.call_args_list], ['fast-model', 'deep-model'])
        self.assertEqual(self.engine.adapter.observe()['state']['turn'], 1)

    @patch('copilot.ai.ask', return_value=({**PLAN, 'need_deep': True}, {}))
    def test_deep_disabled_blocks_action(self, request):
        self.analyze(auto=True, deep_enabled=False)
        self.assertEqual(request.call_count, 1)
        self.assertIsNone(self.engine.pending)
        self.assertEqual(self.engine.adapter.observe()['state']['turn'], 0)

    @patch('copilot.ai.ask', return_value=({**PLAN, 'action_id': 'shell'}, {}))
    def test_unknown_action_rejected(self, _):
        with self.assertRaises(ValueError):
            self.analyze(auto=True)
        self.assertEqual(self.engine.adapter.observe()['state']['turn'], 0)

    def test_manual_and_stale_action(self):
        self.analyze(offline=True)
        self.assertEqual(self.engine.adapter.observe()['state']['turn'], 0)
        self.engine.execute(True)
        self.assertEqual(self.engine.adapter.observe()['state']['turn'], 1)
        self.analyze(offline=True)
        state = self.engine.adapter.observe()
        self.engine.adapter.act('harvest', state['revision'], 'external')
        with self.assertRaises(ValueError):
            self.engine.execute(True)

    def test_expired_decision(self):
        self.analyze(offline=True)
        self.engine.pending['created'] -= 91
        with self.assertRaises(ValueError):
            self.engine.execute(True)
        self.assertEqual(self.engine.adapter.observe()['state']['turn'], 0)

    def test_call_budget(self):
        self.engine.calls = 10
        with self.assertRaises(ValueError):
            self.analyze()

    def test_cancel_discards_late_response_and_blocks_overlap(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            release.wait(3)
            return PLAN, {}
        with patch('copilot.ai.ask', side_effect=slow):
            self.engine.launch(self.engine.analyze, self.profile, 'free', SETTINGS, 'key', False, True)
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ValueError):
                self.engine.launch(lambda: None)
            self.engine.stop()
            deadline = time.monotonic() + 1
            while self.engine.busy and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            self.assertFalse(self.engine.busy)
            with self.assertRaises(ValueError):
                self.engine.launch(lambda: None)
            release.set()
            deadline = time.monotonic() + 1
            while self.engine.network_lock.locked() and time.monotonic() < deadline:
                threading.Event().wait(0.01)
        self.assertEqual(self.engine.adapter.observe()['state']['turn'], 0)
        self.assertIsNone(self.engine.pending)

    def test_deadline_discards_response(self):
        release = threading.Event()
        def slow(*args):
            release.wait(2)
            return PLAN, {}
        with patch('copilot.ai.ask', side_effect=slow):
            with self.assertRaises(ValueError):
                self.engine.call('free', 'fast', 'key', {}, 0.02, 10)
            self.assertTrue(self.engine.network_lock.locked())
            release.set()
            deadline = time.monotonic() + 1
            while self.engine.network_lock.locked() and time.monotonic() < deadline:
                threading.Event().wait(0.01)

    def test_high_risk_not_auto(self):
        self.analyze(offline=True)
        self.engine.pending['action']['risk'] = 'high'
        with self.assertRaises(ValueError):
            self.engine.execute(False)
        self.assertEqual(self.engine.adapter.observe()['state']['turn'], 0)


if __name__ == '__main__':
    unittest.main()
