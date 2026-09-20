import copy
import threading
import unittest
from unittest.mock import patch
from copilot.engine import Engine, Cancelled
from copilot.tasks import TaskManager
from test_core import SETTINGS

PROFILE = {'id': 'test', 'adapter': 'demo', 'goal': '연구 10', 'notes': ''}


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.m = TaskManager(clock=lambda: self.now)

    def begin(self):
        return self.m.begin('game', 'move', 'session:1', timeout=10)['task_id']

    def test_acceptance_is_not_completion_and_blocks_second_task(self):
        ident = self.begin()
        self.m.update(ident, 'accepted', 0)
        self.assertEqual(self.m.snapshot()['status'], 'accepted')
        with self.assertRaises(ValueError):
            self.begin()
        self.m.update(ident, 'running', 1, progress=0.25)
        self.m.update(ident, 'running', 2, progress=0.75)
        self.m.update(ident, 'succeeded', 3)
        self.assertEqual(self.m.snapshot()['progress'], 1)
        self.assertNotEqual(self.begin(), ident)
        self.assertEqual(self.m.history()[0]['status'], 'succeeded')

    def test_failed_work_and_late_reports_cannot_change_new_work(self):
        ident = self.begin()
        self.m.update(ident, 'failed', 0, message='대상 없음')
        self.assertFalse(self.m.update(ident, 'succeeded', 1))
        new = self.begin()
        self.assertFalse(self.m.update(ident, 'running', 2))
        self.assertEqual(self.m.snapshot()['task_id'], new)
        self.assertEqual(self.m.snapshot()['status'], 'starting')

    def test_duplicate_and_out_of_order_updates(self):
        ident = self.begin()
        self.m.update(ident, 'running', 3, progress=0.4)
        snapshot = self.m.snapshot()
        self.assertFalse(self.m.update(ident, 'succeeded', 2))
        self.assertFalse(self.m.update(ident, 'failed', 3))
        self.assertEqual(self.m.snapshot(), snapshot)
        with self.assertRaises(ValueError):
            self.m.update(ident, 'accepted', 4)
        with self.assertRaises(ValueError):
            self.m.update(ident, 'running', 4, progress=0.1)
        self.assertEqual(self.m.snapshot(), snapshot)

    def test_invalid_reports_do_not_change_task(self):
        ident = self.begin()
        snapshot = self.m.snapshot()
        for status, seq, progress in [('typo', 1, 0), ('running', True, 0),
                                      ('running', -1, 0), ('running', 1, float('nan')),
                                      ('running', 1, True), ('running', 1, 1.1)]:
            with self.subTest(status=status, seq=seq, progress=progress), self.assertRaises(ValueError):
                self.m.update(ident, status, seq, progress)
        self.assertEqual(self.m.snapshot(), snapshot)

    def test_cancel_request_is_not_cancel_confirmation(self):
        ident = self.begin()
        self.m.update(ident, 'accepted', 0)
        self.assertTrue(self.m.request_cancel())
        self.assertFalse(self.m.request_cancel())
        self.m.update(ident, 'running', 1, progress=0.3)
        self.assertEqual(self.m.snapshot()['status'], 'cancel_requested')
        with self.assertRaises(ValueError):
            self.begin()
        self.m.update(ident, 'cancelled', 2)
        self.assertEqual(self.m.snapshot()['status'], 'cancelled')
        self.m.ensure_idle()

    def test_success_can_arrive_after_cancel_request(self):
        ident = self.begin()
        self.m.request_cancel()
        self.m.update(ident, 'succeeded', 1)
        self.assertEqual(self.m.snapshot()['status'], 'succeeded')
        self.assertTrue(self.m.snapshot()['cancel_requested'])

    def test_timeout_is_unknown_and_can_be_reconciled(self):
        ident = self.begin()
        self.now = 109
        self.assertFalse(self.m.expire())
        self.now = 110
        self.assertTrue(self.m.expire())
        self.assertEqual(self.m.snapshot()['status'], 'unknown')
        with self.assertRaises(ValueError):
            self.begin()
        self.m.request_cancel()
        self.assertEqual(self.m.snapshot()['status'], 'unknown')
        self.m.update(ident, 'succeeded', 0)
        self.assertEqual(self.m.snapshot()['status'], 'succeeded')
        self.assertFalse(self.m.expire())

    def test_snapshot_and_event_copies_cannot_modify_manager(self):
        ident = self.begin()
        snapshot = self.m.snapshot()
        snapshot['status'] = 'succeeded'
        self.m.events.get()[1]['status'] = 'failed'
        self.assertEqual(self.m.snapshot()['status'], 'starting')
        self.m.update(ident, 'succeeded', 0)
        self.begin()
        self.m.history()[0]['status'] = 'failed'
        self.assertEqual(self.m.history()[0]['status'], 'succeeded')

    def test_concurrent_starts_reserve_only_one_task(self):
        results = []
        barrier = threading.Barrier(3)
        def start():
            barrier.wait()
            try:
                results.append(self.begin())
            except ValueError:
                results.append(None)
        threads = [threading.Thread(target=start) for _ in range(2)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join(2)
        self.assertEqual(sum(value is not None for value in results), 1)


class EngineTaskTests(unittest.TestCase):
    def engine_with_plan(self):
        engine = Engine()
        engine.connect(PROFILE)
        engine.analyze(PROFILE, 'free', SETTINGS, '', offline=True)
        return engine

    def test_both_editions_complete_demo_without_api(self):
        for edition in ('free', 'paid'):
            with self.subTest(edition=edition), patch('copilot.ai.ask', side_effect=AssertionError('API forbidden')):
                engine = Engine()
                engine.connect(PROFILE)
                for _ in range(6):
                    engine.analyze(PROFILE, edition, SETTINGS, '', offline=True, auto=True)
                self.assertEqual(engine.adapter.observe()['state']['outcome'], '승리')
                self.assertEqual(engine.calls, 0)
                self.assertEqual(engine.tasks.snapshot()['status'], 'succeeded')
                self.assertEqual(len(engine.tasks.history()), 5)
                statuses = [value['status'] for kind, value in list(engine.events.queue) if kind == 'task']
                self.assertEqual(statuses, ['starting', 'accepted', 'succeeded'] * 6)

    def test_unknown_result_blocks_repeat_analysis_execution_and_connect(self):
        engine = self.engine_with_plan()
        pending = copy.deepcopy(engine.pending)
        with patch.object(engine.adapter, 'act', side_effect=TimeoutError) as act:
            with self.assertRaises(ValueError):
                engine.execute(True)
            self.assertEqual(engine.tasks.snapshot()['status'], 'unknown')
            with self.assertRaises(ValueError):
                engine.analyze(PROFILE, 'paid', SETTINGS, '', offline=True)
            with self.assertRaises(ValueError):
                engine.connect(PROFILE)
            engine.pending = pending
            with self.assertRaises(ValueError):
                engine.execute(True)
            act.assert_called_once()

    def test_mismatched_receipt_is_unknown(self):
        engine = self.engine_with_plan()
        with patch.object(engine.adapter, 'act', return_value={'request_id': 'other', 'accepted': True}):
            with self.assertRaises(ValueError):
                engine.execute(True)
        self.assertEqual(engine.tasks.snapshot()['status'], 'unknown')

    def test_stop_during_action_keeps_actual_success(self):
        engine = self.engine_with_plan()
        original = engine.adapter.act
        def act(*args):
            engine.stop()
            self.assertEqual(engine.tasks.snapshot()['status'], 'cancel_requested')
            return original(*args)
        with patch.object(engine.adapter, 'act', side_effect=act):
            with self.assertRaises(Cancelled):
                engine.execute(True)
        self.assertEqual(engine.tasks.snapshot()['status'], 'succeeded')
        self.assertEqual(engine.adapter.observe()['state']['turn'], 1)

    def test_stop_before_dispatch_confirms_local_cancellation(self):
        engine = self.engine_with_plan()
        original = engine.tasks.begin
        def begin(*args):
            result = original(*args)
            engine.stop()
            return result
        with patch.object(engine.tasks, 'begin', side_effect=begin), patch.object(engine.adapter, 'act') as act:
            with self.assertRaises(Cancelled):
                engine.execute(True)
            act.assert_not_called()
        self.assertEqual(engine.tasks.snapshot()['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
