"""显卡让路：对话用的小模型优先，后台大模型（消息总结、知识库问答用的 7B）用完就走。

8GB 显卡上，意图模型（约 1.1GB）+ 陪聊 3B（约 2GB）+ 语音合成已经占了大半；
总结用的 qwen2.5:7b 约 4.4GB，按 Ollama 默认会在显存里再待 5 分钟，把意图 / 陪聊模型挤出去，
下一句话就要冷加载，实测单轮慢 3~50 秒（logs/latency.jsonl）。这里做三件事：

1. background_keep_alive(model)：后台调用不是对话模型时返回 0，Ollama 回复完立刻卸载；
2. background_done()：后台大模型用完后，在后台把对话模型重新预热回显存（主程序启动时登记预热函数）；
3. seconds_since_interaction()：主人刚说过话时，定时总结往后推，不在对话中途抢显卡。
"""
import os
import threading
import time

_lock = threading.Lock()
_last_interaction = 0.0
_rewarm_fns = []
_rewarm_running = threading.Event()

# 主人说完话后这么多秒内，定时任务不占显卡
INTERACTIVE_GRACE_SEC = 180


def mark_interaction(now=None):
    global _last_interaction
    with _lock:
        _last_interaction = time.monotonic() if now is None else now


def seconds_since_interaction(now=None):
    with _lock:
        last = _last_interaction
    if not last:
        return float("inf")
    return (time.monotonic() if now is None else now) - last


def user_recently_active(grace=None):
    return seconds_since_interaction() < (INTERACTIVE_GRACE_SEC if grace is None else grace)


DEMO_FLAG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "demo_mode.flag")


def demo_mode():
    """演示模式（data/demo_mode.flag 存在）：陪聊模型一直常驻，定时总结暂停，显卡只留给对话。
    scripts\\演示模式开.bat / 演示模式关.bat 切换，不用重启团团。"""
    try:
        return os.path.exists(DEMO_FLAG)
    except Exception:
        return False


def _interactive_models():
    try:
        import canaan_config as cfg
        return {m for m in (getattr(cfg, "INTENT_COMPACT_MODEL", ""), getattr(cfg, "CHAT_MODEL", ""),
                            getattr(cfg, "CHAT_FALLBACK_MODEL", "")) if m}
    except Exception:
        return set()


def background_keep_alive(model):
    """后台调用该带的 keep_alive：对话模型保持原样（None = 不改），其他模型用完立刻卸载（0）。"""
    return None if model in _interactive_models() else 0


def shared_keep_alive():
    """后台请求用的就是对话模型时要带的常驻时间（不带的话 Ollama 会改回默认 5 分钟，之后把对话模型卸掉）。"""
    try:
        import canaan_config as cfg
        return getattr(cfg, "SHARED_KEEP_ALIVE", "24h")
    except Exception:
        return "24h"


def register_rewarm(fn):
    with _lock:
        if fn not in _rewarm_fns:
            _rewarm_fns.append(fn)


def background_done(model=None, wait=False):
    """后台大模型用完了：在后台把对话模型重新预热回来。同一时间只跑一次。"""
    if model is not None and model in _interactive_models():
        return None
    with _lock:
        fns = list(_rewarm_fns)
    if not fns or _rewarm_running.is_set():
        return None
    _rewarm_running.set()

    def _run():
        try:
            for fn in fns:
                try:
                    fn()
                except Exception as e:
                    print(f"[!] 对话模型重新预热失败: {type(e).__name__}")
        finally:
            _rewarm_running.clear()

    t = threading.Thread(target=_run, name="canaan-rewarm", daemon=True)
    t.start()
    if wait:
        t.join()
    return t


def _reset_for_tests():
    global _last_interaction
    with _lock:
        _last_interaction = 0.0
        _rewarm_fns.clear()
    _rewarm_running.clear()
