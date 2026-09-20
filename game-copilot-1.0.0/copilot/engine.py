"""Single-flight decisions; cancellation, stale-state checks, bounded delegation."""
import copy
import queue
import threading
import time
from . import ai
from .tasks import TaskManager
from .task_control import TaskControl
from .capabilities import capability_summary, execution_block_reason, normalize_capabilities


class Cancelled(Exception):
    pass


class Engine:
    def __init__(self, events=None):
        self.events = events or queue.Queue()
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.network_lock = threading.Lock()
        self.busy = False
        self.calls = 0
        self.pending = None
        self.adapter = None
        self.phase = '대기'
        self.started = None
        self.job_id = 0
        self.tasks = TaskManager(self.events)
        self.task_control = TaskControl(self.tasks, self.cancelled, self.emit)
        self.cancel_demo = None

    def emit(self, kind, value):
        self.events.put((kind, value))

    def phase_set(self, text):
        self.phase = text
        self.emit('log', text)

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def stop(self):
        self.cancelled.set()
        self.tasks.request_cancel()
        self.task_control.cancel_async()
        self.pending = None
        self.emit('log', '중지 요청: 후속 행동 차단 및 지원되는 장기 작업의 원격 취소 요청. 실제 중단은 작업 로그에서 확인하세요.')

    def launch(self, job, *args):
        with self.lock:
            if self.busy:
                raise ValueError('현재 작업이 끝난 후 다시 시도하세요.')
            if self.network_lock.locked():
                raise ValueError('이전 API 연결이 정리 중입니다. 중복 호출을 막고 있습니다.')
            if self.task_control.cancel_busy.is_set():
                raise ValueError('원격 취소 요청을 확인하는 중입니다.')
            self.cancelled.clear()
            self.busy = True
            self.started = time.monotonic()
            self.job_id += 1
            job_id = self.job_id
        def run():
            try:
                job(*copy.deepcopy(args))
            except Cancelled:
                self.pending = None
                self.emit('log', '본체의 후속 실행을 중지했습니다. 이미 전달한 작업 결과는 작업 로그를 확인하세요.')
            except Exception as exc:
                self.pending = None
                # Do not include raw network response, keys, or repr(request) in diagnostics.
                safe = str(exc) if isinstance(exc, ValueError) else type(exc).__name__ + ': 작업 실패 (연결/설정 확인)'
                self.emit('error', safe)
            finally:
                if self.cancelled.is_set():
                    self.pending = None
                self.busy = False
                self.started = None
                self.phase = '대기'
                self.emit('done', job_id)
        threading.Thread(target=run, daemon=True).start()

    def connect(self, profile):
        from .adapters import make_adapter
        self.tasks.ensure_idle()
        self.pending = None
        self.adapter = None
        self.phase_set('게임 연결 확인')
        adapter = make_adapter(profile)
        state = adapter.observe()
        self.check()
        self.adapter = adapter
        self.emit('state', state)
        self.emit('log', '연결 완료: ' + state['game_id'])
        self.emit('log', capability_summary(state))
        reason = execution_block_reason(state)
        if reason:
            self.emit('log', reason)

    def observe(self):
        if self.adapter is None:
            raise ValueError('먼저 프로필 저장 → 연결을 누르세요.')
        self.check()
        self.phase_set('게임 상태 읽기')
        state = self.adapter.observe()
        self.check()
        self.emit('state', state)
        return state

    def call(self, edition, model, key, payload, timeout, limit):
        self.check()
        if not key:
            raise ValueError('먼저 API 키를 저장하세요.')
        if self.calls >= limit:
            raise ValueError('이번 실행의 API 요청 한도에 도달했습니다. 설정의 요청 한도를 확인하세요.')
        if not self.network_lock.acquire(blocking=False):
            raise ValueError('이전 API 요청이 아직 정리 중입니다.')
        self.calls += 1
        self.phase_set(f'API 응답 대기: {model} / 최대 {timeout}초 / 호출 {self.calls}회')
        result = queue.Queue()
        def request():
            try:
                result.put((True, ai.ask(edition, model, key, payload, timeout)))
            except Exception as exc:
                result.put((False, exc))
            finally:
                self.network_lock.release()
        threading.Thread(target=request, daemon=True).start()
        deadline = time.monotonic() + timeout
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('설정한 총 대기시간을 초과했습니다. 늦은 응답은 폐기하며 자동 재시도하지 않습니다.')
            try:
                ok, value = result.get(timeout=min(0.1, remaining))
                self.check()
                if time.monotonic() > deadline:
                    raise ValueError('설정한 총 대기시간을 초과한 응답은 폐기했습니다.')
                if not ok:
                    raise value
                self.emit('usage', value[1])
                return value[0]
            except queue.Empty:
                continue

    def analyze(self, profile, edition, settings, key, offline=False, auto=False, deep_enabled=True):
        self.pending = None
        self.tasks.ensure_idle()
        state = self.observe()
        observed = time.monotonic()
        if state['terminal']:
            self.emit('terminal', '게임/데모가 종료되어 멈췄습니다.')
            return
        payload = {'task': '현재 상태를 분석하고 다음 행동 하나를 제안하세요.',
                   'goal': profile['goal'], 'notes': profile['notes'],
                   'history': profile.get('history', [])[-8:], 'observation': state}
        if offline:
            if profile['adapter'] != 'demo':
                raise ValueError('API 없는 규칙 엔진은 내장 데모에서만 사용할 수 있습니다.')
            plan = ai.demo_plan(state)
        else:
            plan = self.call(edition, settings['fast'], key, payload, settings['fast_timeout'], settings['call_limit'])
            needs_deep = plan['need_deep'] or plan['confidence'] < 0.65
            if needs_deep and deep_enabled:
                # A fresh observation avoids spending a second call on an already obsolete state.
                fresh = self.observe()
                observed = time.monotonic()
                state = fresh
                payload.update(observation=fresh, task='심화 판단: 불확실성을 검토하고 안전한 다음 행동 하나만 제안하세요.', fast_advice=plan['advice'])
                if fresh['terminal']:
                    self.emit('terminal', '종료 상태가 확인되어 멈췄습니다.')
                    return
                plan = self.call(edition, settings['deep'], key, payload, settings['deep_timeout'], settings['call_limit'])
            elif needs_deep:
                plan['action_id'] = None
                plan['advice'] += '\n심화 판단이 필요하지만 꺼져 있어 실행을 보류했습니다.'
        self.check()
        action_id = plan.get('action_id')
        chosen = next((a for a in state['actions'] if a['id'] == action_id), None)
        if action_id is not None and chosen is None:
            raise ValueError('모델이 현재 허용 목록에 없는 행동을 제안했습니다. 실행하지 않습니다.')
        self.emit('plan', {'profile_id': profile['id'], 'plan': plan})
        reason = execution_block_reason(state)
        if reason:
            self.emit('halt' if auto else 'log', reason)
            return
        if chosen and state['can_act']:
            self.pending = {'action': chosen, 'revision': state['revision'],
                            'created': observed, 'confidence': plan['confidence'],
                            'need_deep': plan['need_deep']}
            if auto:
                if chosen['risk'] != 'low' or plan['confidence'] < 0.8 or plan['need_deep']:
                    self.emit('halt', '자동 실행 보류: 위험 행동 또는 불확실한 판단입니다. 내용을 확인하세요.')
                else:
                    self.execute(False)
        elif auto:
            self.emit('halt', '실행 가능한 행동이 없어 자동 실행을 멈췄습니다.')

    def execute(self, confirmed=False):
        self.check()
        self.tasks.ensure_idle()
        pending = self.pending
        self.pending = None  # Never repeat a timed-out/unknown acknowledgement.
        if pending is None:
            raise ValueError('실행할 제안이 없습니다. 먼저 분석하세요.')
        if time.monotonic() - pending['created'] > 90:
            raise ValueError('90초 이상 지난 판단입니다. 다시 분석하세요.')
        if not confirmed and (pending['action']['risk'] != 'low' or pending['confidence'] < 0.8 or pending['need_deep']):
            raise ValueError('자동 실행 조건을 충족하지 못했습니다.')
        fresh = self.observe()
        reason = execution_block_reason(fresh)
        if reason:
            raise ValueError(reason)
        if (fresh['revision'] != pending['revision'] or not fresh['can_act'] or fresh['terminal']
                or pending['action'] not in fresh['actions']):
            raise ValueError('분석 이후 게임 상태/가능한 행동이 바뀌었습니다. 다시 분석하세요.')
        self.check()
        self.phase_set('행동 전달 및 실행 확인')
        if normalize_capabilities(fresh).get('task_protocol') == 1:
            self.phase_set('작업 접수·진행 감시 / 유지 신호 전송')
            result = self.task_control.run(self.adapter, fresh, pending['action']['id'])
            if result['status'] != 'succeeded':
                self.emit('halt', '작업 종료/중단: ' + result['status'] + '. 실제 결과를 작업 로그에서 확인하세요.')
            else:
                self.emit('log', '장기 작업 성공 확인')
            return
        task = self.tasks.begin(fresh['game_id'], pending['action']['id'], pending['revision'])
        task_id = task['task_id']
        try:
            # Check again after reserving a task. A stop before dispatch has no game effect.
            self.check()
        except Cancelled:
            self.tasks.update(task_id, 'cancelled', 0, message='게임에 전달하기 전에 중지했습니다.')
            raise
        try:
            receipt = self.adapter.act(pending['action']['id'], pending['revision'], task_id)
            if (not isinstance(receipt, dict) or receipt.get('request_id') != task_id
                    or receipt.get('accepted') is not True):
                raise ValueError('작업의 실행 확인 응답이 올바르지 않습니다.')
        except Exception:
            # A transport/parse error is not proof that the game did nothing.
            self.tasks.mark_unknown(task_id)
            raise ValueError('작업 결과 불명: 새 행동을 차단했습니다. 게임에서 결과를 확인하세요. 자동 재전송하지 않습니다.') from None
        self.tasks.update(task_id, 'accepted', 0, message='v1 실행 확인 응답을 받았습니다.')
        # v1/action explicitly means applied, not merely queued. Future task ACKs
        # must not take this path: they will remain accepted until a result arrives.
        self.tasks.update(task_id, 'succeeded', 1, message=str(receipt.get('message', '완료'))[:300])
        self.emit('log', '실행 확인: ' + str(receipt.get('message', '완료'))[:300])
        self.check()
        after = self.observe()
        if after['terminal']:
            self.emit('terminal', '게임/데모 종료: ' + str(after['state'].get('outcome', '종료')))

    def generate_goal(self, profile, edition, settings, key):
        plan = self.call(edition, settings['fast'], key, {
            'task': '게임 이름과 사용자 메모로 기본 목표와 승리 방식을 간단히 설명하세요. 인터넷 검색 아님. 모르면 모른다고 하세요.',
            'game': profile['name'], 'notes': profile['notes']}, settings['fast_timeout'], settings['call_limit'])
        self.emit('goal', {'profile_id': profile['id'], 'text': plan['advice']})

    def start_cancel_demo(self):
        """Stage 3 diagnostic only; leaves profiles, game adapter and API keys untouched."""
        from .task_demo import CancellableTaskDemo
        self.tasks.ensure_idle()
        self.check()
        if self.cancel_demo:
            self.cancel_demo.close()
        self.cancel_demo = CancellableTaskDemo()
        self.pending = None
        self.phase_set('3번 중지 시험 (API·실제 게임 미사용)')
        self.emit('log', '가상 카운터가 최대 20초 진행됩니다. 중지 또는 시험 통신 끊기를 눌러 보세요.')
        self.task_control.run(self.cancel_demo, self.cancel_demo.observe(), 'count')

    def reconcile_task(self):
        result = self.task_control.reconcile()
        if self.cancel_demo and self.task_control.context['adapter'] is self.cancel_demo:
            snapshot = self.cancel_demo.observe()['state']
            self.emit('log', f"시험 카운터: {snapshot['counter']} / 조작 유지: {snapshot['controls_active']}")
        return result

    def test_api(self, edition, settings, key, role='fast'):
        self.call(edition, settings[role], key, {'task': '연결 시험입니다. advice에 연결 성공을 넣으세요.'},
                  settings[role + '_timeout'], settings['call_limit'])
        self.emit('log', role + ' 모델 API 인증·응답 형식 확인 성공')
