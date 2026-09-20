"""Step 3 counter executor with an independent monotonic watchdog.

No movement, obstacle simulation, LLM, or real game integration (steps 4+).
"""
import copy
import math
import re
import threading
import time
import uuid
from .tasks import TERMINAL


class CancellableTaskDemo:
    game_id = 'cancel-test'

    def __init__(self, duration=20, lease_seconds=3, clock=time.monotonic, start_thread=True):
        if not 0 < duration <= 30 or not 2 <= lease_seconds <= 5:
            raise ValueError('시험 작업 시간 범위 오류')
        self.duration, self.lease_seconds, self.clock = duration, lease_seconds, clock
        self.session = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.records = {}
        self.active = None
        self.counter = 0
        self.controls_active = False
        self.thread = None
        if start_thread:
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def _loop(self):
        while not self.closed.wait(0.05):
            self.tick()

    @staticmethod
    def _ids(task_id, owner):
        for value in (task_id, owner):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
                raise ValueError('작업/제어자 ID 형식 오류')

    def _get(self, task_id, owner):
        self._ids(task_id, owner)
        record = self.records.get(task_id)
        if not record or record['controller_id'] != owner:
            raise ValueError('작업 또는 제어자가 일치하지 않습니다.')
        return record

    @staticmethod
    def _public(record):
        return {k: copy.deepcopy(v) for k, v in record.items() if not k.startswith('_')}

    def _finish(self, status, message):
        if self.active:
            record = self.records[self.active]
            record.update(status=status, message=message, sequence=record['sequence'] + 1)
            if status == 'succeeded':
                record['progress'] = 1.0
            self.active = None
        self.controls_active = False

    def _watchdog(self):
        if self.active:
            record = self.records[self.active]
            now = self.clock()
            if self.closed.is_set():
                self._finish('cancelled', '연결 모듈 종료 — 작업 중단')
            elif now >= record['_lease_until']:
                self._finish('cancelled', '연결 유지 신호 만료 — 모듈이 독립적으로 중단')
            elif now >= record['_deadline']:
                self._finish('failed', '작업 최대시간 초과 — 모듈이 중단')

    def tick(self):
        with self.lock:
            self._watchdog()
            if not self.active:
                return
            r = self.records[self.active]
            self.controls_active = True
            self.counter += 1
            r.update(sequence=r['sequence'] + 1, status='running',
                     progress=min(1.0, (self.clock() - r['_started']) / self.duration), message='가상 카운터 작업 진행 중')
            if r['progress'] >= 1:
                self._finish('succeeded', '가상 카운터 작업 완료')

    def observe(self):
        with self.lock:
            self._watchdog()
            ready = not self.closed.is_set() and self.active is None
            return {'schema_version': 1, 'game_id': self.game_id, 'revision': self.session,
                    'terminal': False, 'can_act': ready,
                    'capabilities': {'version': 1, 'execution_mode': 'turn_based', 'read_only': False,
                        'supports_tasks': True, 'supports_cancel': True, 'task_protocol': 1,
                        'lease_seconds': self.lease_seconds},
                    'state': {'notice': '3번 중지/통신 시험용 카운터. 실제 게임 아님.',
                              'counter': self.counter, 'controls_active': self.controls_active},
                    'actions': [{'id': 'count', 'label': '취소 가능한 가상 카운터 작업', 'risk': 'low'}] if ready else []}

    def start_task(self, task, owner, expires_at=None, max_seconds=30):
        task_id = task['task_id']
        self._ids(task_id, owner)
        with self.lock:
            self._watchdog()
            if task_id in self.records:
                record = self._get(task_id, owner)
                if record.get('action_id') not in (None, task['action_id']):
                    raise ValueError('동일 ID를 다른 행동에 재사용할 수 없습니다.')
                return self._public(record)
            if len(self.records) >= 10000:
                raise ValueError('시험 세션 기록 한도 도달')
            if expires_at is not None and not time.time() < expires_at <= time.time() + 3:
                raise ValueError('시작 요청 유효기간 만료')
            if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or not 0 < max_seconds <= 30:
                raise ValueError('작업 시간 제한 오류')
            if self.closed.is_set() or self.active or task['revision'] != self.session or task['action_id'] != 'count' or task['game_id'] != self.game_id:
                raise ValueError('현재 상태에서 시작할 수 없는 작업입니다.')
            now = self.clock()
            self.records[task_id] = {'task_id': task_id, 'controller_id': owner, 'game_id': self.game_id,
                'action_id': 'count', 'status': 'accepted', 'sequence': 0, 'progress': 0.0, 'message': '작업 접수 (완료 아님)',
                '_started': now, '_lease_until': now + self.lease_seconds, '_deadline': now + max_seconds}
            self.active = task_id
            return self._public(self.records[task_id])

    def task_status(self, task_id, owner):
        with self.lock:
            self._watchdog()
            return self._public(self._get(task_id, owner))

    def heartbeat(self, task_id, owner):
        with self.lock:
            self._watchdog()  # Check expiry BEFORE renewal: late heartbeats cannot revive work.
            record = self._get(task_id, owner)
            if record['status'] not in TERMINAL:
                record['_lease_until'] = self.clock() + self.lease_seconds
            return self._public(record)

    def cancel_task(self, task_id, owner):
        with self.lock:
            self._ids(task_id, owner)
            if task_id not in self.records:
                if len(self.records) >= 10000:
                    raise ValueError('시험 세션 기록 한도')
                # Remember cancellations that arrive before a delayed start request.
                self.records[task_id] = {'task_id': task_id, 'controller_id': owner, 'game_id': self.game_id,
                    'action_id': None, 'status': 'cancelled', 'sequence': 0, 'progress': 0.0, 'message': '시작 전 취소 확인'}
            record = self._get(task_id, owner)
            if record['status'] not in TERMINAL:
                self._finish('cancelled', '본체 중지 요청 — 모듈이 중단 확인')
            return self._public(record)

    def close(self):
        self.closed.set()
        with self.lock:
            self._finish('cancelled', '연결 모듈 종료 — 작업 중단')
        if self.thread:
            self.thread.join(timeout=1)
