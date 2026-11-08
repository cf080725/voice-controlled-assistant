# 团团 · 本地桌面语音助手

在 Windows 上用语音操作电脑的助手：先听懂你要做什么，有风险的操作先问你，执行完告诉你结果。唤醒、语音识别和记忆都在本机；“听懂意思、陪你聊天”的大模型可以选本机运行，也可以接云端 API，没有独立显卡也能用。

> 这是公开版。它不包含读取、解密或模拟操作任何第三方聊天客户端的功能。

## 能做什么

- **语音操作电脑**：打开或关闭软件、调音量和亮度、截图、搜文件、切歌、设提醒，以及几步串在一起的任务。
- **先问再做**：删除、关机、运行命令、操作电脑这类不能撤回的动作，每次都要你确认。说不清楚的时候，它会反问你，而不是去猜。
- **系统通知**：等你确认、任务失败、提醒到点、智能体任务完成时，会弹 Windows 系统通知，默认只显示状态，不显示内容。游戏模式下不弹，也可以在托盘菜单里关掉。
- **记忆**：会记住你明确告诉它的事，以及做过的任务；重启后能接着刚才的话聊；每天第一次见面时，会提一下“今天要记得”的事，最多 3 条。看板里能看、能改、能删；无痕模式下不记新东西。
- **智能体进度**：接入本机的 Muse / Dots 等智能体服务后，会显示“已接下 / 第几步 / 等你处理 / 办好了 / 没办成”。
- **消息接入**：支持飞书（官方开放平台，用你自己的应用和令牌），以及 Claw / MCP 客户端推送。重要消息会按优先级排序；总结在本机完成。
- **桌宠**：默认形象“团团”是用代码画的原创形象。你可以导入自己的形象包，格式见 [docs/PET_PACK_FORMAT.md](docs/PET_PACK_FORMAT.md)。默认开启平静模式：只在需要你的时候出现。
- **本机接口**：给外部客户端单独发令牌，按能力和资料范围授权；外部客户端永远拿不到“确认执行”。详见 [docs/INTEGRATION_API.md](docs/INTEGRATION_API.md)、[docs/PERMISSIONS.md](docs/PERMISSIONS.md)。

## 给她起个名字

默认叫“团团”。第一次运行 `bootstrap` 时可以换成你喜欢的名字（2–4 个汉字），之后也能改：

```
.venv\Scripts\python.exe canaan.py bootstrap --name 小安
```

唤醒词（“小安”“你好小安”“小安小安”）、界面上显示的名字、陪聊时的自称会一起换，改完重启生效。名字只存在你电脑的 `data/` 目录里。

## 先选大模型怎么跑

唤醒词、端点检测、语音识别都是 CPU 上的小模型，三种方式都一样；区别只在“听懂意思、陪你聊天”的大模型：

| 方式 | 适合 | 要什么 | 花费 | 隐私 |
| :--- | :--- | :--- | :--- | :--- |
| **本机模型** | 有 6 GB 以上显存的 NVIDIA 显卡 | [Ollama](https://ollama.com)；8 GB 显存跑 3B（约占 2.5 GB，可选原版 Qwen2.5-3B 或作者微调版），6 GB 跑 1.5B | 免费 | 全部在本机 |
| **云端 API** | 没有独显、显存小、轻薄本 | 一个服务商的 API 密钥：DeepSeek、通义千问、Kimi、智谱、硅基流动、OpenAI、Claude，或任何 OpenAI 兼容接口 | 按用量付费 | 你说的话会发给所选服务商；长期记忆和本机资料默认**不**发 |
| **只用规则** | 只想要语音开软件、调音量、截图这类固定指令 | 什么都不用 | 免费 | 全部在本机 |

安装时（`一键安装.bat` 或 AI 代装）会检测显存并推荐一种，你确认或改选即可。之后随时可以换：

```
.venv\Scripts\python.exe canaan.py bootstrap --llm local    # 本机模型
.venv\Scripts\python.exe canaan.py bootstrap --llm api      # 云端 API（会问服务商、模型名和密钥）
.venv\Scripts\python.exe canaan.py bootstrap --llm rules    # 只用规则
.venv\Scripts\python.exe tools\llm_provider.py --list       # 看现在用的哪个服务、有没有密钥
```

**作者微调版**：在 Qwen2.5-3B-Instruct 上针对本项目的指令识别做了 LoRA 微调，陪聊风格和原版接近；训练数据里的真实人名和游戏角色都已去掉。

在作者写的 16 个题集（共 1788 句）上，只看模型本身的输出、不加规则和守卫时：

- 原版：完整判断正确 45.6%，不该执行却执行 186 次；
- 微调版：完整判断正确 63.2%，不该执行却执行 135 次。

详见 [Release model-v1](https://github.com/cf080725/voice-controlled-assistant/releases/tag/model-v1)。8 GB 显存选本机模型时会问你要哪一版，只下载选中的那个（都约 1.9 GB）；之后可以用 `.venv\Scripts\python.exe canaan.py bootstrap --local-model tuned` 或 `--local-model base` 切换。它沿用 Qwen2.5-3B 的许可（非商业研究用途）。

改完重启生效。云端密钥加密保存在本机 `data/secrets.db`，也可以用环境变量 `CANAAN_<服务名>_API_KEY`（例如 `CANAAN_DEEPSEEK_API_KEY`）。

> 云端 API 目前只接管“听懂指令”和“陪你聊天”。消息总结、本地知识库问答、界面自动操作、记忆自动整理仍然只用本机模型，没装本机模型时这几项不可用；你明确说的“记住……”照常保存。

## 让 AI 帮你装（推荐）

把下面这句话发给你电脑上的 AI 编程助手（Claude Code、Codex、Cursor 等）：

> 请按 https://github.com/cf080725/voice-controlled-assistant 的 README 里“给 AI 的安装说明”，在这台 Windows 电脑上帮我安装。

它会先问你几个问题（给助手起什么名字、大模型用哪种方式），然后自己装好。

### 给 AI 的安装说明

适用于 Windows 10/11 64 位。整个安装可以重复运行：已经装好的会跳过，下载会续传。

1. **先问用户，只问这几项：**
   - **助手的名字**：2–4 个汉字，默认“团团”。
   - **大模型方式**：先运行 `nvidia-smi` 看显存，再让用户选：
     - 显存 ≥ 6 GB 推荐 `local`（本机模型，免费、离线）；
     - 没有独显或显存更小，推荐 `api`（云端，按量付费）；
     - 也可以选 `rules`（不用大模型，只听固定指令）。
   - **选了 `local` 并且显存 ≥ 8 GB**：问用 `tuned`（作者微调版，指令识别更准，推荐）还是 `base`（原版 Qwen2.5-3B）。
   - **选了 `api`**：问用哪个服务，可选 `deepseek`、`qwen`、`kimi`、`zhipu`、`siliconflow`、`openai`、`claude`。

2. **API 密钥不要经过你。** 不要让用户把密钥发给你，也不要把密钥写进命令行或文件。安装完成后，让用户自己在终端里运行下面这条命令，按提示输入密钥（输入时不显示）：

   ```
   .venv\Scripts\python.exe tools\llm_provider.py --set-key <服务名>
   ```

3. **获取代码**，放到路径里没有空格和中文的目录，例如：

   ```
   git clone https://github.com/cf080725/voice-controlled-assistant.git D:\voice-assistant
   cd D:\voice-assistant
   ```

   没有 git 的话，下载 `https://github.com/cf080725/voice-controlled-assistant/archive/refs/heads/main.zip` 并解压。

4. **安装**：在仓库根目录用 PowerShell 运行，按用户的回答填参数。不要运行 `一键安装.bat`，它最后会停下来等人按键。

   ```
   powershell -NoProfile -ExecutionPolicy Bypass -File installer\install.ps1 -Name 小安 -Llm local -LocalModel tuned
   powershell -NoProfile -ExecutionPolicy Bypass -File installer\install.ps1 -Name 小安 -Llm api -Provider deepseek
   powershell -NoProfile -ExecutionPolicy Bypass -File installer\install.ps1 -Name 小安 -Llm rules
   ```

   脚本会自动完成这些事：
   - 没有 Python 3.12 就安装（只装给当前用户）；
   - 建 `.venv` 虚拟环境；
   - 依次用清华、阿里云、官方源装依赖；
   - 下载语音模型；
   - 选了 `local` 时安装 Ollama 并装好本机大模型；
   - 在桌面放快捷方式（不想要就加 `-NoShortcut`）。

   第一次安装要 10 到 60 分钟：依赖约 1–2 GB，本机大模型约 2 GB。如果你的命令有时间上限，就放到后台跑；中途被打断也没关系，重新运行同一条命令会接着装。

5. **检查**：退出码为 0 说明安装完成。

   ```
   .venv\Scripts\python.exe canaan.py bootstrap --check
   ```

   不为 0 时，输出里会列出缺哪一项，重新运行第 4 步即可。想更彻底地检查，可以运行 `.venv\Scripts\python.exe -m pytest tests -q`，大约 2 分钟。

6. **启动**：双击 `启动.bat` 或桌面快捷方式，或者运行 `.venv\Scripts\pythonw.exe canaan.py`。右下角托盘出现图标就是在运行了。喊“你好<名字>”就能叫醒。

7. **常见问题**：
   - **pip 报 SSL 或超时**：脚本会自动换下一个源。也可以用 `-PipIndex <镜像地址>` 指定镜像。
   - **提示没有 tkinter**：用 python.org 的安装包重新装 Python 3.12，勾选 tcl/tk。
   - **Ollama 没装上**：从 https://ollama.com 手动安装后，重新运行第 4 步。
   - **以后改名字或换大模型方式**：

     ```
     .venv\Scripts\python.exe canaan.py bootstrap --name 新名字
     .venv\Scripts\python.exe canaan.py bootstrap --llm api --provider qwen
     ```

     改完重启生效。

## 安装（自己装）

1. 下载本仓库：点右上角绿色的 **Code → Download ZIP**，解压到一个**路径里没有空格和中文的文件夹**，比如 `D:\voice-assistant`。
2. 双击 **`一键安装.bat`**，按提示操作：
   - 没有 Python 3.12 会自动安装（只装给当前用户）；
   - 依赖装在文件夹里的 `.venv`，不影响电脑上别的 Python，国内默认走清华镜像；
   - 问你给助手起什么名字、大模型用哪种方式，然后下载需要的模型；
   - 选了本机模型又没装 Ollama 时，会自动装上；
   - 最后在桌面放一个以助手名字命名的快捷方式。
3. 双击桌面快捷方式（或文件夹里的 `启动.bat`）启动，右下角托盘出现图标就是在运行了。

第一次安装要下载 1–2 GB 依赖，选本机模型再加约 2 GB，视网速需要十几分钟到一小时；中途断了重新双击 `一键安装.bat` 即可，装好的会跳过、下载会续传。用 Claude 的话再运行 `.venv\Scripts\python.exe -m pip install anthropic`。

没有装 GPT-SoVITS 时，语音合成自动用 Windows 自带的中文语音。

手动安装：Python 3.12 → `pip install -r requirements-lock.txt -r requirements-integrations.txt` → `python canaan.py bootstrap` → `python canaan.py`。

## 隐私与数据

- 数据都在本机 `data/`、`logs/` 目录里，不随代码分发。
- 只有你选了“云端 API”才会联网调用大模型；各服务商的条款和计费请自己核对。
- 只处理你有权处理的数据。总结群聊、转写别人的语音之前，请先取得相关人的同意。

## 许可

- 代码使用 Apache-2.0 许可，见 [LICENSE](LICENSE)。
- 第三方组件和模型各有各的许可，见 [NOTICE](NOTICE) 和 [docs/MODEL_LICENSES.md](docs/MODEL_LICENSES.md)。
- 本机默认模型 Qwen2.5-3B-Instruct 的许可只允许非商业研究用途；要商用，请换成许可允许的模型或改用云端 API。
