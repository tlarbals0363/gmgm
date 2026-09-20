"""Step 1 regression tests. No provider calls or real credentials."""
import copy
import unittest
from unittest.mock import Mock, patch
from copilot.adapters import DemoAdapter, BridgeAdapter, validate_state
from copilot.capabilities import normalize_capabilities, execution_block_reason
from copilot.engine import Engine
from test_core import PLAN, SETTINGS


class CapabilityTests(unittest.TestCase):
    def state(self, **caps):
        state = DemoAdapter().observe()
        state['capabilities'].update(caps)
        return state

    def test_legacy_state_keeps_original_action_semantics(self):
        original = self.state()
        del original['capabilities']
        normalized = validate_state(original, 'demo-colony')
        self.assertEqual(normalized['capabilities']['execution_mode'], 'unknown')
        self.assertFalse(normalized['capabilities']['supports_tasks'])
        self.assertNotIn('capabilities', original)
        self.assertEqual(normalized['actions'], original['actions'])
        self.assertEqual(execution_block_reason(normalized), '')

    def test_temporarily_unavailable_is_not_read_only(self):
        state = self.state()
        del state['capabilities']
        state.update(can_act=False, actions=[])
        self.assertFalse(normalize_capabilities(state)['read_only'])

    def test_invalid_capabilities_rejected(self):
        for change in [{'version': 2}, {'version': True}, {'execution_mode': 'typo'},
                       {'read_only': 'false'}, {'supports_tasks': 1},
                       {'supports_cancel': True}, {'read_only': True}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_state(self.state(**change), 'demo-colony')
        for value in [None, [], {}, {'version': 1}]:
            state = self.state()
            state['capabilities'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_state(state, 'demo-colony')

    def test_read_only_and_future_task_advertisement(self):
        state = self.state(read_only=True)
        state.update(can_act=False, actions=[])
        self.assertTrue(validate_state(state, 'demo-colony')['capabilities']['read_only'])
        self.assertTrue(execution_block_reason(state))
        state = self.state(execution_mode='realtime', supports_tasks=True, supports_cancel=True)
        self.assertTrue(validate_state(state, 'demo-colony')['capabilities']['supports_cancel'])
        self.assertTrue(execution_block_reason(state))

    @patch('copilot.adapters.fetch_json')
    def test_bridge_accepts_legacy_and_extended_states(self, fetch):
        adapter = BridgeAdapter({'endpoint': 'http://127.0.0.1:8766', 'game_id': 'demo-colony', 'bridge_token': 'test'})
        legacy = self.state()
        del legacy['capabilities']
        fetch.return_value = legacy
        self.assertEqual(adapter.observe()['capabilities']['execution_mode'], 'unknown')
        fetch.return_value = self.state(execution_mode='realtime')
        self.assertEqual(adapter.observe()['capabilities']['execution_mode'], 'realtime')

    @patch('copilot.ai.ask', return_value=(PLAN, {}))
    def test_realtime_auto_generates_advice_without_acting(self, ask):
        engine = Engine()
        engine.adapter = Mock()
        engine.adapter.observe.return_value = self.state(execution_mode='realtime', supports_tasks=True)
        engine.analyze({'id': 'test', 'goal': '', 'notes': '', 'adapter': 'bridge'},
                       'paid', SETTINGS, 'test-only', auto=True)
        ask.assert_called_once()
        engine.adapter.act.assert_not_called()
        self.assertIsNone(engine.pending)
        events = list(engine.events.queue)
        self.assertTrue(any(kind == 'plan' for kind, value in events))
        self.assertTrue(any(kind == 'halt' for kind, value in events))

    @patch('copilot.ai.ask', return_value=({**PLAN, 'action_id': None}, {}))
    def test_read_only_still_allows_advice(self, ask):
        engine = Engine()
        engine.adapter = Mock()
        state = self.state(read_only=True)
        state.update(can_act=False, actions=[])
        engine.adapter.observe.return_value = state
        engine.analyze({'id': 'test', 'goal': '', 'notes': '', 'adapter': 'bridge'},
                       'paid', SETTINGS, 'test-only')
        ask.assert_called_once()
        engine.adapter.act.assert_not_called()
        self.assertIsNone(engine.pending)

    @patch('copilot.ai.ask', return_value=(PLAN, {}))
    def test_capability_change_blocks_manually_confirmed_action(self, ask):
        for changes in [{'execution_mode': 'realtime'}, {'read_only': True}]:
            engine = Engine()
            engine.adapter = Mock()
            initial = self.state()
            fresh = copy.deepcopy(initial)
            fresh['capabilities'].update(changes)
            if changes.get('read_only'):
                fresh.update(can_act=False, actions=[])
            engine.adapter.observe.side_effect = [initial, fresh]
            engine.analyze({'id': 'test', 'goal': '', 'notes': '', 'adapter': 'bridge'},
                           'paid', SETTINGS, 'test-only')
            self.assertIsNotNone(engine.pending)
            with self.assertRaises(ValueError):
                engine.execute(confirmed=True)
            engine.adapter.act.assert_not_called()
            self.assertIsNone(engine.pending)

    @patch('copilot.ai.ask', return_value=(PLAN, {}))
    def test_legacy_adapter_can_still_execute(self, ask):
        demo = DemoAdapter()
        original_observe = demo.observe
        def legacy_observe():
            state = original_observe()
            del state['capabilities']
            return state
        demo.observe = legacy_observe
        engine = Engine()
        engine.adapter = demo
        engine.analyze({'id': 'test', 'goal': '', 'notes': '', 'adapter': 'bridge'},
                       'paid', SETTINGS, 'test-only', auto=True)
        self.assertEqual(demo.observe()['state']['turn'], 1)


if __name__ == '__main__':
    unittest.main()
