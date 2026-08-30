"""引用索引（REFIDX）存储 + file_info 缓存。

官方没有「按 msg_id 查历史消息」的 API，引用回复的 message_id 实为
REFIDX_xxx 索引：非机器人消息取入站 message_scene.ext 的 msg_idx
（msg_elements[0].msg_idx 兜底），机器人消息取发送响应的 ext_info.ref_idx。
两者在此持久化（JSONL + TTL + LRU），是发送引用回复的前置依赖。

file_info 缓存：key=内容hash:场景:openid:file_type，
有效期=官方 ttl-60s（下限 10s），仅 file_data 路径缓存、URL 直传不缓存。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from pathlib import Path

__all__ = ["RefStore", "parse_scene_ext"]

_MAX_FILE_CACHE_TTL = 10.0  # 秒，下限

def parse_scene_ext(ext_list) -> dict[str, str]:
    """把 message_scene.ext（["k=v", ...]）解析成 dict，提取 msg_idx/ref_msg_idx。"""
    out: dict[str, str] = {}
    for item in ext_list or []:
        if isinstance(item, str) and "=" in item:
            k, _, v = item.partition("=")
            out[k.strip()] = v.strip()
        elif isinstance(item, dict):
            k = item.get("k") or item.get("key")
            v = item.get("v") or item.get("value")
            if k:
                out[str(k)] = str(v)
    return out

class RefStore:
    """ref-index 持久化存储（JSONL 追加 + 启动裁剪 + LRU 上限）。"""

    def __init__(self, data_dir: Path | None = None, *, ttl_days: int = 7, max_entries: int = 50000):
        self.ttl_seconds = max(0, int(ttl_days)) * 86400
        self.max_entries = max(1000, int(max_entries))
        self._mem: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._path: Path | None = None
        self._hit = 0
        self._miss = 0
        self._file_cache: dict[str, tuple[float, str]] = {}
        if data_dir is not None and self.ttl_seconds > 0:
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                self._path = data_dir / "refindex.jsonl"
                self._load()
            except Exception:
                self._path = None

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        cutoff = time.time() - self.ttl_seconds
        lines: list[str] = []
        try:
            raw = self._path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            ts = float(obj.get("ts", 0))
            if ts < cutoff:
                continue
            self._mem[obj["k"]] = (ts, obj["v"])
            lines.append(line)
        self._prune_to_limit()
        # 启动时重写一次完成裁剪
        try:
            self._path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except OSError:
            pass

    def _append(self, key: str, value: str, ts: float) -> None:
        if self._path is None:
            return
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"k": key, "v": value, "ts": ts}, ensure_ascii=False) + "\n")
        except OSError:
            pass
        # 超过约 2×条目上限的体积即压缩重写
        try:
            if self._path.stat().st_size > 128 * self.max_entries:
                self._load()
        except OSError:
            pass

    def _prune_to_limit(self) -> None:
        while len(self._mem) > self.max_entries:
            self._mem.popitem(last=False)

    def store(self, key: str, ref_id: str) -> None:
        ts = time.time()
        self._mem[key] = (ts, ref_id)
        self._mem.move_to_end(key)
        self._prune_to_limit()
        self._append(key, ref_id, ts)

    def get(self, key: str) -> str | None:
        entry = self._mem.get(key)
        if entry is None:
            self._miss += 1
            return None
        ts, value = entry
        if self.ttl_seconds and time.time() - ts > self.ttl_seconds:
            self._mem.pop(key, None)
            self._miss += 1
            return None
        self._mem.move_to_end(key)
        self._hit += 1
        return value

    @staticmethod
    def _inbound_key(scene: str, openid: str, msg_id: str) -> str:
        return f"in:{scene}:{openid}:{msg_id}"

    @staticmethod
    def _outbound_key(scene: str, openid: str) -> str:
        return f"out:{scene}:{openid}:latest"

    def record_inbound(self, scene: str, openid: str, msg_id: str, data: dict) -> str | None:
        """入站事件：存 message_scene.ext 的 msg_idx（引用消息再从
        msg_elements[0].msg_idx 兜底）。返回存入的 ref 值。"""
        if not msg_id:
            return None
        scene_ext = parse_scene_ext(((data.get("message_scene") or {}).get("ext")))
        ref = scene_ext.get("msg_idx")
        if not ref:
            for el in (data.get("msg_elements") or data.get("elements") or []):
                if isinstance(el, dict) and el.get("msg_idx"):
                    ref = str(el["msg_idx"])
                    break
        if ref:
            self.store(self._inbound_key(scene, openid, msg_id), ref)
        return ref

    def record_outbound(self, scene: str, openid: str, response: dict, local_key: str | None = None) -> str | None:
        """出站响应：存 ext_info.ref_idx；local_key 可选（调用方自定义键）。"""
        ref = ((response or {}).get("ext_info") or {}).get("ref_idx")
        if not ref:
            return None
        self.store(self._outbound_key(scene, openid), str(ref))
        if local_key:
            self.store(f"out:{scene}:{openid}:{local_key}", str(ref))
        return str(ref)

    def get_inbound(self, scene: str, openid: str, msg_id: str) -> str | None:
        return self.get(self._inbound_key(scene, openid, msg_id))

    def get_outbound_latest(self, scene: str, openid: str) -> str | None:
        return self.get(self._outbound_key(scene, openid))

    @staticmethod
    def file_cache_key(content_hash: str, scene: str, openid: str, file_type: int) -> str:
        return f"{content_hash}:{scene}:{openid}:{file_type}"

    def cache_file_info(self, content_hash: str, scene: str, openid: str, file_type: int,
                        file_info: str, ttl: float) -> None:
        """file_data 上传路径专用缓存；URL 直传路径不缓存。"""
        if not file_info:
            return
        ttl = max(_MAX_FILE_CACHE_TTL, float(ttl) - 60.0) if ttl else _MAX_FILE_CACHE_TTL
        self._file_cache[self.file_cache_key(content_hash, scene, openid, file_type)] = (
            time.monotonic() + ttl,
            file_info,
        )

    def get_file_info(self, content_hash: str, scene: str, openid: str, file_type: int) -> str | None:
        key = self.file_cache_key(content_hash, scene, openid, file_type)
        entry = self._file_cache.get(key)
        if entry is None:
            return None
        expire_at, file_info = entry
        if time.monotonic() > expire_at:
            self._file_cache.pop(key, None)
            return None
        return file_info

    @staticmethod
    def content_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()[:32]

    def snapshot(self) -> dict:
        return {
            "entries": len(self._mem),
            "hit": self._hit,
            "miss": self._miss,
            "file_cache": len(self._file_cache),
            "persisted": bool(self._path and self._path.exists()),
            "ttl_days": self.ttl_seconds // 86400 if self.ttl_seconds else 0,
        }

    def close(self) -> None:
        """无缓冲句柄，无需刷盘；保留接口以对齐 terminate 生命周期。"""
        return None
