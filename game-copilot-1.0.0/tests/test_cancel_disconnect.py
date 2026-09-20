import copy
import json
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch
from copilot.adapters import BridgeAdapter, validate_state, fetch_json
from copilot.capabilities import normalize_capabilities, execution_block_reason
from copilot.engine import Engine
from copilot.task_demo import CancellableTaskDemo
from copilot.task_control import TaskControl
from copilot.tasks import TaskManager
from examples.cancel_bridge import make_server
from examples.cancel_disconnect_demo import wait_until


class DemoWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.demo = CancellableTaskDemo(clock=lambda: self.now, start_thread=False)
        self.addCleanup(self.demo.close)

    def task(self, ident='one'):
        return {'task_id': ident, 'game_id': self.demo.game_id, 'action_id': 'count', 'revision': self.demo.session}

    def test_acceptance_not_completion_duplicate_start(self):
        task = self.task()
        r = self.demo.start_task(task, 'owner')
        self.assertEqual(r['status'], 'accepted')
        self.now += 0.5
        self.demo.tick()
        r = self.demo.start_task(task, 'owner')
        self.assertEqual(r['status'], 'running')
        self.assertEqual(self.demo.counter, 1)
        with self.assertRaises(ValueError):
            self.demo.start_task(self.task('two'), 'owner')

    def test_cancel_actually_releases_and_freezes_counter(self):
        self.demo.start_task(self.task(), 'owner')
        self.now += 0.5
        self.demo.tick()
        self.assertTrue(self.demo.controls_active)
        self.assertEqual(self.demo.cancel_task('one', 'owner')['status'], 'cancelled')
        count = self.demo.counter
        self.now += 0.5
        self.demo.tick()
        self.assertEqual(self.demo.counter, count)
        self.assertFalse(self.demo.controls_active)

    def test_watchdog_expiry_not_revived_by_late_heartbeat(self):
        self.demo.start_task(self.task(), 'owner')
        self.now += 0.2
        self.demo.tick()
        self.now += 3
        self.demo.tick()
        self.assertEqual(self.demo.heartbeat('one', 'owner')['status'], 'cancelled')
        self.assertFalse(self.demo.controls_active)

    def test_polling_does_not_renew_lease(self):
        self.demo.start_task(self.task(), 'owner')
        for _ in range(4):
            self.now += 1
            self.demo.task_status('one', 'owner')
        self.assertIsNone(self.demo.active)

    def test_heartbeat_extends_but_hard_deadline_still_stops(self):
        self.demo.start_task(self.task(), 'owner', max_seconds=5)
        for _ in range(6):
            self.now += 1
            self.demo.heartbeat('one', 'owner')
            self.demo.tick()
        self.assertEqual(self.demo.task_status('one', 'owner')['status'], 'failed')
        self.assertFalse(self.demo.controls_active)

    def test_cancel_before_start_never_executes(self):
        self.demo.cancel_task('one', 'owner')
        self.assertEqual(self.demo.start_task(self.task(), 'owner')['status'], 'cancelled')
        self.demo.tick()
        self.assertEqual(self.demo.counter, 0)

    def test_other_owner_rejected_and_success_preserved_after_stop(self):
        self.demo.start_task(self.task(), 'owner')
        for fn in (self.demo.cancel_task, self.demo.heartbeat, self.demo.task_status):
            with self.assertRaises(ValueError):
                fn('one', 'someone-else')
        for _ in range(21):
            self.demo.heartbeat('one', 'owner')
            self.now += 1
            self.demo.tick()
        self.assertEqual(self.demo.cancel_task('one', 'owner')['status'], 'succeeded')

    def test_stale_revision_expired_request(self):
        task = self.task()
        with self.assertRaises(ValueError):
            self.demo.start_task({**task, 'revision': 'old-session'}, 'owner')
        with self.assertRaises(ValueError):
            self.demo.start_task(task, 'owner', expires_at=time.time()-1)

    def test_caps_optional_but_malformed_safety_rejected(self):
        state = self.demo.observe()
        self.assertEqual(validate_state(state, self.demo.game_id)['capabilities']['task_protocol'], 1)
        for change in ({'task_protocol': True}, {'supports_cancel': False}, {'lease_seconds': float('nan')}, {'lease_seconds': 100}):
            candidate = copy.deepcopy(state)
            candidate['capabilities'].update(change)
            with self.assertRaises(ValueError):
                normalize_capabilities(candidate)
        legacy = copy.deepcopy(state)
        del legacy['capabilities']['task_protocol']
        del legacy['capabilities']['lease_seconds']
        self.assertTrue(execution_block_reason(legacy))
        state['capabilities']['execution_mode'] = 'realtime'
        self.assertTrue(execution_block_reason(state))  # Step 4+ control is intentionally not enabled.


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.engine.stop()
        if self.engine.cancel_demo:
            self.engine.cancel_demo.close()

    def begin(self):
        self.engine.launch(self.engine.start_cancel_demo)
        wait_until(lambda: self.engine.tasks.snapshot() is not None and self.engine.tasks.snapshot()['status'] == 'running')

    def test_ui_engine_stop_and_no_second_work(self):
        self.begin()
        with self.assertRaises(ValueError):
            self.engine.launch(self.engine.start_cancel_demo)
        self.engine.stop()
        wait_until(lambda: not self.engine.busy and not self.engine.task_control.cancel_busy.is_set())
        self.assertEqual(self.engine.tasks.snapshot()['status'], 'cancelled')
        self.assertFalse(self.engine.cancel_demo.controls_active)
        self.assertEqual(self.engine.calls, 0)

    def test_disconnect_unknown_until_independent_stop_and_reconcile(self):
        self.begin()
        self.engine.task_control.drop_demo_connection()
        wait_until(lambda: not self.engine.busy)
        self.assertEqual(self.engine.tasks.snapshot()['status'], 'unknown')
        with self.assertRaises(ValueError):
            self.engine.tasks.ensure_idle()
        # No method calls into the demo while waiting. Its background watchdog must run.
        wait_until(lambda: self.engine.cancel_demo.active is None)
        self.assertFalse(self.engine.cancel_demo.controls_active)
        self.assertEqual(self.engine.tasks.snapshot()['status'], 'unknown')
        self.assertEqual(self.engine.reconcile_task()['status'], 'cancelled')
        self.engine.tasks.ensure_idle()

    def test_cancel_before_delayed_start_response(self):
        demo = CancellableTaskDemo()
        self.addCleanup(demo.close)
        entered, release = threading.Event(), threading.Event()
        original = demo.start_task
        def delayed(*args):
            entered.set()
            release.wait(2)
            return original(*args)
        with patch.object(demo, 'start_task', side_effect=delayed):
            thread = threading.Thread(target=self.engine.task_control.run, args=(demo, demo.observe(), 'count'))
            thread.start()
            self.assertTrue(entered.wait(1))
            self.engine.stop()
            wait_until(lambda: not self.engine.task_control.cancel_busy.is_set())
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(demo.counter, 0)
        self.assertEqual(self.engine.tasks.snapshot()['status'], 'cancelled')

    def test_lost_start_ack_is_not_retried_and_cancelled(self):
        demo = CancellableTaskDemo()
        self.addCleanup(demo.close)
        original = demo.start_task
        def lost(*args):
            original(*args)
            raise TimeoutError()
        with patch.object(demo, 'start_task', side_effect=lost) as start:
            with self.assertRaises(ValueError):
                self.engine.task_control.run(demo, demo.observe(), 'count')
        self.assertEqual(start.call_count, 1)
        self.assertEqual(self.engine.tasks.snapshot()['status'], 'cancelled')
        self.assertFalse(demo.controls_active)

    def test_failed_cancel_stays_unknown_and_blocks_new_work(self):
        demo = CancellableTaskDemo()
        self.addCleanup(demo.close)
        with patch.object(demo, 'start_task', side_effect=TimeoutError), patch.object(demo, 'cancel_task', side_effect=TimeoutError):
            with self.assertRaises(ValueError):
                self.engine.task_control.run(demo, demo.observe(), 'count')
        self.assertEqual(self.engine.tasks.snapshot()['status'], 'unknown')
        with self.assertRaises(ValueError):
            self.engine.tasks.ensure_idle()

    def test_mismatched_receipt_never_counts_as_success(self):
        demo = CancellableTaskDemo()
        self.addCleanup(demo.close)
        fake = {'task_id': 'another', 'controller_id': 'other', 'game_id': demo.game_id, 'status': 'succeeded', 'sequence': 0}
        with patch.object(demo, 'start_task', return_value=fake), patch.object(demo, 'cancel_task', return_value=fake):
            with self.assertRaises(ValueError):
                self.engine.task_control.run(demo, demo.observe(), 'count')
        self.assertEqual(self.engine.tasks.snapshot()['status'], 'unknown')

    def test_complete_turn_based_task_through_existing_engine(self):
        demo = CancellableTaskDemo(duration=0.2)
        self.addCleanup(demo.close)
        self.engine.adapter = demo
        self.engine.pending = {'action': demo.observe()['actions'][0], 'revision': demo.session,
            'created': time.monotonic(), 'confidence': 1, 'need_deep': False}
        self.engine.execute(True)
        self.assertEqual(self.engine.tasks.snapshot()['status'], 'succeeded')

    def test_recovery_of_old_task_cannot_confirm_a_new_v1_task(self):
        demo = CancellableTaskDemo(duration=0.1)
        self.addCleanup(demo.close)
        self.engine.task_control.run(demo, demo.observe(), 'count')
        new = self.engine.tasks.begin('other-game', 'other-action', 'r')
        with self.assertRaises(ValueError):
            self.engine.reconcile_task()
        self.assertEqual(self.engine.tasks.snapshot()['task_id'], new['task_id'])
        self.assertEqual(self.engine.tasks.snapshot()['status'], 'starting')

    def test_fault_injection_cannot_target_a_new_v1_task(self):
        demo = CancellableTaskDemo(duration=0.1)
        self.addCleanup(demo.close)
        self.engine.task_control.run(demo, demo.observe(), 'count')
        self.engine.tasks.begin('other-game', 'other-action', 'r')
        with self.assertRaises(ValueError):
            self.engine.task_control.drop_demo_connection()
        self.assertEqual(self.engine.tasks.snapshot()['status'], 'starting')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server, self.demo, self.token = make_server(0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.profile = {'endpoint': self.url, 'game_id': self.demo.game_id, 'bridge_token': self.token}
        self.adapter = BridgeAdapter(self.profile)

    def tearDown(self):
        self.demo.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_http_start_heartbeat_cancel_status(self):
        task = {'task_id': 'one', 'game_id': self.demo.game_id, 'action_id': 'count', 'revision': self.adapter.observe()['revision']}
        self.assertEqual(self.adapter.start_task(task, 'owner')['status'], 'accepted')
        self.assertEqual(self.adapter.heartbeat('one', 'owner')['task_id'], 'one')
        self.assertEqual(self.adapter.cancel_task('one', 'owner')['status'], 'cancelled')
        self.assertEqual(self.adapter.task_status('one', 'owner')['status'], 'cancelled')

    def test_auth_and_wrong_game(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            fetch_json(self.url + '/v1/state', token='wrong', local=True)
        self.assertEqual(e.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError):
            fetch_json(self.url + '/v1/tasks/cancel', {'schema_version': 1, 'game_id': 'wrong', 'task_id': 't',
                'controller_id': 'o'}, self.token, local=True)

    def test_client_process_exit_does_not_leave_job_running(self):
        # The server/watchdog live in THIS process. A separate client exits without cancelling.
        code = '''import json, sys
from copilot.adapters import BridgeAdapter
a = BridgeAdapter(json.loads(sys.argv[1]))
s = a.observe()
r = a.start_task({'task_id':'departed-client','game_id':s['game_id'],'action_id':'count','revision':s['revision']}, 'departed-owner')
assert r['status'] == 'accepted'
'''
        completed = subprocess.run([sys.executable, '-c', code, json.dumps(self.profile)], capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        wait_until(lambda: self.demo.active is None)
        self.assertFalse(self.demo.controls_active)
        self.assertEqual(self.demo.task_status('departed-client', 'departed-owner')['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
