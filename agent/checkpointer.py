"""LangGraph checkpointer on plain Redis keys (works on Upstash, which has no RediSearch/RedisJSON).

LangGraph hands `put` only the channels that changed in a step; unchanged channels keep their earlier
version. So, like LangGraph's own InMemorySaver, channel values are stored per (channel, version) and a
checkpoint is rebuilt from its `channel_versions` on read.

Layout per thread (all keys expire after `ttl` seconds, refreshed on every write):
  ckpt:{thread}:{ns}:idx                  sorted set of checkpoint ids (lexicographic = time order)
  ckpt:{thread}:{ns}:{id}                 hash: checkpoint (without values), metadata, parent id
  ckpt:{thread}:{ns}:blobs                hash: "{channel}|{version}" -> encoded value
  ckpt:{thread}:{ns}:{id}:writes          hash: "{task_id}|{idx}" -> pending write
Values are encoded with the saver's serde (`dumps_typed`), so nothing is pickled by hand.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Iterator, Sequence
from typing import Any

import redis
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (WRITES_IDX_MAP, BaseCheckpointSaver, ChannelVersions, Checkpoint,
                                       CheckpointMetadata, CheckpointTuple, get_checkpoint_id, get_checkpoint_metadata)


def _enc(typed: tuple[str, bytes]) -> bytes:
    t, b = typed
    return t.encode() + b"\x00" + b


def _dec(raw: bytes) -> tuple[str, bytes]:
    t, _, b = raw.partition(b"\x00")
    return t.decode(), b


class RedisSaver(BaseCheckpointSaver):
    def __init__(self, client: redis.Redis, *, ttl: int = 24 * 3600, prefix: str = "ckpt") -> None:
        super().__init__()
        self.r = client
        self.ttl = ttl
        self.prefix = prefix

    @classmethod
    def from_url(cls, url: str, **kw) -> "RedisSaver":
        return cls(redis.Redis.from_url(url, decode_responses=False, socket_timeout=5, socket_connect_timeout=5), **kw)

    def _base(self, thread_id: str, ns: str) -> str:
        return f"{self.prefix}:{thread_id}:{ns}"

    # ----- read -----
    def _values(self, base: str, versions: dict) -> dict[str, Any]:
        if not versions:
            return {}
        fields = [f"{ch}|{v}".encode() for ch, v in versions.items()]
        raw = self.r.hmget(f"{base}:blobs", fields)
        out = {}
        for (ch, _), val in zip(versions.items(), raw):
            if val is None:
                continue
            typed = _dec(val)
            if typed[0] != "empty":
                out[ch] = self.serde.loads_typed(typed)
        return out

    def _tuple(self, thread_id: str, ns: str, checkpoint_id: str, config: RunnableConfig | None = None) -> CheckpointTuple | None:
        base = self._base(thread_id, ns)
        h = self.r.hgetall(f"{base}:{checkpoint_id}")
        if not h:
            return None
        checkpoint = self.serde.loads_typed(_dec(h[b"c"]))
        checkpoint = {**checkpoint, "channel_values": self._values(base, checkpoint.get("channel_versions", {}))}
        metadata = self.serde.loads_typed(_dec(h[b"m"]))
        parent = h.get(b"p", b"").decode() or None
        writes_raw = self.r.hgetall(f"{base}:{checkpoint_id}:writes")
        writes = []
        for k in sorted(writes_raw, key=lambda k: (k.decode().split("|")[0], int(k.decode().split("|")[1]))):
            w = json.loads(writes_raw[k])
            writes.append((w["task_id"], w["channel"], self.serde.loads_typed((w["t"], base64.b64decode(w["b"])))))
        cfg = config or {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}}
        return CheckpointTuple(
            config=cfg, checkpoint=checkpoint, metadata=metadata, pending_writes=writes,
            parent_config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": parent}}
            if parent else None)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        if checkpoint_id := get_checkpoint_id(config):
            return self._tuple(thread_id, ns, checkpoint_id, config)
        latest = self.r.zrevrangebylex(f"{self._base(thread_id, ns)}:idx", "+", "-", start=0, num=1)
        return self._tuple(thread_id, ns, latest[0].decode()) if latest else None

    def list(self, config: RunnableConfig | None, *, filter: dict[str, Any] | None = None,
             before: RunnableConfig | None = None, limit: int | None = None) -> Iterator[CheckpointTuple]:
        if config is None:
            return  # listing across all threads is not supported on this store
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        ids = [i.decode() for i in self.r.zrevrangebylex(f"{self._base(thread_id, ns)}:idx", "+", "-")]
        want = get_checkpoint_id(config)
        before_id = get_checkpoint_id(before) if before else None
        n = 0
        for cid in ids:
            if want and cid != want:
                continue
            if before_id and cid >= before_id:
                continue
            t = self._tuple(thread_id, ns, cid)
            if t is None:
                continue
            if filter and not all(t.metadata.get(k) == v for k, v in filter.items()):
                continue
            yield t
            n += 1
            if limit is not None and n >= limit:
                return

    # ----- write -----
    def put(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
            new_versions: ChannelVersions) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        base = self._base(thread_id, ns)
        cid = checkpoint["id"]
        c = checkpoint.copy()
        values: dict[str, Any] = c.pop("channel_values", {})  # type: ignore[misc]
        p = self.r.pipeline(transaction=False)
        if new_versions:
            p.hset(f"{base}:blobs", mapping={
                f"{ch}|{v}": _enc(self.serde.dumps_typed(values[ch]) if ch in values else ("empty", b""))
                for ch, v in new_versions.items()})
        p.hset(f"{base}:{cid}", mapping={
            "c": _enc(self.serde.dumps_typed(c)),
            "m": _enc(self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))),
            "p": config["configurable"].get("checkpoint_id") or "",
        })
        p.zadd(f"{base}:idx", {cid: 0})
        for k in (f"{base}:{cid}", f"{base}:idx", f"{base}:blobs"):
            p.expire(k, self.ttl)
        p.execute()
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": cid}}

    def put_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        key = f"{self._base(thread_id, ns)}:{config['configurable']['checkpoint_id']}:writes"
        p = self.r.pipeline(transaction=False)
        for idx, (channel, value) in enumerate(writes):
            field = f"{task_id}|{WRITES_IDX_MAP.get(channel, idx)}"
            t, b = self.serde.dumps_typed(value)
            payload = json.dumps({"task_id": task_id, "channel": channel, "t": t, "b": base64.b64encode(b).decode(),
                                  "path": task_path})
            if WRITES_IDX_MAP.get(channel, idx) >= 0:
                p.hsetnx(key, field, payload)
            else:
                p.hset(key, field, payload)
        p.expire(key, self.ttl)
        p.execute()

    def delete_thread(self, thread_id: str) -> None:
        for k in self.r.scan_iter(match=f"{self.prefix}:{thread_id}:*", count=200):
            self.r.delete(k)

    def get_next_version(self, current: str | None, channel: None) -> str:
        # same scheme as InMemorySaver: monotonically increasing, string-sortable
        import random
        current_v = 0 if current is None else (current if isinstance(current, int) else int(str(current).split(".")[0]))
        return f"{current_v + 1:032}.{random.random():016}"

    # async variants delegate to the sync client (the agent runs one turn at a time per thread)
    async def aget_tuple(self, config):
        return self.get_tuple(config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        for t in self.list(config, filter=filter, before=before, limit=limit):
            yield t

    async def aput(self, config, checkpoint, metadata, new_versions):
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id):
        return self.delete_thread(thread_id)
