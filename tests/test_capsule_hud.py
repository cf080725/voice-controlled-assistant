# -*- coding: utf-8 -*-
"""测试顶部灵动胶囊 (Dynamic Island) 折叠、展开、纯伴侣隐藏模式与桌宠外观升级。"""
import os
import sys
import time
import tkinter as tk
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import canaan_companion_pet as ccp
import canaan_desk_os_hub as hub

_SHARED_ROOT = None


def _get_shared_root():
    from tests._tk import shared_root
    return shared_root()


def test_hub_capsule_collapse_and_expand():
    root = _get_shared_root()
    h = object.__new__(hub.CanaanDeskOSHub)
    h.root = root
    h.mini_w = 260
    h.mini_h = 36
    h.expanded_w = 640
    h.expanded_h = 88
    h.is_collapsed = True
    h.is_hidden = False
    h._autofold_job = None

    h.mini_capsule = MagicMock()
    h.expanded_panel = MagicMock()

    # 1. Expand
    h.expand_capsule()
    assert h.is_collapsed is False
    assert h.mini_capsule.pack_forget.called
    assert h.expanded_panel.pack.called

    # 2. Collapse
    h.collapse_capsule()
    assert h.is_collapsed is True
    assert h.expanded_panel.pack_forget.called
    assert h.mini_capsule.pack.called


def test_hub_capsule_hide_and_show():
    root = _get_shared_root()
    h = object.__new__(hub.CanaanDeskOSHub)
    h.root = root
    h.mini_w = 260
    h.mini_h = 36
    h.expanded_w = 640
    h.expanded_h = 88
    h.is_collapsed = True
    h.is_hidden = False
    h._autofold_job = None
    h.mini_capsule = MagicMock()
    h.expanded_panel = MagicMock()
    h.pet = None

    # Hide
    h.hide_top_bar()
    assert h.is_hidden is True

    # Show
    h.show_top_bar()
    assert h.is_hidden is False
    assert h.is_collapsed is True


def test_pet_delegates_to_hub():
    root = _get_shared_root()
    mock_hub = MagicMock()
    mock_hub.is_hidden = False

    pet = ccp.CanaanCompanionPet(root, is_speaking_provider=None)
    pet.hub = mock_hub

    pet.toggle_hub_capsule()
    assert mock_hub.toggle_top_bar.called

    pet.hide_hub()
    assert mock_hub.hide_top_bar.called

    pet.show_hub()
    assert mock_hub.show_top_bar.called
