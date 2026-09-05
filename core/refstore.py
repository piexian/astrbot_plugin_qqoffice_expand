"""引用索引（REFIDX）存储 + file_info 缓存（N 实例身份命名空间版）。

键带机器人身份前缀（`APPID@environment|scene`），同身份多实例共享、
不同身份隔离；容量上限为全插件一份（不随实例数增长）。旧版本的无身份
JSONL 文件不会被读取归属到任何机器人，也不删除用户历史文件。

官方没有「按 msg_id 查历史消息」的 API，引用回复的 message_id 实为
REFIDX_xxx 索引：非机器人消息取入站 message_scene.ext 的 msg_idx，
机器人消息取发送响应的 ext_info.ref_idx。
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
    """ref-index 持久化存储（JSONL 追加 + 启动裁剪 + 全局 LRU 上限）。

    max_entries 是全插件总容量，与实例数无关。
    """

    LEGACY_PREFIXES = ("in:", "out:")   # 旧版无身份前缀：只读兼容查一次，不写入归属

    def __init__(self, data_dir: Path | None = None, *, ttl_days: int = 7,
                 max_entries: int = 50000):
        self.ttl_seconds = max(0, int(ttl_days)) * 86400
        self.max_entries = max(1000, int(max_entries))
        self._mem: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._path: Path | None = None
        self._legacy_path: Path | None = None
        self._hit = 0
        self._miss = 0
        self._file_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        self._file_cache_max = 512   # 全插件 file_info 缓存上限（不乘以实例数）
        self._compact_threshold = 128 * self.max_entries   # 压缩触发阈值（压缩后翻倍）
        if data_dir is not None and self.ttl_seconds > 0:
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                self._path = data_dir / "refindex.v2.jsonl"
                self._load()
                # 旧文件只读迁移窗口：不把旧记录归给首个机器人；仅保留文件本身。
                self._legacy_path = data_dir / "refindex.jsonl"
            except Exception:
                self._path = None

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        cutoff = time.time() - self.ttl_seconds
        keep: dict[str, tuple[float, str]] = {}   # 每 key 只保留最新记录
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
            prev = keep.get(obj["k"])
            if prev is None or ts >= prev[0]:
                keep[obj["k"]] = (ts, obj["v"])
        for key, (ts, value) in keep.items():
            self._mem.pop(key, None)   # 先删后插：重复 key 按最新出现顺序排列
            self._mem[key] = (ts, value)
        self._prune_to_limit()
        self._rewrite_compacted()

    def _rewrite_compacted(self) -> None:
        """压缩重写：只写未过期、容量内的每 key 最新记录；

        下一次触发阈值按压缩后实际体积翻倍（与保底阈值取 max），
        保证有足够追加余量，不会压缩后仍高于固定阈值而每次全扫。
        """
        if self._path is None:
            return
        lines = [
            json.dumps({"k": k, "v": v, "ts": ts}, ensure_ascii=False)
            for k, (ts, v) in self._mem.items()
        ]
        try:
            self._path.write_text("\n".join(lines) + ("\n" if lines else ""),
                                  encoding="utf-8")
            size = self._path.stat().st_size
            baseline = 128 * self.max_entries
            self._compact_threshold = max(baseline, size * 2)
        except OSError:
            pass

    def _append(self, key: str, value: str, ts: float) -> None:
        if self._path is None:
            return
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"k": key, "v": value, "ts": ts}, ensure_ascii=False) + "\n")
            if self._path.stat().st_size > self._compact_threshold:
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
    def inbound_key(scene: str, openid: str, msg_id: str) -> str:
        return f"in:{scene}:{openid}:{msg_id}"

    @staticmethod
    def outbound_key(scene: str, openid: str) -> str:
        return f"out:{scene}:{openid}:latest"

    def record_inbound(self, scene: str, openid: str, msg_id: str, data: dict) -> str | None:
        """入站事件：scene 需已带身份前缀（`PREFIX|group`）。存 msg_idx。"""
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
            self.store(self.inbound_key(scene, openid, msg_id), ref)
        return ref

    def record_outbound(self, scene: str, openid: str, response: dict,
                        local_key: str | None = None) -> str | None:
        ref = ((response or {}).get("ext_info") or {}).get("ref_idx")
        if not ref:
            return None
        self.store(self.outbound_key(scene, openid), str(ref))
        if local_key:
            self.store(f"out:{scene}:{openid}:{local_key}", str(ref))
        return str(ref)

    def get_inbound(self, scene: str, openid: str, msg_id: str) -> str | None:
        return self.get(self.inbound_key(scene, openid, msg_id))

    def get_outbound_latest(self, scene: str, openid: str) -> str | None:
        return self.get(self.outbound_key(scene, openid))

    @staticmethod
    def file_cache_key(content_hash: str, scene: str, openid: str, file_type: int) -> str:
        return f"{content_hash}:{scene}:{openid}:{file_type}"

    def cache_file_info(self, content_hash: str, scene: str, openid: str, file_type: int,
                        file_info: str, ttl: float) -> None:
        """file_data 上传路径专用缓存（content_hash 已含身份前缀）；全局 LRU 上限。"""
        if not file_info:
            return
        ttl = max(_MAX_FILE_CACHE_TTL, float(ttl) - 60.0) if ttl else _MAX_FILE_CACHE_TTL
        key = self.file_cache_key(content_hash, scene, openid, file_type)
        self._file_cache.pop(key, None)
        self._file_cache[key] = (time.monotonic() + ttl, file_info)
        while len(self._file_cache) > self._file_cache_max:
            self._file_cache.popitem(last=False)   # LRU 淘汰最旧

    def get_file_info(self, content_hash: str, scene: str, openid: str, file_type: int) -> str | None:
        key = self.file_cache_key(content_hash, scene, openid, file_type)
        entry = self._file_cache.get(key)
        if entry is None:
            return None
        expire_at, file_info = entry
        if time.monotonic() > expire_at:
            self._file_cache.pop(key, None)
            return None
        self._file_cache.move_to_end(key)   # 命中更新 LRU 顺序
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
            "legacy_file_kept": bool(self._legacy_path and self._legacy_path.exists()),
            "ttl_days": self.ttl_seconds // 86400 if self.ttl_seconds else 0,
        }

    def close(self) -> None:
        """无缓冲句柄，无需刷盘；保留接口以对齐 terminate 生命周期。"""
        return None
