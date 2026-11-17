PROBE3
#!/usr/bin/env python3
"""最小 WebSocket 客户端（RFC 6455），仅用 Python 标准库。

存在的理由：atvloadly 的 Apple ID 登录只提供 WebSocket 通道（/ws/login），
没有对应的 REST 接口。要实现"登录账号"能力就必须有 WebSocket 客户端，
而引入第三方库会破坏这个技能"零依赖、随处可跑"的可移植性。

支持范围：文本帧、二进制帧、分片重组、ping/pong、close。
不支持：TLS 的 wss://（本技能的 atvloadly 场景均为局域网 http://）、
       扩展（permessage-deflate）、子协议协商。
"""

import base64
import hashlib
import os
import socket
import struct


class WebSocketError(Exception):
    pass


class WebSocket:
    """阻塞式 WebSocket 客户端，支持上下文管理器。"""

    def __init__(self, url, timeout=30):
        if not url.startswith("ws://"):
            raise WebSocketError(f"仅支持 ws://（当前仅需局域网 http 服务），收到: {url}")
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.host = host
        self.port = int(port) if port else 80
        self.path = "/" + path if path else "/"
        self.timeout = timeout
        self.sock = None
        self._buffer = b""

    # -- 握手 --

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))

        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手期间连接被关闭")
            header += chunk
            if len(header) > 65536:
                raise WebSocketError("握手响应过大")
        header_text = header.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        if "101" not in header_text.split("\r\n")[0]:
            raise WebSocketError(f"握手失败: {header_text.splitlines()[0]}")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode("ascii")
        if expected.lower() not in header_text.lower():
            raise WebSocketError("Sec-WebSocket-Accept 校验失败")
        # 握手响应之后可能已带部分帧数据
        self._buffer += header.split(b"\r\n\r\n", 1)[1]
        return self

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()
        return False

    # -- 帧读写 --

    def _recv_exact(self, n):
        while len(self._buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("连接已关闭")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def send(self, text, opcode=0x1):
        """发送一帧（客户端必须用掩码）。"""
        payload = text.encode("utf-8") if isinstance(text, str) else text
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        mask_bit = 0x80
        if length < 126:
            header.append(mask_bit | length)
        elif length < (1 << 16):
            header.append(mask_bit | 126)
            header += struct.pack(">H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self):
        """接收一条完整消息（自动处理分片与 ping/pong）。"""
        chunks = []
        message_opcode = None
        while True:
            b1, b2 = self._recv_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F

            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]

            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # close
                raise WebSocketError("对端关闭了连接")
            if opcode == 0x9:  # ping
                self.send(payload, opcode=0xA)
                continue
            if opcode == 0xA:  # pong
                continue

            if opcode in (0x1, 0x2):
                message_opcode = opcode
                chunks = [payload]
            elif opcode == 0x0:  # continuation
                chunks.append(payload)

            if fin:
                data = b"".join(chunks)
                if message_opcode == 0x2:
                    return data
                return data.decode("utf-8", errors="replace")

    def recv_until(self, markers, timeout=None):
        """持续接收直到某一行命中 markers 之一，返回 (命中标记, 全部输出)。

        markers 为字符串列表；返回的命中项取第一个包含该标记的标记名。
        """
        old_timeout = self.sock.gettimeout()
        if timeout:
            self.sock.settimeout(timeout)
        lines = []
        try:
            while True:
                text = self.recv()
                for raw_line in text.splitlines():
                    if not raw_line.strip():
                        continue
                    lines.append(raw_line)
                    for marker in markers:
                        if marker.lower() in raw_line.lower():
                            return marker, "\n".join(lines)
        finally:
            if old_timeout:
                self.sock.settimeout(old_timeout)

    def close(self):
        try:
            if self.sock:
                try:
                    self.send(b"", opcode=0x8)
                except OSError:
                    pass
                self.sock.close()
        finally:
            self.sock = None


def send_json(ws, msg_type, data):
    """按 atvloadly 的消息格式发送：{"t": type, "d": data}。"""
    import json as _json

    ws.send(_json.dumps({"t": msg_type, "d": data}))
