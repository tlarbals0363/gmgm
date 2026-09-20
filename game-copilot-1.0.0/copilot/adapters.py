"""Game adapters expose state and finite, concrete allowed actions, not arbitrary code."""
import copy
import json
import threading
import time
import urllib.request
import urllib.parse
import uuid
from .capabilities import normalize_capabilities


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('리디렉션은 허용하지 않습니다.')


def fetch_json(url, body=None, token='', timeout=5, local=False):
    if local:
        p = urllib.parse.urlsplit(url)
        if p.scheme != 'http' or p.hostname != '127.0.0.1' or p.username or p.password or p.query or p.fragment:
            raise ValueError('게임 브리지는 http://127.0.0.1:포트 주소만 지원합니다.')
    handlers = [NoRedirect()]
    if local:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers)
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError('응답이 1MB 제한을 초과했습니다.')
    return json.loads(raw)


def validate_state(state, game_id):
    if not isinstance(state, dict) or state.get('schema_version') != 1 or state.get('game_id') != game_id:
        raise ValueError('브리지 게임 ID 또는 규격 버전이 일치하지 않습니다.')
    if not isinstance(state.get('revision'), str) or not state['revision']:
        raise ValueError('상태 revision이 없습니다.')
    if type(state.get('terminal')) is not bool or not isinstance(state.get('state'), dict):
        raise ValueError('상태/종료 필드가 올바르지 않습니다.')
    if type(state.get('can_act')) is not bool or not isinstance(state.get('actions'), list):
        raise ValueError('행동 지원 정보가 올바르지 않습니다.')
    ids = set()
    for action in state['actions']:
        if (not isinstance(action, dict) or not isinstance(action.get('id'), str)
                or not action['id'] or action['id'] in ids
                or action.get('risk') not in ('low', 'high')
                or not isinstance(action.get('label'), str)):
            raise ValueError('허용 행동 목록이 올바르지 않습니다.')
        ids.add(action['id'])
    if len(ids) > 200:
        raise ValueError('행동 후보는 한 상태당 최대 200개입니다.')
    return {**state, 'capabilities': normalize_capabilities(state)}


class DemoAdapter:
    def __init__(self):
        self.lock = threading.Lock()
        self.session = uuid.uuid4().hex
        self.turn, self.food, self.science = 0, 4, 0
        self.receipts = {}

    def observe(self):
        with self.lock:
            terminal = self.science >= 10 or self.food <= 0
            return {'schema_version': 1, 'game_id': 'demo-colony',
                    'capabilities': {'version': 1, 'execution_mode': 'turn_based',
                                     'read_only': False, 'supports_tasks': False, 'supports_cancel': False},
                    'revision': f'{self.session}:{self.turn}', 'can_act': True,
                    'terminal': terminal, 'state': {
                        'turn': self.turn, 'food': self.food, 'science': self.science,
                        'rules': '수확: 식량 +3. 연구: 식량 -1, 연구 +2. 연구 10이면 승리, 식량 0이면 패배.',
                        'outcome': '승리' if self.science >= 10 else ('패배' if self.food <= 0 else '진행 중')},
                    'actions': [] if terminal else [
                        {'id': 'harvest', 'label': '식량 수확 (+3)', 'risk': 'low'},
                        {'id': 'research', 'label': '연구 (식량 -1 / 연구 +2)', 'risk': 'low'}]}

    def act(self, action_id, revision, request_id):
        with self.lock:
            if request_id in self.receipts:
                return copy.deepcopy(self.receipts[request_id])
            if revision != f'{self.session}:{self.turn}':
                raise ValueError('상태가 바뀌어 행동을 취소했습니다.')
            if self.science >= 10 or self.food <= 0:
                raise ValueError('데모가 종료되었습니다. 다시 연결하면 새 데모가 시작됩니다.')
            if action_id == 'harvest':
                self.food += 3
            elif action_id == 'research':
                self.food -= 1
                self.science += 2
            else:
                raise ValueError('허용되지 않은 행동입니다.')
            self.turn += 1
            receipt = {'request_id': request_id, 'accepted': True,
                       'revision': f'{self.session}:{self.turn}', 'message': action_id + ' 실행 완료'}
            self.receipts[request_id] = receipt
            return copy.deepcopy(receipt)


class BridgeAdapter:
    def __init__(self, profile):
        self.url = profile['endpoint'].rstrip('/')
        p = urllib.parse.urlsplit(self.url)
        if p.scheme != 'http' or p.hostname != '127.0.0.1' or p.path or p.query or p.fragment or p.username or p.password:
            raise ValueError('브리지 주소 예: http://127.0.0.1:8766 (경로 없이)')
        self.game_id = profile['game_id']
        self.token = profile['bridge_token']
        if not self.game_id or not self.token:
            raise ValueError('게임 ID와 브리지 토큰을 입력하세요. 토큰은 API 키와 다릅니다.')

    def observe(self):
        return validate_state(fetch_json(self.url + '/v1/state', token=self.token, local=True), self.game_id)

    def start_task(self, task, controller_id):
        return self._task('start', task['task_id'], controller_id, {
            'action_id': task['action_id'], 'expected_revision': task['revision'],
            'expires_at': time.time() + 2, 'max_seconds': 30})

    def task_status(self, task_id, controller_id):
        return self._task('status', task_id, controller_id)

    def heartbeat(self, task_id, controller_id):
        return self._task('heartbeat', task_id, controller_id)

    def cancel_task(self, task_id, controller_id):
        return self._task('cancel', task_id, controller_id)

    def _task(self, operation, task_id, controller_id, extra=None):
        return fetch_json(self.url + '/v1/tasks/' + operation, {
            'schema_version': 1, 'game_id': self.game_id, 'task_id': task_id,
            'controller_id': controller_id, **(extra or {})}, self.token, timeout=1, local=True)

    def act(self, action_id, revision, request_id):
        result = fetch_json(self.url + '/v1/action', {
            'schema_version': 1, 'game_id': self.game_id, 'action_id': action_id,
            'expected_revision': revision, 'request_id': request_id,
            'expires_at': time.time() + 4}, self.token, local=True)
        if result.get('request_id') != request_id or result.get('accepted') is not True:
            raise ValueError('행동이 거절되었거나 실행 확인을 받지 못했습니다. 자동 재시도하지 않습니다.')
        return result


def make_adapter(profile):
    if profile['adapter'] == 'demo':
        return DemoAdapter()
    if profile['adapter'] == 'bridge':
        return BridgeAdapter(profile)
    raise ValueError('연결 모듈이 아직 없습니다. 프로필 이름만으로 실제 게임에 연결되지는 않습니다.')
