"""Reference localhost bridge. NOT an adapter for an installed commercial game."""
import argparse
import hmac
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot.adapters import DemoAdapter


def make_server(port=8766, token=None):
    token = token or secrets.token_urlsafe(32)
    demo = DemoAdapter()
    action_lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def authorized(self):
            return not self.headers.get('Origin') and hmac.compare_digest(
                self.headers.get('Authorization', ''), 'Bearer ' + token)

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
            if self.path != '/v1/action':
                self.reply(404, {'error': 'not found'})
                return
            try:
                self.connection.settimeout(5)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('body size')
                body = json.loads(self.rfile.read(length))
                if body.get('schema_version') != 1 or body.get('game_id') != 'demo-colony':
                    raise ValueError('game/version mismatch')
                if not isinstance(body.get('request_id'), str) or not 1 <= len(body['request_id']) <= 128:
                    raise ValueError('request ID required')
                with action_lock:
                    if body['request_id'] in demo.receipts:
                        receipt = demo.receipts[body['request_id']]
                    else:
                        if not time.time() < float(body['expires_at']) <= time.time() + 10:
                            raise ValueError('expired request')
                        receipt = demo.act(body['action_id'], body['expected_revision'], body['request_id'])
                self.reply(200, receipt)
            except (ValueError, KeyError, TypeError):
                self.reply(409, {'accepted': False, 'error': 'invalid or stale action'})
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server, token


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    server, token = make_server(args.port)
    print('Reference demo bridge only. Leave this terminal open.')
    print(f'Endpoint: http://127.0.0.1:{server.server_port}\nGame ID: demo-colony\nBridge token: {token}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
