# 统一状态事件与桌宠形象包（2026-10-12）

## 状态事件（core/canaan_events.py）

执行器和中枢只往外发 7 种状态：在听、在想、第 n/总 步、等确认、成功、失败、空闲。桌宠、看板、托盘、共创平台桥接都订阅它。

- 事件里只有动作名、步骤号和结果类别（ok / failed / blocked / confirm）。原话、路径、提示词、执行结果正文都不放进去；不合规的字段直接丢掉。
- 发出就不管：事件进有界队列，由后台线程分发，队列满了就丢；订阅方出错只影响它自己。
- 谁发：执行器统一入口 `CanaanOSExecutor.execute` 发步骤、等确认、成功、失败（闲聊不发）；中枢发在听、在想、空闲。
- 谁收：
  - 桌宠：头顶步骤卡。不再显示任务编号，只显示“第 n/总 步 · 动作名”。
  - 看板：`GET /api/status` 返回当前状态，只需要“看状态”权限，页面上显示“当前：……”。
  - 托盘：中枢把快照写到 `data/canaan_status.json`，托盘每 3 秒读一次，写进图标提示。
- 过期规则：进行中的状态 90 秒没有新事件就显示“待确认”，10 分钟没有就清回空闲。

## 桌宠形象包（shells/canaan_pet_pack.py）

右键 → 形象 → 导入形象包，选共创平台格式的 zip，或文件夹里的 `pet.json`。“恢复默认 72 态”可以换回来，原来的 72 态继续作为高级形象保留。

- 支持的格式：
  - V1 图集：8×9 格，每格 192×208。
  - V2 图集：`spriteVersionNumber: 2`，8×11 格。
  - `mediaVersion: 1` 动作包：`actions`、`stateBindings`、`background`（alpha / chroma 绿幕）、`staticActions`。媒体支持动画 WebP、MP4、WebM，在 CPU 上解码。
- 9 个核心动作缺了的退回 idle；可选动作 dragged 退回 jumping，sleeping 退回 idle。团团的 72 态姿态按 `CANAAN_TO_PACK` 映射到包里的动作。
- 光标注视：只对 V2、只在待机时生效。光标在动时跟着看，停下 1.2 秒后回正；宠物翻转时方向也跟着镜像。右键菜单可以关。
- 安全：
  - 拒绝绝对路径、`..`、链接；
  - 最多 1000 个文件，总共不超过 1 GiB，单个文件不超过 256 MiB；
  - 动画最多 600 帧；
  - 单帧图片必须在 `staticActions` 里声明。
- 对方文档没写清、这里是推断的（都写在 `canaan_pet_pack.py` 开头，可以改）：
  - 图集第 0–8 行依次是 idle、running-right、running-left、waving、jumping、failed、waiting、running、review；
  - 每行从左往右取非空格子作为帧，idle 行最多 6 帧；
  - 16 个注视方向从正上方开始顺时针排；
  - 图集每行一圈大约 1 秒。
- 验收说明：目前还没有真实的共创平台形象包，以上全部用自己生成的测试图集验收（`tests/test_pet_pack.py`、`tools/bench_pet_pack.py`）。拿到真实的包后，要再核对行顺序和注视方向。
- 性能（`tools/bench_pet_pack.py`，窗口隐藏，各 6 秒）：
  - 原 72 态、形象包待机、形象包 + 注视、形象包动作循环，四种情况都是约 57 帧/秒，帧间隔 P50 约 17.5 毫秒，最大约 19.5 毫秒，没有一帧超过 50 毫秒；
  - 本进程独占显存 0.0 MB；
  - 窗口隐藏时屏幕合成的开销没计入。

## 共创平台桥接（plugins/canaan_openpets_bridge.py，默认关闭）

在 `data/openpets_bridge.json` 写 `{"enabled": true}` 后，下次启动团团生效。

- 只调用 `openpets react thinking|working|waiting|success|error`，同一种反应 10 秒内只发一次。
- 再加 `"say": true`，会额外发一句只含动作名和步骤号的短状态（最多 140 字，20 秒一次）。
- 命令不经过 shell，3 秒超时，失败不影响团团。
