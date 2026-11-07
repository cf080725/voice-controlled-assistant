# -*- coding: utf-8 -*-
"""
桌面伴侣的养成数值（不依赖 Tk，可单独测试）：饱腹度 / 心情 / 精力，0~100。

衰减（每小时）：
  饱腹度  12.5（满到空约 8 小时）
  心情    4；超过 1 小时没人摸头或聊天，再多掉 10（冷落）
  精力    主人在干活（写代码、伴读/番茄钟）时掉 50（连续 2 小时耗尽）；不干活时回 20，睡着时回 60
团团没开着的时间也算衰减，但最多按 8 小时算（隔几天再打开不会饿“死”）。

提醒（每种 20 分钟最多一次）：
  饱腹度 < 30：头顶冒小笼包，求投喂
  心情 < 20：傲娇别扭或委屈抹泪
  精力 < 10 且在干活：打哈欠/打瞌睡，提醒主人也休息
数据存在 data/canaan_pet_stats.json 的 "needs" 字段（与好感度同一个文件）。
"""
import time

HUNGER_PER_H = 12.5
MOOD_PER_H = 4.0
MOOD_LONELY_EXTRA_PER_H = 10.0
LONELY_AFTER_SEC = 3600.0
ENERGY_WORK_PER_H = 50.0
ENERGY_REST_PER_H = 20.0
ENERGY_SLEEP_PER_H = 60.0
OFFLINE_CAP_SEC = 8 * 3600.0
ALERT_COOLDOWN_SEC = 20 * 60.0

HUNGRY_BELOW, SAD_BELOW, TIRED_BELOW = 30.0, 20.0, 10.0

# 投喂：食物（多数与姿态同名；dessert 是“西域点心”，姿态用 cheer）→ (饱腹度, 心情, 精力)
FEED_VALUES = {
    "eating": (35, 5, 0), "takeout": (30, 4, 0), "zongzi": (25, 5, 0), "mooncake": (25, 5, 0),
    "dumpling": (25, 5, 0), "dessert": (20, 6, 0), "boba": (15, 6, 0), "coffee": (5, 2, 20), "tea": (5, 3, 15),
}


def _clamp(v):
    return max(0.0, min(100.0, float(v)))


class PetNeeds:
    def __init__(self, hunger=80.0, mood=80.0, energy=90.0, last_update=None, last_care=None, alerts=None):
        now = time.time()
        self.hunger, self.mood, self.energy = _clamp(hunger), _clamp(mood), _clamp(energy)
        self.last_update = now if last_update is None else float(last_update)
        self.last_care = now if last_care is None else float(last_care)   # 最近一次摸头/聊天/投喂
        self.alerts = dict(alerts or {})                                   # 提醒种类 → 上次提醒时间

    # ------------------------------ 存取 ------------------------------
    @classmethod
    def from_dict(cls, d, now=None):
        d = d or {}
        n = cls(d.get("hunger", 80), d.get("mood", 80), d.get("energy", 90),
                d.get("last_update"), d.get("last_care"), d.get("alerts"))
        now = time.time() if now is None else now
        # 没开着的这段时间：按休息算衰减，最多 8 小时
        off = max(0.0, min(OFFLINE_CAP_SEC, now - n.last_update))
        n.last_update = now - off
        n.last_care = now          # 关着的时候不算被冷落（主人可能只是关了电脑），重新打开从头算
        n.tick(now, working=False, sleeping=False)
        return n

    def to_dict(self):
        return {"hunger": round(self.hunger, 2), "mood": round(self.mood, 2), "energy": round(self.energy, 2),
                "last_update": self.last_update, "last_care": self.last_care, "alerts": self.alerts}

    # ------------------------------ 衰减 ------------------------------
    def tick(self, now=None, working=False, sleeping=False):
        now = time.time() if now is None else now
        dt_h = max(0.0, now - self.last_update) / 3600.0
        self.last_update = now
        if dt_h <= 0:
            return
        self.hunger = _clamp(self.hunger - HUNGER_PER_H * dt_h)
        lonely = now - self.last_care > LONELY_AFTER_SEC
        self.mood = _clamp(self.mood - (MOOD_PER_H + (MOOD_LONELY_EXTRA_PER_H if lonely else 0)) * dt_h)
        if working:
            self.energy = _clamp(self.energy - ENERGY_WORK_PER_H * dt_h)
        else:
            self.energy = _clamp(self.energy + (ENERGY_SLEEP_PER_H if sleeping else ENERGY_REST_PER_H) * dt_h)

    # ------------------------------ 互动 ------------------------------
    def feed(self, pose, now=None):
        h, m, e = FEED_VALUES.get(pose, (0, 0, 0))
        self.hunger, self.mood, self.energy = _clamp(self.hunger + h), _clamp(self.mood + m), _clamp(self.energy + e)
        self.last_care = time.time() if now is None else now
        return h > 0 or e > 0

    def pat(self, now=None):
        self.mood = _clamp(self.mood + 8)
        self.last_care = time.time() if now is None else now

    def chatted(self, now=None):
        self.mood = _clamp(self.mood + 4)
        self.last_care = time.time() if now is None else now

    # ------------------------------ 提醒 ------------------------------
    def due_alerts(self, now=None, working=False, limit=None):
        """返回现在该提醒的种类列表（"hungry" / "sad" / "tired"），并记下提醒时间。
        limit=1 时只取一件（其余不记时间，下次再提醒）。"""
        now = time.time() if now is None else now
        out = []
        for kind, cond in (("hungry", self.hunger < HUNGRY_BELOW), ("sad", self.mood < SAD_BELOW),
                           ("tired", working and self.energy < TIRED_BELOW)):
            if cond and now - self.alerts.get(kind, float("-inf")) >= ALERT_COOLDOWN_SEC:
                if limit is not None and len(out) >= limit:
                    break
                self.alerts[kind] = now
                out.append(kind)
        return out

    @property
    def hungry(self):
        return self.hunger < HUNGRY_BELOW
