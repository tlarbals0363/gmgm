"""Run from project folder: python -m examples.cancel_disconnect_demo"""
import threading
import time
from copilot.engine import Engine


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('condition timeout')
        threading.Event().wait(0.02)


def main():
    engine = Engine()
    try:
        print('1. Start counter -> stop -> confirm actual cancellation (no API).')
        engine.launch(engine.start_cancel_demo)
        wait_until(lambda: engine.cancel_demo is not None and engine.cancel_demo.counter >= 3)
        engine.stop()
        wait_until(lambda: not engine.busy and not engine.task_control.cancel_busy.is_set())
        assert engine.tasks.snapshot()['status'] == 'cancelled'
        assert engine.cancel_demo.controls_active is False
        print('PASS: stop acknowledged; local counter stopped.')

        print('2. Start counter -> stop heartbeats/status reads -> independent 3s watchdog.')
        engine.launch(engine.start_cancel_demo)
        wait_until(lambda: engine.tasks.snapshot()['status'] == 'running')
        engine.task_control.drop_demo_connection()
        wait_until(lambda: not engine.busy)
        assert engine.tasks.snapshot()['status'] == 'unknown'
        # Check active directly: do not use status/observe to drive watchdog execution.
        wait_until(lambda: engine.cancel_demo.active is None)
        count = engine.cancel_demo.counter
        assert not engine.cancel_demo.controls_active
        result = engine.reconcile_task()
        assert result['status'] == 'cancelled'
        assert '만료' in result['message']
        print(f'PASS: lease expiry stopped independently at count {count}; read-only confirmation succeeded.')
    finally:
        engine.stop()
        if engine.cancel_demo:
            engine.cancel_demo.close()


if __name__ == '__main__':
    main()
