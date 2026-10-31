#!/usr/bin/env python3
"""Valid HTTP/1.1 fragmentation, chunking, pipelining, and idle connection checks."""
import base64
import hashlib
import hmac
import json
import os
import socket
import time


def signed_token():
    def b64(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
    payload = {'sub': '1', 'username': 'golden_ember_1', 'exp': int(time.time()) + 3600}
    message = b64({'alg': 'HS256', 'typ': 'JWT'}) + b'.' + b64(payload)
    signature = hmac.new(os.environ.get('JWT_SECRET', 'million-users-challenge').encode(), message, hashlib.sha256).digest()
    return (message + b'.' + base64.urlsafe_b64encode(signature).rstrip(b'=')).decode()


class Connection:
    def __init__(self, port):
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=15)
        self.buffer = b''

    def close(self):
        self.socket.close()

    def receive(self):
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
                width = widths[position % len(widths)]
                self.socket.sendall(data[position:position + width])
                position += width
                time.sleep(.001)
        else:
            self.socket.sendall(data)


def get(path, close=False):
    return (f'GET {path} HTTP/1.1\r\nHost: localhost\r\n'
            f'Connection: {"close" if close else "keep-alive"}\r\n\r\n').encode()


def main():
    token = signed_token()
    idle_connections = []
    try:
        for port in (3000, 3001):
            connection = Connection(port)
            try:
                connection.send(get('/health'), fragmented=True)
                status, value = connection.response()
                assert status == 200 and value['status'] == 'ok' and value['db'] == 'ok'
                # All requests are coalesced into one write; responses must remain ordered.
                connection.send(get('/posts/1') + get('/health') + get('/feed'))
                assert connection.response()[1]['post']['id'] == 1
                assert connection.response()[1]['status'] == 'ok'
                assert len(connection.response()[1]['posts']) == 20
                body_text = 'fragmented HTTP UTF-8: café 😀 newline\nquote" slash\\'
                body = json.dumps({'body': body_text}, ensure_ascii=False).encode()
                common = (f'POST /posts HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n'
                          'Content-Type: application/json\r\n').encode()
                connection.send(common + f'Content-Length: {len(body)}\r\n\r\n'.encode() + body, fragmented=True)
                status, value = connection.response()
                assert status == 201 and value['post']['body'] == body_text
                connection.send(get('/posts/' + str(value['post']['id'])))
                assert connection.response()[1]['post']['body'] == body_text
                chunks = b''.join(f'{len(body[i:i+7]):x}\r\n'.encode() + body[i:i+7] + b'\r\n' for i in range(0, len(body), 7))
                # Follow the zero chunk immediately with a pipelined GET. The
                # parser must consume exactly one request, leaving the next intact.
                connection.send(common + b'Transfer-Encoding: chunked\r\n\r\n' + chunks + b'0\r\n\r\n' + get('/health'), fragmented=True)
                status, value = connection.response()
                assert status == 201 and value['post']['body'] == body_text
                assert connection.response()[1]['status'] == 'ok'
                connection.send(get('/health', close=True))
                assert connection.response()[0] == 200
                assert connection.socket.recv(1) == b'', 'Connection: close was not honored'
                print(f'PASS {port}: fragmented headers/body/UTF-8, pipelined requests, chunked POST, response framing, Connection: close', flush=True)
            finally:
                connection.close()
            half_closed = Connection(port)
            try:
                half_closed.send(get('/posts/1') + get('/health') + get('/feed'))
                half_closed.socket.shutdown(socket.SHUT_WR)
                assert half_closed.response()[1]['post']['id'] == 1
                assert half_closed.response()[1]['status'] == 'ok'
                assert len(half_closed.response()[1]['posts']) == 20
                assert half_closed.socket.recv(1) == b'', 'Half-closed request stream did not finish after its queued responses'
                print(f'PASS {port}: all three pipelined responses survive client write-half-close', flush=True)
            finally:
                half_closed.close()
            idle = Connection(port)
            idle.send(get('/health'))
            assert idle.response()[0] == 200
            idle_connections.append(idle)
        print('Checking the same two sockets after 76 seconds idle...', flush=True)
        time.sleep(76)
        for connection in idle_connections:
            connection.send(get('/health'))
            assert connection.response()[0] == 200
        print('PASS both original sockets remained usable after 76 seconds idle', flush=True)
    finally:
        for connection in idle_connections:
            connection.close()


if __name__ == '__main__':
    main()
