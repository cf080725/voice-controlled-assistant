# -*- coding: utf-8 -*-
"""跨进程文件锁回归：同一把锁不能同时被两个持有者拿到。"""
import pytest
from canaan_filelock import FileLockTimeout, file_lock


def test_file_lock_blocks_second_holder(tmp_path):
    target = tmp_path / "x.jsonl"
    with file_lock(target, timeout=0.5):
        with pytest.raises(FileLockTimeout):
            with file_lock(target, timeout=0.1):
                pass
    with file_lock(target, timeout=0.5):
        pass


def test_lock_file_is_created(tmp_path):
    target = tmp_path / "x.jsonl"
    with file_lock(target):
        pass
    assert (tmp_path / "x.jsonl.lock").exists()