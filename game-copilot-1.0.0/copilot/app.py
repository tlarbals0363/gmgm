import argparse
import json
import os
import queue
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from . import __version__
from .storage import Store
from .engine import Engine
from .tasks import LABELS


class App:
    def __init__(self, root, edition, store):
        self.root, self.edition, self.store = root, edition, store
        self.engine = Engine()
        self.active = None
        self.auto = False
        self.timer = None
        self.logs = []
        self.closing = False
        root.title('Game Copilot ' + __version__ + ' — ' + ('무료 NVIDIA' if edition == 'free' else '유료 OpenAI'))
        root.geometry('1100x820')
        root.minsize(900, 660)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Escape>', lambda _: self.stop())
        style = ttk.Style()
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure('TButton', padding=6)
        style.configure('TLabel', padding=3)
        header = ttk.Frame(root, padding=12)
        header.pack(fill='x')
        ttk.Label(header, text='GAME COPILOT ' + __version__, font=('Segoe UI', 18, 'bold')).pack(side='left')
        ttk.Label(header, text='구조화된 게임 상태 · 다중 게임 연결 기반').pack(side='left', padx=16)
        ttk.Button(header, text='■ 중지 (이 창에서 Esc)', command=self.stop).pack(side='right')
        tabs = ttk.Notebook(root)
        tabs.pack(fill='both', expand=True, padx=12)
        main = ttk.Frame(tabs, padding=10)
        settings = ttk.Frame(tabs, padding=15)
        help_tab = ttk.Frame(tabs, padding=15)
        tabs.add(main, text='게임 / 코치')
        tabs.add(settings, text='모델 / API 키')
        tabs.add(help_tab, text='시작 안내')
        top = ttk.Frame(main)
        top.pack(fill='x')
        self.profile_select = ttk.Combobox(top, state='readonly', width=30)
        self.profile_select.pack(side='left')
        self.profile_select.bind('<<ComboboxSelected>>', self.select_profile)
        for label, method in [('새 프로필', self.new_profile), ('이름 변경', self.rename_profile),
                              ('삭제', self.delete_profile), ('최근 삭제 복원', self.restore_profile)]:
            ttk.Button(top, text=label, command=self.wrap(method)).pack(side='left', padx=3)
        form = ttk.Frame(main)
        form.pack(fill='x', pady=8)
        self.pvars = {k: tk.StringVar() for k in ['adapter', 'game_id', 'endpoint', 'bridge_token', 'goal']}
        fields = [('연결 종류', 'adapter'), ('게임 ID', 'game_id'), ('브리지 주소', 'endpoint'),
                  ('브리지 토큰 (API 키 아님)', 'bridge_token'), ('사용자 목표', 'goal')]
        for i, (label, key) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky='w')
            if key == 'adapter':
                widget = ttk.Combobox(form, textvariable=self.pvars[key], state='readonly',
                                      values=['unconnected', 'demo', 'bridge'])
            else:
                widget = ttk.Entry(form, textvariable=self.pvars[key], show='*' if key == 'bridge_token' else '')
            widget.grid(row=i, column=1, sticky='ew', pady=2)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text='기억 메모 / 규칙\n(직접 편집 가능)').grid(row=5, column=0, sticky='nw')
        self.notes = tk.Text(form, height=3, wrap='word', font=('Segoe UI', 10))
        self.notes.grid(row=5, column=1, sticky='ew')
        controls = ttk.Frame(main)
        controls.pack(fill='x', pady=5)
        for label, method in [('프로필 저장', self.save_profile), ('연결 / 데모 초기화', self.connect),
                              ('상태 읽기', lambda: self.launch(self.engine.observe)),
                              ('목표 안내 생성', self.goal), ('한 번 분석', self.analyze),
                              ('제안 1회 실행', self.execute), ('자동 시작', self.start_auto)]:
            ttk.Button(controls, text=label, command=self.wrap(method)).pack(side='left', padx=2)
        flags = ttk.Frame(main)
        flags.pack(fill='x')
        self.offline = tk.BooleanVar(value=True)
        self.deep = tk.BooleanVar(value=True)
        ttk.Checkbutton(flags, text='데모 규칙 엔진 (API 미사용 / 내장 데모 전용)', variable=self.offline).pack(side='left')
        ttk.Checkbutton(flags, text='필요할 때 심화 모델 사용', variable=self.deep).pack(side='left', padx=12)
        diagnostics = ttk.Frame(main)
        diagnostics.pack(fill='x', pady=3)
        ttk.Label(diagnostics, text='3번 중지 시험 (API·실제 게임 미사용):').pack(side='left')
        for label, method in [('시험 작업 시작', lambda: self.launch(self.engine.start_cancel_demo)),
                              ('시험 통신 끊기', self.engine.task_control.drop_demo_connection),
                              ('작업 결과 확인', lambda: self.launch(self.engine.reconcile_task))]:
            ttk.Button(diagnostics, text=label, command=self.wrap(method)).pack(side='left', padx=2)
        self.status = tk.StringVar(value='데모 프로필 → 연결 → 자동 시작으로 먼저 확인하세요.')
        ttk.Label(main, textvariable=self.status, foreground='#15616d').pack(fill='x', pady=5)
        panes = ttk.Panedwindow(main, orient='horizontal')
        panes.pack(fill='both', expand=True)
        self.state_text = self.text_pane(panes, '실제 관측 상태 (JSON)')
        self.advice_text = self.text_pane(panes, '조언 / 제안 행동 / 목표 안내')
        logs = ttk.LabelFrame(main, text='진행 로그', padding=4)
        logs.pack(fill='x', pady=7)
        self.log_text = tk.Text(logs, height=6, state='disabled', wrap='word')
        self.log_text.pack(side='left', fill='both', expand=True)
        ttk.Button(logs, text='진단 로그 저장', command=self.wrap(self.export_log)).pack(side='right')
        self.build_settings(settings)
        help_text = '''1.0.3 — 3번 작업 취소 / 연결 끊김 처리

“시험 작업 시작”은 API 없이 내부 가상 카운터를 실행합니다. 실제 게임이 아닙니다.
상단 “중지” → 작업 로그의 “취소 확인”으로 원격 중단을 확인하세요.
“시험 통신 끊기”는 이 내부 시험의 유지 신호와 상태 조회만 차단합니다.
본체에는 “결과 불명”이 남으며 모듈은 마지막 유지 신호로부터 3초 뒤 중단합니다.
4초 정도 기다려 “작업 결과 확인”을 누르면 실제 중단 결과와 카운터를 조회합니다.
결과 확인은 작업을 다시 시작하거나 유지 신호를 갱신하지 않습니다.
기존 v1 단발 행동에는 원격 취소 기능이 없으므로 이미 실행된 행동을 되돌리지 않습니다.
실시간 제어·상황 변화에 따른 AI 재호출·상용 게임 연결은 이번 단계에 추가하지 않았습니다.

처음 실행하기

1. “데모 기지”를 선택하고 “연결 / 데모 초기화”를 누릅니다.
2. “데모 규칙 엔진”을 켠 채 “자동 시작”을 누르면 API 없이 데모가 진행됩니다.
   식량과 연구 수치가 바뀌고 승리하면 멈춥니다. 이는 AI 성능 시험이 아닙니다.
3. 모델 / API 키에서 기존 키를 저장하고 빠른·심화 모델 연결을 각각 시험하세요.
4. 데모 규칙 엔진을 끄면 같은 데모를 실제 AI가 판단합니다. API 사용량이 발생합니다.

새 게임 프로필
이름 / 목표 / 메모를 게임별로 저장합니다. 프로필 생성만으로 게임에 연결되지는 않습니다.
unconnected = 미연결, demo = 내장 데모, bridge = 개발한 게임 모드/브리지와 연결.
실제 게임 모드는 이번 배포에 포함되어 있지 않습니다. ADAPTER_PROTOCOL.md를 참고하세요.
이 버전은 화면 인식·마우스·키보드 조작 방식이 아닌 구조화된 상태/행동 연결 기반입니다.

판단 구조와 중지
빠른 모델 → 필요하면 심화 모델 1회 → 현재 상태 재확인 → 행동 1개 → 결과 관측.
빠른 모델 기본 30초 / 심화 기본 60초입니다. 연속 호출 시 합산 시간은 늘어날 수 있습니다.
시간 초과/오류는 자동 재시도하지 않습니다. 신뢰도는 모델 추정치이지 정확도 보증이 아닙니다.
자동 실행은 낮은 위험 + 신뢰도 0.8 이상에만 허용됩니다. 고위험 행동은 직접 확인하세요.
중지는 후속 행동을 차단하고 지원 모듈에 작업 취소를 전달합니다. 이미 끝난 행동/API 비용은 되돌리지 못합니다.
Esc는 이 창에 포커스가 있을 때만 작동합니다. 전역 단축키는 제공하지 않습니다.

기억과 비용
메모와 최근 판단 기록을 다음 요청에 넣습니다. 모델 자체를 재학습하는 기능은 아닙니다.
목표 안내는 모델 사전지식 기반이며 인터넷 검색이 아닙니다. 답이 틀릴 수 있으니 확인하세요.
무료 모드는 NVIDIA의 무료 엔드포인트/계정 한도 내 이용이며 무제한 무료를 보장하지 않습니다.
유료 모드는 OpenAI API 별도 과금입니다. 무료 모드가 유료 API로 자동 전환하지 않습니다.
게임 상태·목표·메모·최근 판단은 선택한 API 제공자에 전송됩니다. 비밀을 넣지 마세요.
키는 Windows 사용자 계정에 묶어 암호화 저장합니다. 다른 PC에서는 다시 입력하세요.

프로필 삭제는 휴지통으로 이동합니다. “최근 삭제 복원”으로 되살릴 수 있습니다.
이전 0.x 프로그램 폴더는 수정하지 않습니다. 무료·유료 창은 동시에 실행하지 마세요.'''
        view = tk.Text(help_tab, wrap='word', font=('Segoe UI', 11))
        view.insert('1.0', help_text)
        view.configure(state='disabled')
        view.pack(fill='both', expand=True)
        self.refresh_profiles()
        root.after(100, self.poll)

    def wrap(self, fn):
        def run():
            try:
                fn()
            except Exception as exc:
                messagebox.showerror('확인 필요', str(exc), parent=self.root)
        return run

    def idle(self):
        if self.engine.busy or self.auto or self.closing or self.engine.task_control.cancel_busy.is_set():
            raise ValueError('먼저 중지하고 현재 작업이 끝난 뒤 변경하세요.')

    def text_pane(self, panes, label):
        frame = ttk.LabelFrame(panes, text=label, padding=6)
        text = tk.Text(frame, wrap='word', width=40, height=10, state='disabled')
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        panes.add(frame, weight=1)
        return text

    def replace_text(self, widget, value):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', value)
        widget.configure(state='disabled')

    def refresh_profiles(self, ident=None):
        self.ids = list(self.store.data['profiles'])
        self.profile_select['values'] = [self.store.data['profiles'][i]['name'] for i in self.ids]
        self.active = ident if ident in self.ids else (self.ids[0] if self.ids else None)
        self.engine.adapter = None
        self.engine.pending = None
        if self.active:
            self.profile_select.current(self.ids.index(self.active))
            self.load_profile()
        else:
            self.profile_select.set('프로필을 새로 만드세요')
        self.replace_text(self.state_text, '미연결 — 연결 버튼을 누르세요.')

    def load_profile(self):
        p = self.store.data['profiles'][self.active]
        for key, var in self.pvars.items():
            var.set(p[key])
        self.notes.delete('1.0', 'end')
        self.notes.insert('1.0', p['notes'])
        self.offline.set(p['adapter'] == 'demo')
        self.replace_text(self.advice_text, '메모와 최근 판단 기록은 이 프로필에 저장됩니다.')

    def select_profile(self, _):
        if self.engine.busy or self.auto:
            if self.active in self.ids:
                self.profile_select.current(self.ids.index(self.active))
            messagebox.showinfo('진행 중', '먼저 중지하세요.')
            return
        self.active = self.ids[self.profile_select.current()]
        self.engine.adapter = None
        self.engine.pending = None
        self.load_profile()
        self.replace_text(self.state_text, '미연결 — 연결 버튼을 누르세요.')

    def current(self):
        if not self.active:
            raise ValueError('프로필을 먼저 만드세요.')
        return self.store.data['profiles'][self.active]

    def save_profile(self):
        self.idle()
        p = self.current()
        changes = {k: v.get().strip() for k, v in self.pvars.items()}
        if any(p[k] != changes[k] for k in ('adapter', 'game_id', 'endpoint', 'bridge_token')):
            self.engine.adapter = None
        self.engine.pending = None
        p.update(changes)
        p['notes'] = self.notes.get('1.0', 'end').strip()[:12000]
        self.store.save()
        self.log('프로필 저장 완료')

    def new_profile(self):
        self.idle()
        name = simpledialog.askstring('새 프로필', '게임/프로필 이름:', parent=self.root)
        if name:
            self.refresh_profiles(self.store.create(name))

    def rename_profile(self):
        self.idle()
        p = self.current()
        name = simpledialog.askstring('이름 변경', '새 이름:', initialvalue=p['name'], parent=self.root)
        if name and name.strip():
            p['name'] = name.strip()[:100]
            self.store.save()
            self.refresh_profiles(p['id'])

    def delete_profile(self):
        self.idle()
        p = self.current()
        if messagebox.askyesno('삭제', p['name'] + ' 프로필을 휴지통으로 옮길까요? 복원할 수 있습니다.'):
            self.store.delete(self.active)
            self.refresh_profiles()

    def restore_profile(self):
        self.idle()
        self.refresh_profiles(self.store.restore())

    def launch(self, fn, *args):
        self.idle()
        self.engine.launch(fn, *args)

    def connect(self):
        self.save_profile()
        self.launch(self.engine.connect, self.current())

    def analyze(self):
        self.save_profile()
        offline = self.offline.get()
        self.launch(self.engine.analyze, self.current(), self.edition, self.store.settings(self.edition),
                    '' if offline else self.store.key(self.edition), offline, False, self.deep.get())

    def goal(self):
        self.save_profile()
        self.launch(self.engine.generate_goal, self.current(), self.edition,
                    self.store.settings(self.edition), self.store.key(self.edition))

    def execute(self):
        self.idle()
        pending = self.engine.pending
        if not pending:
            raise ValueError('먼저 분석해서 행동 제안을 받으세요.')
        if messagebox.askyesno('행동 확인', f"{pending['action']['label']}\n위험도: {pending['action']['risk']}\n이 행동을 한 번 실행할까요?"):
            self.launch(self.engine.execute, True)

    def start_auto(self):
        self.save_profile()
        if self.engine.adapter is None:
            raise ValueError('먼저 연결을 누르세요.')
        if not messagebox.askyesno('자동 실행 시작', '허용된 낮은 위험 행동을 반복 실행합니다.\nAPI 사용 시 요청 한도까지 요금/사용량이 발생할 수 있습니다.\n시작할까요?'):
            return
        self.auto = True
        self.auto_step()

    def auto_step(self):
        self.timer = None
        if not self.auto or self.closing:
            return
        try:
            offline = self.offline.get()
            self.engine.launch(self.engine.analyze, self.current(), self.edition,
                self.store.settings(self.edition), '' if offline else self.store.key(self.edition),
                offline, True, self.deep.get())
        except Exception as exc:
            self.auto = False
            self.log(str(exc))

    def stop(self):
        self.auto = False
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.engine.stop()

    def build_settings(self, frame):
        self.svars = {}
        settings = self.store.settings(self.edition)
        fields = [('빠른 모델 ID', 'fast'), ('심화 모델 ID', 'deep'),
                  ('빠른 모델 대기시간 (5~180초)', 'fast_timeout'), ('심화 모델 대기시간 (5~180초)', 'deep_timeout'),
                  ('자동 실행 간격 (2~120초)', 'interval'), ('이번 실행 API 요청 한도 (1~1000)', 'call_limit')]
        for i, (label, key) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky='w', pady=4)
            self.svars[key] = tk.StringVar(value=str(settings[key]))
            ttk.Entry(frame, textvariable=self.svars[key], width=55).grid(row=i, column=1, sticky='ew')
        self.key_var = tk.StringVar()
        ttk.Label(frame, text='기존 API 키 (저장된 키는 표시하지 않음)').grid(row=6, column=0, sticky='w')
        ttk.Entry(frame, textvariable=self.key_var, show='*', width=55).grid(row=6, column=1, sticky='ew')
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky='w', pady=12)
        for label, method in [('설정 / 입력한 키 저장', self.save_settings), ('이전 폴더에서 키 가져오기', self.import_key),
                              ('빠른 모델 시험', lambda: self.test_api('fast')), ('심화 모델 시험', lambda: self.test_api('deep'))]:
            ttk.Button(buttons, text=label, command=self.wrap(method)).pack(side='left', padx=3)
        self.key_status = tk.StringVar()
        ttk.Label(frame, textvariable=self.key_status).grid(row=8, column=0, columnspan=2, sticky='w')
        self.update_key_status()
        text = ('무료 NVIDIA 엔드포인트 전용. 계정 한도/모델 접근 여부에 따라 실패할 수 있습니다.' if self.edition == 'free'
                else '유료 OpenAI API 전용. ChatGPT 구독과 별도 과금되며 계정별 모델 권한을 확인하세요.')
        ttk.Label(frame, text=text).grid(row=9, column=0, columnspan=2, sticky='w', pady=8)
        ttk.Label(frame, text='키 발급: build.nvidia.com / platform.openai.com/api-keys\n'
                  '같은 제공자의 기존 키를 사용합니다. 모델 변경만으로 새 키가 필요한 것은 아닙니다.\n'
                  '대기시간을 늘려도 성공을 보장하지 않습니다. 최대 90초 지난 관측의 행동은 거절합니다.\n'
                  '설정은 저장 후 적용됩니다. 키 입력란을 비워 두면 저장된 키는 유지됩니다.\n'
                  '진단 로그는 키와 전체 게임 상태를 제외하지만 게임 내 행동 이름이 포함될 수 있습니다.\n\n'
                  '데이터 위치: ' + str(self.store.root)).grid(row=10, column=0, columnspan=2, sticky='w')
        frame.columnconfigure(1, weight=1)

    def update_key_status(self):
        try:
            available = bool(self.store.key(self.edition))
            self.key_status.set('키 있음 (인증 여부는 모델 시험으로 확인)' if available else '키 없음 — 기존 키를 붙여넣고 저장하세요.')
        except Exception:
            self.key_status.set('저장된 키를 읽지 못했습니다. 이 PC에서 다시 저장하세요.')

    def save_settings(self):
        self.idle()
        settings = {k: v.get().strip() for k, v in self.svars.items()}
        for key, low, high in [('fast_timeout', 5, 180), ('deep_timeout', 5, 180), ('interval', 2, 120), ('call_limit', 1, 1000)]:
            try:
                settings[key] = int(settings[key])
            except ValueError:
                raise ValueError(key + ': 정수를 입력하세요.') from None
            if not low <= settings[key] <= high:
                raise ValueError(f'{key}: {low}~{high} 범위로 입력하세요.')
        if not settings['fast'] or not settings['deep']:
            raise ValueError('모델 ID가 필요합니다.')
        if self.key_var.get().strip():
            self.store.save_key(self.edition, self.key_var.get())
            self.key_var.set('')
        self.store.data['settings'][self.edition] = settings
        self.store.save()
        self.update_key_status()
        self.log('모델 설정 저장 완료')

    def import_key(self):
        self.idle()
        folder = filedialog.askdirectory(title='기존 .env 또는 .env.nvidia가 있는 폴더 선택',
            initialdir=str(self.store.root.parent / 'GameCopilot'))
        if folder:
            self.store.import_key(self.edition, folder)
            self.update_key_status()
            self.log('기존 키 가져오기 완료 (원본 파일은 변경하지 않음)')

    def test_api(self, role):
        self.save_settings()
        self.launch(self.engine.test_api, self.edition, self.store.settings(self.edition), self.store.key(self.edition), role)

    def log(self, text):
        line = time.strftime('%H:%M:%S') + '  ' + text
        self.logs.append(line)
        self.logs = self.logs[-500:]
        self.replace_text(self.log_text, '\n'.join(self.logs[-100:]))
        self.log_text.see('end')

    def export_log(self):
        path = filedialog.asksaveasfilename(defaultextension='.txt', initialfile='game-copilot-' + __version__ + '-log.txt')
        if path:
            from pathlib import Path
            Path(path).write_text('Game Copilot ' + __version__ + ' / ' + self.edition + '\n' + '\n'.join(self.logs), encoding='utf-8')

    def poll(self):
        try:
            while True:
                kind, value = self.engine.events.get_nowait()
                if kind in ('log', 'error', 'halt', 'terminal'):
                    self.log(value)
                    if kind != 'log':
                        self.auto = False
                elif kind == 'task':
                    progress = '' if value['progress'] is None else f" / {value['progress']:.0%}"
                    self.log(f"작업 {value['task_id'][:8]} / {LABELS[value['status']]}{progress} / {value['message']}")
                    if value['status'] in ('unknown', 'failed', 'cancelled'):
                        self.auto = False
                elif kind == 'state':
                    self.replace_text(self.state_text, json.dumps(value, ensure_ascii=False, indent=2))
                elif kind == 'usage':
                    self.log('제공자 사용량: ' + json.dumps(value, ensure_ascii=False)[:500])
                elif kind == 'plan':
                    p = value['plan']
                    self.replace_text(self.advice_text, p['advice'] + '\n\n제안: ' + str(p['action_id']) +
                        f"\n모델 추정 신뢰도: {p['confidence']:.2f}\n\n관측 메모(검증 필요): " + p['memory'])
                    profile = self.store.data['profiles'].get(value['profile_id'])
                    if profile is not None:
                        profile['history'] = (profile.get('history', []) + [{
                            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                            'model_note_unverified': p['memory'], 'advice': p['advice'],
                            'proposed_action_not_receipt': p['action_id']}])[-30:]
                        self.store.save()
                elif kind == 'goal':
                    self.replace_text(self.advice_text, '기본 목표 안내 (모델 사전지식 / 검색 아님)\n\n' + value['text'] +
                        '\n\n확인한 뒤 원하는 내용을 사용자 목표에 입력하고 저장하세요.')
                elif kind == 'done' and self.auto and value == self.engine.job_id and not self.engine.busy:
                    if self.timer:
                        self.root.after_cancel(self.timer)
                    self.timer = self.root.after(self.store.settings(self.edition)['interval'] * 1000, self.auto_step)
        except queue.Empty:
            pass
        except Exception as exc:
            self.auto = False
            self.log('결과 저장/표시 실패: ' + type(exc).__name__)
        elapsed = int(time.monotonic() - self.engine.started) if self.engine.started else 0
        self.status.set(f"{'자동 진행' if self.auto else '수동'} · {self.engine.phase} · {elapsed}초 · API 요청 {self.engine.calls}회")
        self.root.after(100, self.poll)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.stop()
        self.close_deadline = time.monotonic() + 3
        self._finish_close()

    def _finish_close(self):
        if (self.engine.busy or self.engine.task_control.cancel_busy.is_set()) and time.monotonic() < self.close_deadline:
            self.root.after(100, self._finish_close)
            return
        if self.engine.cancel_demo:
            self.engine.cancel_demo.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--edition', choices=['free', 'paid'], default='free')
    args = parser.parse_args()
    store = Store()
    # OS file lock is released on process exit, including crashes. Avoid shared profile writes.
    lock_file = (store.root / 'running.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.write(b'0')
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('Game Copilot 1.0 is already running. Close the other window first.')
        return
    root = tk.Tk()
    App(root, args.edition, store)
    root.mainloop()
    lock_file.close()


if __name__ == '__main__':
    main()
