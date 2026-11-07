# 桌宠形象包格式

桌宠的样子由“形象包”决定。默认形象是代码画的原创小精灵“团团”（`shells/canaan_default_pack.py`）。你可以做自己的形象包，在桌宠右键菜单 → 形象 → 导入形象包。导入时会先完整检查，检查通过才复制到 `data/packs/`，所以原文件挪走也不影响。

**只导入你有权使用的形象**：自己画的、买了授权的、或者作者明确允许再使用的都可以。游戏、动画、影视角色和别人的画作，没有授权就不要做成包公开分享。

格式兼容共创平台（desktop-pet-community）的 `pet.json`。

## 目录结构

```
my-pet.zip
├── pet.json
└── spritesheet.png        # 也可以是 .webp
```

也可以不打包，直接选文件夹里的 `pet.json`。

## pet.json

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 小写字母或数字开头，最多 64 位，只能用 `a-z 0-9 _ -`；不能是 `builtin` |
| `displayName` | 否 | 菜单里显示的名字，最多 60 字 |
| `spriteVersionNumber` | 否 | 写 `2` 表示 V2 图集（有 16 个注视方向）；不写就是 V1 |
| `spritesheetPath` | 否 | 图集文件名，默认 `spritesheet.webp` |
| `stateBindings` | 否 | 把状态换绑到别的动作，如 `{"waiting": "review"}` |
| `mediaVersion` | 否 | 写 `1` 表示用动作包（每个动作一个 .webp / .mp4 / .webm），见下文 |

## 图集

- 每格 192×208 像素，8 列。
- V1 共 9 行（1536×1872），V2 共 11 行（1536×2288）。
- 背景要透明。每行从左往右读，遇到第一个全透明的格子就停，所以一行可以不画满。

| 行 | 动作 | 什么时候用 |
|---|---|---|
| 0 | idle | 待机，最多 6 帧；V2 第 0 行第 7 格（下标 6）是正面待机 |
| 1 | running-right | 向右走 |
| 2 | running-left | 向左走 |
| 3 | waving | 打招呼、开心 |
| 4 | jumping | 庆祝、办好了 |
| 5 | failed | 没办成 |
| 6 | waiting | 等你确认 / 等你处理 |
| 7 | running | 正在干活 |
| 8 | review | 查看、核对 |
| 9–10（仅 V2） | 注视 | 16 个方向，从正上方开始顺时针，每格 22.5° |

缺了的动作会自动用相近的动作代替，最后退回 idle。只有 idle 是必须的。

## 动作包（mediaVersion 1）

```json
{"id": "my-pet", "mediaVersion": 1,
 "actions": {"idle": "idle.webp", "waving": "wave.mp4"},
 "staticActions": ["idle"],
 "background": {"mode": "alpha"}}
```

- `background.mode` 可以是 `alpha`（透明背景）或 `chroma`（纯色抠图，可加 `color`、`tolerance`）。
- 视频在 CPU 上解码，不占显存；每个动作最多 600 帧。

## 限制

- 包里最多 1000 个文件，合计不超过 1GB，单个文件不超过 256MB。
- 路径不能是绝对路径，也不能含 `..`。不安全的包会被直接拒绝。
