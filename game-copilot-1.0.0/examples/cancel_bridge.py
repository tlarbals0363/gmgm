"""Step 3 reference task server. Independent counter watchdog; no real game."""
import argparse
import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from copilot.task_demo import CancellableTaskDemo


def make_server(port=8767, demo=None):
    demo = demo or CancellableTaskDemo()
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def authorized(self):
            return not self.headers.get('Origin') and hmac.compare_digest(
                self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode())

        def reply(self, code, data):
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self.authorized():
                self.reply(401, {'error': 'unauthorized'})
            elif self.path == '/v1/state':
                self.reply(200, demo.observe())
            else:
                self.reply(404, {'error': 'not found'})

        def do_POST(self):
            if not self.authorized():
                self.reply(401, {'error': 'unauthorized'})
                return
            operation = self.path.removeprefix('/v1/tasks/')
            if self.path != '/v1/tasks/' + operation or operation not in ('start', 'status', 'heartbeat', 'cancel'):
                self.reply(404, {'error': 'not found'})
                return
            try:
                self.connection.settimeout(2)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('body size')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or body.get('schema_version') != 1 or body.get('game_id') != demo.game_id:
                    raise ValueError('game/version mismatch')
                if operation == 'start':
                    result = demo.start_task({'task_id': body['task_id'], 'game_id': body['game_id'],
                        'action_id': body['action_id'], 'revision': body['expected_revision']},
                        body['controller_id'], expires_at=float(body['expires_at']), max_seconds=body['max_seconds'])
                else:
                    function = {'status': demo.task_status, 'heartbeat': demo.heartbeat, 'cancel': demo.cancel_task}[operation]
                    result = function(body['task_id'], body['controller_id'])
                self.reply(200, result)
            except (TypeError, KeyError, ValueError):
                self.reply(409, {'error': 'invalid/conflicting/stale task'})
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server, demo, token


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8767)
    args = parser.parse_args()
    server, demo, token = make_server(args.port)
    print('Step 3 COUNTER DEMO ONLY - no real game, no API calls.', flush=True)
    print(f'Endpoint: http://127.0.0.1:{server.server_port}\nGame ID: {demo.game_id}\nBridge token: {token}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        demo.close()
        server.server_close()


if __name__ == '__main__':
    main()
