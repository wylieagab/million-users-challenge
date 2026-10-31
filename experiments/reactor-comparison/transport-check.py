#!/usr/bin/env python3
"""Valid HTTP/1.1 fragmentation, chunking, pipelining, and idle connection checks."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import threading
import time
import traceback


def signed_token():
    def b64(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
    payload = {'sub': '1', 'username': 'golden_ember_1', 'exp': int(time.time()) + 3600}
    message = b64({'alg': 'HS256', 'typ': 'JWT'}) + b'.' + b64(payload)
    signature = hmac.new(os.environ.get('JWT_SECRET', 'million-users-challenge').encode(), message, hashlib.sha256).digest()
    return (message + b'.' + base64.urlsafe_b64encode(signature).rstrip(b'=')).decode()


class Connection:
    def __init__(self, port):
        self.deadline = time.monotonic() + 15
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=15)
        self.buffer = b''

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.socket.close()

    def check_deadline(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Transport case exceeded its 15-second I/O deadline')
        self.socket.settimeout(remaining)

    def receive(self):
        self.check_deadline()
        data = self.socket.recv(65536)
        if not data:
            raise AssertionError('Unexpected EOF while reading a complete response')
        self.buffer += data

    def line(self):
        while b'\r\n' not in self.buffer:
            self.receive()
        result, self.buffer = self.buffer.split(b'\r\n', 1)
        return result

    def take(self, count):
        while len(self.buffer) < count:
            self.receive()
        result, self.buffer = self.buffer[:count], self.buffer[count:]
        return result

    def response(self):
        status = int(self.line().split()[1])
        headers = {}
        while line := self.line():
            key, value = line.split(b':', 1)
            headers[key.lower()] = value.strip()
        if headers.get(b'transfer-encoding', b'').lower() == b'chunked':
            body = b''
            while size := int(self.line().split(b';', 1)[0], 16):
                body += self.take(size)
                assert self.take(2) == b'\r\n'
            while self.line():
                pass
        else:
            assert b'content-length' in headers, 'Persistent response requires unambiguous framing'
            body = self.take(int(headers[b'content-length']))
        assert headers.get(b'content-type', b'').startswith(b'application/json')
        return status, json.loads(body)

    def send(self, data, fragmented=False):
        if fragmented:
            # Split headers, CRLF boundaries, chunk syntax, and multi-byte UTF-8.
            position = 0
            widths = [1, 2, 7, 3, 19, 1, 5]
            while position < len(data):
                self.check_deadline()
                width = widths[position % len(widths)]
                self.socket.sendall(data[position:position + width])
                position += width
                time.sleep(.001)
        else:
            self.check_deadline()
            self.socket.sendall(data)

    def expect_eof(self):
        assert not self.buffer, 'Unexpected bytes remain after the final response'
        self.check_deadline()
        assert self.socket.recv(1) == b'', 'Expected EOF after the final response'


def get(path, close=False):
    return (f'GET {path} HTTP/1.1\r\nHost: localhost\r\n'
            f'Connection: {"close" if close else "keep-alive"}\r\n\r\n').encode()


def expect_health(connection):
    status, value = connection.response()
    assert status == 200 and value['status'] == 'ok' and value['db'] == 'ok'


def expect_pipeline(connection):
    status, value = connection.response()
    assert status == 200 and value['post']['id'] == 1
    expect_health(connection)
    status, value = connection.response()
    assert status == 200 and len(value['posts']) == 20


def post_payload(token):
    text = 'fragmented HTTP UTF-8: café 😀 newline\nquote" slash\\'
    body = json.dumps({'body': text}, ensure_ascii=False).encode()
    headers = (f'POST /posts HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n'
               'Content-Type: application/json\r\n').encode()
    return headers, body, text


def fragmented_headers(port):
    with Connection(port) as connection:
        connection.send(get('/health'), fragmented=True)
        expect_health(connection)


def pipelined_gets(port):
    with Connection(port) as connection:
        connection.send(get('/posts/1') + get('/health') + get('/feed'))
        expect_pipeline(connection)


def fragmented_post(port, *, token):
    headers, body, text = post_payload(token)
    with Connection(port) as connection:
        connection.send(headers + f'Content-Length: {len(body)}\r\n\r\n'.encode() + body, fragmented=True)
        status, value = connection.response()
        assert status == 201 and value['post']['body'] == text
        connection.send(get('/posts/' + str(value['post']['id'])))
        status, value = connection.response()
        assert status == 200 and value['post']['body'] == text


def chunked_post_and_pipeline(port, *, token):
    headers, body, text = post_payload(token)
    chunks = b''.join(f'{len(body[i:i+7]):x}\r\n'.encode() + body[i:i+7] + b'\r\n' for i in range(0, len(body), 7))
    with Connection(port) as connection:
        connection.send(headers + b'Transfer-Encoding: chunked\r\n\r\n' + chunks + b'0\r\n\r\n' + get('/health'), fragmented=True)
        status, value = connection.response()
        assert status == 201 and value['post']['body'] == text
        expect_health(connection)


def connection_close(port):
    with Connection(port) as connection:
        connection.send(get('/health', close=True))
        expect_health(connection)
        connection.expect_eof()


def write_half_close(port):
    with Connection(port) as connection:
        connection.send(get('/posts/1') + get('/health') + get('/feed'))
        connection.socket.shutdown(socket.SHUT_WR)
        expect_pipeline(connection)
        connection.expect_eof()


def idle_keepalive(port):
    with Connection(port) as connection:
        connection.send(get('/health'))
        expect_health(connection)
        time.sleep(76)
        connection.deadline = time.monotonic() + 15
        connection.send(get('/health'))
        expect_health(connection)


class Suite:
    def __init__(self, output=None):
        self.output = Path(output) if output else None
        self.lock = threading.Lock()
        self.result = {'schema_version': 1, 'suite': 'http11-transport', 'complete': False, 'cases': []}
        self.save()

    def save(self):
        cases = self.result['cases']
        self.result['summary'] = {'passed': sum(c['status'] == 'passed' for c in cases),
                                  'failed': sum(c['status'] == 'failed' for c in cases), 'total': len(cases)}
        if self.output:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.output.with_suffix(self.output.suffix + '.tmp')
            temporary.write_text(json.dumps(self.result, indent=2) + '\n', encoding='utf-8')
            temporary.replace(self.output)

    def run(self, name, port, operation):
        started = time.monotonic()
        case = {'name': name, 'port': port, 'status': 'passed'}
        try:
            operation(port)
        except Exception as error:
            case.update(status='failed', error_type=type(error).__name__, error=str(error),
                        traceback=traceback.format_exc())
        case['elapsed_s'] = time.monotonic() - started
        with self.lock:
            self.result['cases'].append(case)
            self.save()
            print('TRANSPORT_CASE ' + json.dumps(case, separators=(',', ':')), flush=True)
        return case

    def finish(self):
        self.result['complete'] = True
        self.save()
        print('TRANSPORT_SUMMARY ' + json.dumps(self.result['summary']), flush=True)
        return 1 if self.result['summary']['failed'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Per-case JSON artifact, updated after every completed case')
    args = parser.parse_args()
    token = signed_token()
    suite = Suite(args.output)
    cases = [('fragmented_headers', fragmented_headers),
             ('pipelined_gets', pipelined_gets),
             ('fragmented_utf8_post_and_readback', partial(fragmented_post, token=token)),
             ('chunked_post_and_pipelined_get', partial(chunked_post_and_pipeline, token=token)),
             ('connection_close', connection_close),
             ('write_half_close_drains_three_responses', write_half_close)]
    for port in (3000, 3001):
        for name, operation in cases:
            suite.run(name, port, operation)
    # Separate sockets and results; wait only once for the two idle checks.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(suite.run, 'idle_keepalive_76_seconds', port, idle_keepalive)
                   for port in (3000, 3001)]
        for future in futures:
            future.result()
    return suite.finish()


if __name__ == '__main__':
    raise SystemExit(main())
