"""Step 3 transport control over the existing step 2 TaskManager.

Never retries start. A local stop or elapsed lease is not proof of remote stop.
All completion reports must match the controller, game and task IDs.
"""
import threading
import time
import uuid
from .tasks import TERMINAL


class ConnectionLost(Exception):
    pass


class TaskControl:
    def __init__(self, manager, stop_event, emit):
        self.manager, self.stop_event, self.emit = manager, stop_event, emit
        self.controller_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.cancel_busy = threading.Event()
        self.context = None
        self.connected = True

    def _apply(self, context, report):
        task = context['task']
        current = self.manager.snapshot()
        if not current or current['task_id'] != task['task_id']:
            raise ValueError('현재 작업과 다른 작업의 확인 응답입니다.')
        if (not isinstance(report, dict) or report.get('task_id') != task['task_id']
                or report.get('controller_id') != self.controller_id
                or report.get('game_id') != task['game_id']):
            raise ValueError('작업 확인 응답의 작업/게임/제어자 ID가 일치하지 않습니다.')
        self.manager.update(task['task_id'], report.get('status'), report.get('sequence'),
                            report.get('progress'), report.get('message', ''))
        return self.manager.snapshot()

    def _cancel(self, context):
        try:
            report = context['adapter'].cancel_task(context['task']['task_id'], self.controller_id)
            current = self._apply(context, report)
            if current['status'] not in TERMINAL:
                self.manager.mark_unknown(current['task_id'], '취소 응답을 받았으나 중단은 확인되지 않았습니다.')
            return current
        except Exception:
            self.manager.mark_unknown(context['task']['task_id'],
                '원격 취소 확인 실패. 유지 신호 중단; 모듈의 연결 만료 중지가 필요합니다. 결과 확인 전 새 실행을 막습니다.')
            return None

    def cancel_async(self):
        """Called by the UI stop button, including while start/status is in flight."""
        self.manager.request_cancel()
        with self.lock:
            context = self.context
            current = self.manager.snapshot()
            if (not context or not current or current['task_id'] != context['task']['task_id']
                    or current['status'] in TERMINAL or self.cancel_busy.is_set()):
                return
            self.cancel_busy.set()
        def cancel():
            try:
                self._cancel(context)
            finally:
                self.cancel_busy.clear()
        threading.Thread(target=cancel, daemon=True).start()

    def drop_demo_connection(self):
        """Fault injection is strictly limited to the internal counter demo."""
        from .task_demo import CancellableTaskDemo
        with self.lock:
            if not self.context or not isinstance(self.context['adapter'], CancellableTaskDemo):
                raise ValueError('통신 끊김 시험은 3번 내부 시험 작업에서만 가능합니다.')
            task = self.manager.snapshot()
            if not task or task['task_id'] != self.context['task']['task_id'] or task['status'] in TERMINAL:
                raise ValueError('진행 중인 3번 시험 작업이 없습니다.')
            self.connected = False
            self.manager.mark_unknown(task['task_id'], '시험: 유지 신호/상태 조회 차단. 3초 후 모듈이 중단해야 합니다. 이후 작업 결과 확인을 누르세요.')

    def reconcile(self):
        """Read-only recovery. Does not renew leases or resend a start request."""
        with self.lock:
            context = self.context
            if not context:
                raise ValueError('확인할 장기 작업이 없습니다. 기존 v1 단발 작업에는 이 조회 규격이 없습니다.')
            current = self.manager.snapshot()
            if not current or current['task_id'] != context['task']['task_id']:
                raise ValueError('현재 작업은 보관된 장기 작업과 다릅니다. 이전 작업 응답으로 결과를 확정하지 않습니다.')
        try:
            result = self._apply(context, context['adapter'].task_status(context['task']['task_id'], self.controller_id))
        except Exception:
            self.manager.mark_unknown(context['task']['task_id'], '작업 상태 조회 실패. 중단/완료를 확인하지 못했습니다.')
            raise ValueError('작업 결과를 확인할 수 없습니다. 통신 복구 후 다시 확인하세요.') from None
        self.emit('log', '작업 결과 조회: ' + result['status'] + ' (시작 재전송/유지 신호 없음)')
        return result

    def run(self, adapter, state, action_id):
        self.manager.ensure_idle()
        if self.cancel_busy.is_set():
            raise ValueError('이전 취소 요청이 아직 정리 중입니다.')
        # Reserve and bind atomically with respect to cancel_async.
        with self.lock:
            task = self.manager.begin(state['game_id'], action_id, state['revision'], timeout=30)
            context = {'adapter': adapter, 'task': task}
            self.context = context
            self.connected = True
        if self.stop_event.is_set() or self.manager.snapshot()['cancel_requested']:
            self.manager.update(task['task_id'], 'cancelled', 0, message='연결 모듈에 전달하기 전에 중지했습니다.')
            return self.manager.snapshot()
        lease = state['capabilities']['lease_seconds']
        heartbeat_at = time.monotonic() + lease / 3
        try:
            result = self._apply(context, adapter.start_task(task, self.controller_id))
            while result['status'] not in TERMINAL:
                if not self.connected:
                    raise ConnectionLost()
                if self.stop_event.is_set() or result['cancel_requested']:
                    self.manager.request_cancel()
                    self._cancel(context)
                    return self.manager.snapshot()
                if self.manager.expire():
                    self.manager.request_cancel()
                    self._cancel(context)
                    return self.manager.snapshot()
                self.stop_event.wait(0.15)
                if self.stop_event.is_set():
                    continue
                if not self.connected:
                    raise ConnectionLost()
                if time.monotonic() >= heartbeat_at:
                    report = adapter.heartbeat(task['task_id'], self.controller_id)
                    heartbeat_at = time.monotonic() + lease / 3
                else:
                    report = adapter.task_status(task['task_id'], self.controller_id)
                result = self._apply(context, report)
            return result
        except ConnectionLost:
            # Deliberately send no cancellation or heartbeat: exercise the independent watchdog.
            raise ValueError('시험 연결이 끊겼습니다. 모듈의 자동 중단 후 작업 결과 확인을 누르세요.') from None
        except Exception:
            self.manager.mark_unknown(task['task_id'], '작업 통신 오류. 시작을 재전송하지 않으며 원격 취소를 요청합니다.')
            self.manager.request_cancel()
            self._cancel(context)
            raise ValueError('작업 통신 오류가 발생했습니다. 실제 결과는 작업 로그를 확인하세요.') from None
