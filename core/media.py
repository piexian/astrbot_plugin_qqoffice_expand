"""媒体归一与安全防护。

to_uploadable(): URL / base64 / 本地路径 / bytes 归一为可上传对象
assert_public_url(): 下载远程 URL 前的 SSRF 防护（内网段 + DNS 逐 IP 校验）
strip_amr_header(): .amr 实为带 #!AMR 头的 SILK，发前剥头
image_size(): 只读 64KB 头解析 PNG/JPEG/GIF/WebP 尺寸，默认 512x512
"""

from __future__ import annotations

import base64
import binascii
import re as _re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .errors import QQOfficeAPIError

__all__ = ["Uploadable", "to_uploadable", "assert_public_url", "strip_amr_header", "image_size", "download_url"]

DEFAULT_IMAGE_SIZE = (512, 512)
_IMAGE_HEADER_LIMIT = 64 * 1024  # 只读头即够解析尺寸
_BASE64_RE = _re.compile(r"^[A-Za-z0-9+/\-_]+={0,2}$")

@dataclass
class Uploadable:
    """归一后的上传对象。kind: url / base64 / bytes / path。"""

    kind: str
    source: str | bytes = ""       # url 字符串 / base64 字符串 / 原始 bytes / 路径
    filename: str = "file"
    mime: str = ""
    size: int | None = None        # 已知字节数（url 场景可为 None）
    content_hash: str | None = None
    _bytes: bytes | None = field(default=None, repr=False)

    @property
    def is_url(self) -> bool:
        return self.kind == "url"

    def loaded_bytes(self) -> bytes | None:
        return self._bytes

    async def load_bytes(self) -> bytes:
        """按需取得原始字节：base64/bytes 直接返回；path 读文件；url 走 SSRF 校验下载。"""
        if self._bytes is not None:
            return self._bytes
        if self.kind == "bytes":
            raise QQOfficeAPIError(-1, "内部错误：bytes 来源缺少数据")
        if self.kind == "base64":
            data = base64.b64decode(self.source, validate=False)
        elif self.kind == "path":
            p = Path(str(self.source)).expanduser()
            if not p.is_file():
                raise QQOfficeAPIError(-1, f"本地文件不存在: {p}")
            data = p.read_bytes()
        elif self.kind == "url":
            data = await download_url(str(self.source))
        else:
            raise QQOfficeAPIError(-1, f"未知上传来源类型: {self.kind}")
        self._bytes = data
        self.size = len(data)
        self.content_hash = hashlib_sha256(data)
        return data

def hashlib_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()

def to_uploadable(source, *, filename: str | None = None, mime: str = "") -> Uploadable:
    """把多种来源归一为 Uploadable。

    接受：http(s) URL 字符串、base64 字符串或 data:URI、本地路径、bytes、
    上述任一组成的 list（取第一个，方便直接传 AstrBot 组件列表）。
    """
    if isinstance(source, (list, tuple)):
        for item in source:
            try:
                return to_uploadable(item, filename=filename, mime=mime)
            except QQOfficeAPIError:
                continue
        raise QQOfficeAPIError(-1, "未找到可用的媒体来源")

    if isinstance(source, Uploadable):
        return source

    if isinstance(source, (bytes, bytearray)):
        return Uploadable("bytes", bytes(source), filename or "file.bin", mime, len(source))

    if not isinstance(source, str) or not source.strip():
        raise QQOfficeAPIError(-1, f"无法识别的媒体来源: {type(source).__name__}")

    source = source.strip()

    if source.startswith(("http://", "https://")):
        assert_public_url(source)
        name = filename or Path(urlparse(source).path).name or "file"
        return Uploadable("url", source, name, mime)

    if source.startswith("data:"):
        # data:[<mime>];base64,<payload>
        header, _, payload = source.partition(",")
        meta = header[5:].split(";")[0]
        return Uploadable("base64", payload, filename or "file.bin", mime or meta)

    if _looks_like_base64(source):
        return Uploadable("base64", source, filename or "file.bin", mime)

    p = Path(source).expanduser()
    if p.is_file():
        size = p.stat().st_size
        return Uploadable("path", str(p), filename or p.name, mime, size)

    raise QQOfficeAPIError(-1, f"媒体来源既不是可访问 URL/base64，也不是存在的本地文件: {source[:80]}")

def _looks_like_base64(s: str) -> bool:
    """启发式：<64 字符的串按本地路径处理（与文件名无法区分）。"""
    if len(s) < 64 or len(s) % 4 != 0 or not _BASE64_RE.match(s):
        return False
    try:
        base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True

def _is_reserved_ip(ip: str) -> bool:
    import ipaddress

    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )

def assert_public_url(url: str) -> None:
    """校验 URL 公网可达性：仅 http(s)、拒绝保留网段，DNS 解析后逐 IP 校验。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise QQOfficeAPIError(-1, f"仅允许 http/https 外链: {url[:80]}")
    host = parsed.hostname
    if not host:
        raise QQOfficeAPIError(-1, f"URL 缺少主机名: {url[:80]}")
    import socket

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as e:
        raise QQOfficeAPIError(-1, f"域名解析失败: {host} ({e})")
    if not infos:
        raise QQOfficeAPIError(-1, f"域名解析结果为空: {host}")
    for info in infos:
        ip = info[4][0]
        if _is_reserved_ip(ip):
            raise QQOfficeAPIError(-1, f"拒绝访问内网/保留地址: {host} -> {ip}")

async def download_url(url: str, *, timeout: float = 30.0, max_bytes: int = 2 * 1024**3) -> bytes:
    """SSRF 校验后下载远程资源。"""
    assert_public_url(url)
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        resp = await client.get(url)
        if resp.status_code >= 400:
            raise QQOfficeAPIError(-1, f"下载失败 HTTP {resp.status_code}: {url[:80]}")
        data = resp.content
    if len(data) > max_bytes:
        raise QQOfficeAPIError(-1, f"下载内容超过上限 {max_bytes} 字节")
    return data

# ---------------- AMR/SILK ----------------

def strip_amr_header(data: bytes) -> tuple[bytes, bool]:
    """QQ 语音要求裸 SILK；.amr 实为带 #!AMR 头的 SILK，发前剥头。

    返回 (payload, was_stripped)。
    """
    if data.startswith(b"#!AMR"):
        idx = data.find(b"\n")
        if idx != -1:
            return data[idx + 1:], True
    return data, False

# ---------------- 图片尺寸 ----------------

def image_size(data: bytes) -> tuple[int, int]:
    """从图片头部解析 (宽, 高)；不支持/解析失败返回默认 512x512。只读前 64KB。"""
    head = data[:_IMAGE_HEADER_LIMIT]
    try:
        if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
            w, h = struct.unpack(">II", head[16:24])
            return int(w), int(h)
        if head.startswith(b"GIF8") and len(head) >= 10:
            w, h = struct.unpack("<HH", head[6:10])
            return int(w), int(h)
        if head.startswith(b"\xff\xd8"):
            return _jpeg_size(head)
        if head[:4] in (b"RIFF",) and head[8:12] == b"WEBP":
            return _webp_size(head)
    except (struct.error, IndexError, ValueError):
        pass
    return DEFAULT_IMAGE_SIZE

def _jpeg_size(head: bytes) -> tuple[int, int]:
    i = 2
    n = len(head)
    while i + 9 < n:
        if head[i] != 0xFF:
            i += 1
            continue
        marker = head[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # 无长度段
            i += 2
            continue
        seg_len = struct.unpack(">H", head[i + 2 : i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", head[i + 5 : i + 9])
            return int(w), int(h)
        i += 2 + seg_len
    return DEFAULT_IMAGE_SIZE

def _webp_size(head: bytes) -> tuple[int, int]:
    chunk = head[12:16]
    if chunk == b"VP8 " and len(head) >= 30:
        w = struct.unpack("<H", head[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", head[28:30])[0] & 0x3FFF
        return int(w), int(h)
    if chunk == b"VP8L" and len(head) >= 25:
        b = head[21:25]
        w = 1 + (((b[1] & 0x3F) << 8) | b[0])
        h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        return int(w), int(h)
    if chunk == b"VP8X" and len(head) >= 30:
        w = 1 + int.from_bytes(head[24:27], "little")
        h = 1 + int.from_bytes(head[27:30], "little")
        return int(w), int(h)
    return DEFAULT_IMAGE_SIZE

async def image_size_from(uploadable: Uploadable) -> tuple[int, int]:
    """惰性取图：bytes/base64/path 同步可解则不解包 url（url 场景官方默认 512）。"""
    if uploadable.kind == "url":
        return DEFAULT_IMAGE_SIZE
    data = uploadable.loaded_bytes()
    if data is None and uploadable.kind in ("base64", "path", "bytes"):
        data = await uploadable.load_bytes()
    if data is None:
        return DEFAULT_IMAGE_SIZE
    return image_size(data)

def amr_check(voice_bytes: bytes) -> bytes:
    """同步版 AMR 剥头，供发送语音前调用。"""
    payload, _ = strip_amr_header(voice_bytes)
    return payload
