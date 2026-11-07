# 把 Claude Code 的进度接到团团

Claude Code 等你处理、或者做完一轮时，团团的灵动岛会弹出提示。钩子只报状态、项目文件夹名和一句提示，不报对话内容；它只能报进度，不能让团团执行任何东西。

## 1. 给 Claude Code 单独配对一个只能报进度的令牌

```bash
python canaan.py api --pair "Claude Code" --caps progress
```

把打印出来的令牌存成用户环境变量 `CANAAN_PROGRESS_TOKEN`（Windows：设置 → 系统 → 关于 → 高级系统设置 → 环境变量）。

## 2. 在 Claude Code 的设置里加钩子

在 `~/.claude/settings.json`（全局）或项目的 `.claude/settings.json` 里加上下面这段，把路径换成你本机的：

```json
{
  "hooks": {
    "Notification": [{ "hooks": [{ "type": "command", "command": "python <团团目录>/integrations/claude_code_hooks/canaan_progress_hook.py" }] }],
    "Stop": [{ "hooks": [{ "type": "command", "command": "python <团团目录>/integrations/claude_code_hooks/canaan_progress_hook.py" }] }]
  }
}
```

## 其他智能体

任何程序都可以用一个 HTTP 请求接进来（需要 `progress` 能力的令牌）：

```
POST http://127.0.0.1:8789/api/progress
Authorization: Bearer <令牌>
Content-Type: application/json

{"kind": "progress" | "waiting" | "done" | "failed" | "info", "title": "最多 80 字", "progress": 0-100（可选）, "key": "同一件事用同一个 key，后面的更新会覆盖前面的（可选）"}
```

提示的来源显示为配对时起的名字，不能冒充别的应用。
