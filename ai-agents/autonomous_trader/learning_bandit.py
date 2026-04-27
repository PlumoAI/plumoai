from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class BanditArm:
    key: str
    pulls: int = 0
    reward_sum: float = 0.0

    @property
    def mean_reward(self) -> float:
        return self.reward_sum / self.pulls if self.pulls else 0.0


@dataclass
class Ucb1Bandit:
    arms: Dict[str, BanditArm] = field(default_factory=dict)
    total_pulls: int = 0

    def ensure_arms(self, keys: List[str]) -> None:
        for k in keys:
            if k not in self.arms:
                self.arms[k] = BanditArm(key=k)

    def select(self, keys: List[str], exploration: float = 2.0) -> str:
        self.ensure_arms(keys)

        # Pull each arm once
        for k in keys:
            if self.arms[k].pulls == 0:
                return k

        self.total_pulls = max(self.total_pulls, sum(a.pulls for a in self.arms.values()))
        log_n = math.log(self.total_pulls)
        best_k = keys[0]
        best_score = float("-inf")
        for k in keys:
            a = self.arms[k]
            bonus = math.sqrt((exploration * log_n) / a.pulls)
            score = a.mean_reward + bonus
            if score > best_score:
                best_score = score
                best_k = k
        return best_k

    def update(self, key: str, reward: float) -> None:
        if key not in self.arms:
            self.arms[key] = BanditArm(key=key)
        self.arms[key].pulls += 1
        self.arms[key].reward_sum += float(reward)
        self.total_pulls += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_pulls": self.total_pulls,
            "arms": {k: asdict(v) for k, v in self.arms.items()},
        }

    @classmethod
    def from_dict(cls, blob: Optional[Dict[str, Any]]) -> "Ucb1Bandit":
        blob = blob or {}
        arms_blob = blob.get("arms") or {}
        arms: Dict[str, BanditArm] = {}
        for k, v in arms_blob.items():
            if isinstance(v, dict):
                arms[k] = BanditArm(
                    key=str(v.get("key") or k),
                    pulls=int(v.get("pulls") or 0),
                    reward_sum=float(v.get("reward_sum") or 0.0),
                )
        return cls(arms=arms, total_pulls=int(blob.get("total_pulls") or 0))


def bounded_reward(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    if x != x:
        return 0.0
    return max(lo, min(hi, float(x)))


def random_reward_noise(scale: float = 0.01) -> float:
    return random.uniform(-scale, scale)

