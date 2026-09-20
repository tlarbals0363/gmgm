"""Session-local task lifecycle. No networking, automatic retries or remote cancellation."""
import copy
import math
import queue
import threading
import time
import uuid
from collections import deque

TERMINAL = frozenset({'succeeded', 'failed', 'cancelled'})
REMOTE = frozenset({'accepted', 'running', *TERMINAL})
LABELS = {'starting': '시작 요청', 'accepted': '접수됨', 'running': '진행 중',
          'cancel_requested': '취소 요청 (중단 확인 전)', 'unknown': '결과 불명',
          'succeeded': '성공', 'failed': '실패', 'cancelled': '취소 확인'}


class TaskManager:
    """One unresolved task at a time, across games. Copies prevent external mutation.

    Bridge updates carry a monotonically increasing per-task sequence. Local
    cancellation/timeout does not consume a bridge sequence. Unknown is NOT terminal.
    This module records cancellation intent; it cannot stop a game by itself.
    """
    def __init__(self, events=None, clock=time.monotonic):
        self.events = events if events is not None else queue.Queue()
        self.clock = clock
        self._lock = threading.RLock()
        self._current = None
        self._history = deque(maxlen=50)

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._current)

    def history(self):
        with self._lock:
            return copy.deepcopy(list(self._history))

    def ensure_idle(self):
        with self._lock:
            if self._current and self._current['status'] not in TERMINAL:
                raise ValueError('미완료 작업이 있습니다: ' + LABELS[self._current['status']]
                                 + '. 결과 확인 전에는 새 행동이나 재연결을 하지 않습니다.')

    def _emit(self):
        self.events.put(('task', copy.deepcopy(self._current)))

    def begin(self, game_id, action_id, revision, timeout=30):
        for name, value in [('game_id', game_id), ('action_id', action_id), ('revision', revision)]:
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ValueError('작업 ' + name + ' 값이 올바르지 않습니다.')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('작업 대기시간은 양수여야 합니다.')
        with self._lock:
            self.ensure_idle()
            if self._current:
                self._history.append(copy.deepcopy(self._current))
            now = self.clock()
            self._current = {'task_id': uuid.uuid4().hex, 'game_id': game_id,
                             'action_id': action_id, 'revision': revision,
                             'status': 'starting', 'sequence': -1, 'progress': None,
                             'cancel_requested': False, 'message': '',
                             'created_at': now, 'updated_at': now, 'deadline': now + timeout}
            self._emit()
            return copy.deepcopy(self._current)

    def update(self, task_id, status, sequence, progress=None, message=''):
        """Record a verified executor report, not a model prediction.

        Returns False for stale IDs/sequences and duplicate/late terminal reports.
        Sequence conflicts and invalid transitions raise without modifying the task.
        """
        if status not in REMOTE or type(sequence) is not int or sequence < 0:
            raise ValueError('작업 상태 또는 순서 번호가 올바르지 않습니다.')
        if progress is not None and (type(progress) not in (int, float)
                                    or not math.isfinite(progress) or not 0 <= progress <= 1):
            raise ValueError('진행률은 0~1 또는 null이어야 합니다.')
        if not isinstance(message, str):
            raise ValueError('작업 메시지는 문자열이어야 합니다.')
        with self._lock:
            task = self._current
            if task is None or task_id != task['task_id'] or sequence <= task['sequence']:
                return False
            if task['status'] in TERMINAL:
                return False
            if task['status'] == 'running' and status == 'accepted':
                raise ValueError('진행 중인 작업을 접수 상태로 되돌릴 수 없습니다.')
            if progress is not None and task['progress'] is not None and progress < task['progress']:
                raise ValueError('진행률을 되돌릴 수 없습니다.')
            # A cancellation request may race with a genuine success acknowledgement.
            new_status = 'cancel_requested' if task['cancel_requested'] and status not in TERMINAL else status
            task.update(status=new_status, sequence=sequence, updated_at=self.clock(), message=message[:300])
            if status == 'succeeded':
                task['progress'] = 1.0
            elif progress is not None:
                task['progress'] = progress
            self._emit()
            return True

    def request_cancel(self):
        with self._lock:
            task = self._current
            if task is None or task['status'] in TERMINAL or task['cancel_requested']:
                return False
            task['cancel_requested'] = True
            # Preserve uncertainty; an intent is not proof of a running or stopped task.
            if task['status'] != 'unknown':
                task['status'] = 'cancel_requested'
            task.update(updated_at=self.clock(), message='취소 의도만 기록됨. 원격 중단은 아직 확인되지 않았습니다.')
            self._emit()
            return True

    def mark_unknown(self, task_id, message='실행 결과를 확인하지 못했습니다. 자동 재전송하지 않습니다.'):
        with self._lock:
            task = self._current
            if task is None or task_id != task['task_id'] or task['status'] in TERMINAL:
                return False
            if task['status'] == 'unknown':
                return False
            task.update(status='unknown', updated_at=self.clock(), message=message[:300])
            self._emit()
            return True

    def expire(self):
        """Called by the executor's polling loop; no background timer is installed."""
        with self._lock:
            task = self._current
            if task and task['status'] not in TERMINAL and self.clock() >= task['deadline']:
                return self.mark_unknown(task['task_id'], '대기시간 초과. 게임 작업의 실패나 중단을 뜻하지 않습니다.')
            return False
