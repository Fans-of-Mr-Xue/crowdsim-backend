"""Bounded experience replay for online artificial-society diagnostics."""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import random
from typing import Iterable

from crowdsim.modeling_algorithm.contracts import Experience


METHOD_NAME = "经验回放"
IMPLEMENTED = True


class ExperienceReplayBuffer:
    method_name = METHOD_NAME

    def __init__(self, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError("experience replay capacity must be positive")
        self.capacity = int(capacity)
        self._items: deque[Experience] = deque(maxlen=self.capacity)

    def append(self, experience: Experience) -> None:
        self._items.append(experience)

    def sample(self, batch_size: int, *, token: str = "") -> tuple[Experience, ...]:
        size = min(max(0, int(batch_size)), len(self._items))
        if size == 0:
            return ()
        seed = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
        return tuple(random.Random(seed).sample(list(self._items), size))

    def action_values(self, *, role_id: str | None = None) -> dict[str, float]:
        rewards: dict[str, list[float]] = defaultdict(list)
        for item in self._items:
            if role_id is None or item.role_id == role_id:
                rewards[item.action].append(item.reward)
        return {
            action: sum(values) / len(values)
            for action, values in rewards.items()
            if values
        }

    def diagnostics(self) -> dict[str, int]:
        return {"size": len(self._items), "capacity": self.capacity}

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterable[Experience]:
        return iter(tuple(self._items))
