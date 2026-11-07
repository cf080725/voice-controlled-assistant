# -*- coding: utf-8 -*-
import canaan_pet_needs as pn

H = 3600.0


def test_hunger_decays_to_hungry_in_about_4_hours():
    n = pn.PetNeeds(hunger=80, last_update=0, last_care=0)
    n.tick(4 * H)
    assert abs(n.hunger - 30) < 1e-6                     # 80 - 12.5*4
    n.tick(4 * H + 60)
    assert n.hungry


def test_mood_drops_faster_when_lonely():
    a = pn.PetNeeds(mood=80, last_update=0, last_care=0)
    a.tick(0.5 * H)                                      # 半小时内不算冷落
    assert abs(a.mood - 78) < 1e-6
    b = pn.PetNeeds(mood=80, last_update=2 * H, last_care=0)
    b.tick(3 * H)                                        # 已经 2 小时没人理
    assert abs(b.mood - (80 - 14)) < 1e-6


def test_energy_runs_out_after_two_hours_work_and_recovers():
    n = pn.PetNeeds(energy=100, last_update=0, last_care=0)
    n.tick(2 * H, working=True)
    assert n.energy == 0
    n.tick(3 * H, working=False)
    assert abs(n.energy - 20) < 1e-6
    n.tick(4 * H, sleeping=True)
    assert abs(n.energy - 80) < 1e-6


def test_feed_pat_chat_restore():
    n = pn.PetNeeds(hunger=10, mood=10, energy=50, last_update=0, last_care=0)
    assert n.feed("eating", now=1)
    assert n.hunger == 45 and n.mood == 15 and n.last_care == 1
    n.feed("coffee", now=2)
    assert n.energy == 70
    n.pat(now=3)
    n.chatted(now=4)
    assert n.mood == 15 + 2 + 8 + 4 and n.last_care == 4
    assert not n.feed("dance")                           # 不是吃的
    n2 = pn.PetNeeds(hunger=95)
    n2.feed("eating")
    assert n2.hunger == 100                              # 不超过 100


def test_alerts_thresholds_and_cooldown():
    n = pn.PetNeeds(hunger=20, mood=10, energy=5, last_update=0, last_care=0)
    assert sorted(n.due_alerts(now=100, working=True)) == ["hungry", "sad", "tired"]
    assert n.due_alerts(now=200, working=True) == []     # 20 分钟内不重复
    assert sorted(n.due_alerts(now=100 + 20 * 60, working=False)) == ["hungry", "sad"]   # 不干活不提醒累
    full = pn.PetNeeds(hunger=90, mood=90, energy=90)
    assert full.due_alerts(working=True) == []


def test_offline_decay_capped_and_not_lonely():
    d = pn.PetNeeds(hunger=100, mood=80, energy=10, last_update=0, last_care=0).to_dict()
    n = pn.PetNeeds.from_dict(d, now=3 * 24 * H)         # 关了 3 天
    assert abs(n.hunger - 0) < 1e-6                      # 只按 8 小时算：100 - 100
    assert abs(n.mood - (80 - 32)) < 1e-6                # 8 小时 × 4，不算冷落
    assert abs(n.energy - 100) < 1e-6                    # 休息回满
    assert n.last_care == 3 * 24 * H
    n2 = pn.PetNeeds.from_dict(pn.PetNeeds(hunger=60, last_update=0).to_dict(), now=H)
    assert abs(n2.hunger - 47.5) < 1e-6                  # 关了 1 小时照常衰减


def test_roundtrip_and_defaults():
    n = pn.PetNeeds.from_dict(None, now=1000.0)
    assert (n.hunger, n.mood, n.energy) == (80, 80, 90)
    d = n.to_dict()
    assert set(d) == {"hunger", "mood", "energy", "last_update", "last_care", "alerts"}
