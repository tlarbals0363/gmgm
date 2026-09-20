"""Run with: python -m examples.task_lifecycle_demo (no API or real game)."""
from copilot.tasks import TaskManager, LABELS


def main():
    now = [0.0]
    tasks = TaskManager(clock=lambda: now[0])
    def show():
        while not tasks.events.empty():
            _, task = tasks.events.get_nowait()
            print(task['action_id'], LABELS[task['status']], task['progress'], task['message'])
    print('모의 작업 수명 주기입니다. 실제 이동/채집/원격 취소는 수행하지 않습니다.')
    task = tasks.begin('simulation', '이동 예시', 'session:1')['task_id']
    tasks.update(task, 'accepted', 0)
    tasks.update(task, 'running', 1, progress=0.5)
    tasks.update(task, 'succeeded', 2)
    show()
    task = tasks.begin('simulation', '실패 예시', 'session:2')['task_id']
    tasks.update(task, 'failed', 0, message='모의 대상 없음')
    show()
    task = tasks.begin('simulation', '취소 예시', 'session:3')['task_id']
    tasks.update(task, 'running', 0, progress=0.2)
    tasks.request_cancel()
    show()
    tasks.update(task, 'cancelled', 1, message='모의 실행기가 중단을 확인함')
    show()
    task = tasks.begin('simulation', '결과 불명 예시', 'session:4', timeout=5)['task_id']
    now[0] = 6
    tasks.expire()
    show()
    try:
        tasks.begin('simulation', '중복 작업', 'session:4')
    except ValueError as exc:
        print('예상된 차단:', exc)
    tasks.update(task, 'succeeded', 0, message='모의 실행 결과를 늦게 확인함')
    show()


if __name__ == '__main__':
    main()
