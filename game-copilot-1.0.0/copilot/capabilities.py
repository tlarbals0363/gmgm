"""Optional v1 capability advertisement; does not implement task execution."""
import math


def normalize_capabilities(state):
    """Absent metadata preserves v1 behavior, without guessing a game's timing."""
    if 'capabilities' not in state:
        return {
            'version': 1, 'execution_mode': 'unknown', 'read_only': False,
            'supports_tasks': False, 'supports_cancel': False,
        }
    caps = state['capabilities']
    if not isinstance(caps, dict) or type(caps.get('version')) is not int or caps['version'] != 1:
        raise ValueError('연결 기능 정보 capabilities.version은 1이어야 합니다.')
    if caps.get('execution_mode') not in ('turn_based', 'realtime', 'unknown'):
        raise ValueError('연결 실행 방식은 turn_based/realtime/unknown 중 하나여야 합니다.')
    for field in ('read_only', 'supports_tasks', 'supports_cancel'):
        if type(caps.get(field)) is not bool:
            raise ValueError('연결 기능 정보의 ' + field + '는 true/false여야 합니다.')
    if caps['supports_cancel'] and not caps['supports_tasks']:
        raise ValueError('작업 취소 지원에는 장기 작업 지원이 필요합니다.')
    if caps['read_only'] and (state.get('can_act') or state.get('actions')):
        raise ValueError('관측 전용 연결은 can_act:false, actions:[]이어야 합니다.')
    result = {field: caps[field] for field in (
        'version', 'execution_mode', 'read_only', 'supports_tasks', 'supports_cancel')}
    if 'task_protocol' in caps:
        if type(caps['task_protocol']) is not int or caps['task_protocol'] != 1:
            raise ValueError('지원하지 않는 작업 통신 규격입니다.')
        if not caps['supports_tasks'] or not caps['supports_cancel']:
            raise ValueError('작업 통신에는 작업 실행과 취소 지원이 필요합니다.')
        lease = caps.get('lease_seconds')
        if type(lease) not in (float, int) or not math.isfinite(lease) or not 2 <= lease <= 5:
            raise ValueError('작업 유지 제한 lease_seconds는 2~5초여야 합니다.')
        result.update(task_protocol=1, lease_seconds=lease)
    return result


def execution_block_reason(state):
    caps = normalize_capabilities(state)
    if caps['read_only']:
        return '관측 전용 연결입니다. 조언만 제공하며 행동은 실행하지 않습니다.'
    if caps['execution_mode'] == 'realtime':
        return '실시간 연결은 이번 패치에서 조언 전용입니다. 실시간 작업 실행기는 후속 단계에서 추가됩니다.'
    if caps['supports_tasks'] and caps.get('task_protocol') != 1:
        return '장기 작업 선언은 있으나 3단계 작업 통신/중지 규격이 없습니다. 단발 실행으로 대신 보내지 않습니다.'
    return ''


def capability_summary(state):
    caps = normalize_capabilities(state)
    mode = {'turn_based': '턴제', 'realtime': '실시간', 'unknown': '미지정 (기존 v1 방식)'}[caps['execution_mode']]
    yesno = lambda value: '지원' if value else '미지원'
    return (f"연결 기능: {mode} / 관측 전용: {'예' if caps['read_only'] else '아니오'}"
            f" / 장기 작업: {yesno(caps['supports_tasks'])} / 작업 취소: {yesno(caps['supports_cancel'])}"
            + (' / 작업 통신·유지 제한: 지원' if caps.get('task_protocol') == 1 else ' / 작업 통신·유지 제한: 미지원'))
