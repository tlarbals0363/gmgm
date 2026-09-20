"""Versioned storage, atomic writes and Windows user-bound API key encryption."""
import base64
import ctypes
import json
import os
import re
import uuid
from pathlib import Path


def data_root():
    return Path(os.environ.get('APPDATA', str(Path.home() / '.config'))) / 'GameCopilot1'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as f:
            os.chmod(temporary, 0o600)
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def crypt(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('키 영구 저장은 Windows에서만 지원합니다. 다른 OS에서는 환경변수를 사용하세요.')
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    api = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    api.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    api.restype = ctypes.c_int
    if not api(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('Windows 키 암호화/복호화에 실패했습니다.')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(target.data)


DEFAULTS = {
    'free': {'fast': 'z-ai/glm-5.3-flash', 'deep': 'moonshotai/kimi-k3'},
    'paid': {'fast': 'gpt-5.6-luna', 'deep': 'gpt-6-astra'},
}


class Store:
    def __init__(self, root=None):
        self.root = Path(root or data_root())
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'workspace.json'
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if self.data.get('version') != 1:
                raise ValueError('지원하지 않는 데이터 버전입니다. 원본 파일은 변경하지 않았습니다.')
        else:
            self.data = {'version': 1, 'profiles': {}, 'trash': {}, 'settings': {}}
            self.create('데모 기지', 'demo')

    def save(self):
        atomic_json(self.path, self.data)

    def create(self, name, adapter='unconnected'):
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError('프로필 이름은 1~100자로 입력하세요.')
        ident = uuid.uuid4().hex
        self.data['profiles'][ident] = {
            'id': ident, 'name': name, 'adapter': adapter,
            'game_id': 'demo-colony' if adapter == 'demo' else '',
            'endpoint': 'http://127.0.0.1:8766', 'bridge_token': '',
            'goal': '연구 점수 10 달성' if adapter == 'demo' else '',
            'notes': '', 'history': []}
        self.save()
        return ident

    def delete(self, ident):
        self.data['trash'][ident] = self.data['profiles'].pop(ident)
        self.save()

    def restore(self):
        if not self.data['trash']:
            raise ValueError('복원할 프로필이 없습니다.')
        ident = next(reversed(self.data['trash']))
        self.data['profiles'][ident] = self.data['trash'].pop(ident)
        self.save()
        return ident

    def settings(self, edition):
        return {**DEFAULTS[edition], 'fast_timeout': 30, 'deep_timeout': 60,
                'interval': 5, 'call_limit': 40, **self.data['settings'].get(edition, {})}

    def key(self, edition):
        path = self.root / (edition + '.key')
        if path.exists():
            return crypt(base64.b64decode(path.read_bytes()), True).decode()
        return os.environ.get('NVIDIA_API_KEY' if edition == 'free' else 'OPENAI_API_KEY', '').strip()

    def save_key(self, edition, key):
        key = key.strip()
        if key.lower().startswith('bearer '):
            key = key[7:].strip()
        if not key or any(c.isspace() for c in key) or not key.isascii():
            raise ValueError('공백 없이 API 키만 붙여넣어 주세요. Bearer는 자동 제거됩니다.')
        encrypted = base64.b64encode(crypt(key.encode()))
        path = self.root / (edition + '.key')
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(encrypted)
        os.replace(temporary, path)

    def import_key(self, edition, folder):
        """Only read the selected folder's known .env filename; never execute it."""
        var = 'NVIDIA_API_KEY' if edition == 'free' else 'OPENAI_API_KEY'
        names = ['.env.nvidia', '.env'] if edition == 'free' else ['.env']
        for name in names:
            path = Path(folder) / name
            if not path.is_file():
                continue
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                found = re.match(r'^\s*(?:export\s+)?' + var + r'\s*=\s*(.*?)\s*$', line)
                if found:
                    self.save_key(edition, found[1].strip('"\''))
                    return
        raise ValueError('선택한 폴더의 .env 파일에서 해당 API 키를 찾지 못했습니다.')
