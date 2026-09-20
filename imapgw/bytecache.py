"""A byte-budgeted LRU cache for immutable message bytes."""

from __future__ import annotations

from collections import OrderedDict


class ByteCache:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(0, max_bytes)
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0

    @property
    def size(self) -> int:
        return self._size

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: str) -> bool:
        return key in self._items

    def get(self, key: str) -> bytes | None:
        data = self._items.get(key)
        if data is not None:
            self._items.move_to_end(key)
        return data

    def put(self, key: str, data: bytes) -> None:
        if not isinstance(data, bytes | bytearray):
            raise TypeError("cache values must be bytes")
        data = bytes(data)
        if len(data) > self.max_bytes:
            self._items.pop(key, None)
            return
        old = self._items.pop(key, None)
        if old is not None:
            self._size -= len(old)
        self._items[key] = data
        self._size += len(data)
        while self._size > self.max_bytes and self._items:
            _, evicted = self._items.popitem(last=False)
            self._size -= len(evicted)

    def discard(self, key: str) -> None:
        old = self._items.pop(key, None)
        if old is not None:
            self._size -= len(old)

    def clear(self) -> None:
        self._items.clear()
        self._size = 0
