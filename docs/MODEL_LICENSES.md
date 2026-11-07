# 模型与组件许可清单

代码、角色资产、数据、模型分别许可，不能用一个许可证概括整个项目。下表的“状态”一栏：**已核** = 已对照上游许可文件；**待核** = 还没逐字核对上游，发行前必须补核。
衍生权重（微调、量化、换模板）沿用基座模型的许可条件，不会因为改了名字就变成别的许可。

| 用在哪里 | 本地名称 | 基座 / 上游 | 许可 | 状态 | 对发行的影响 |
|---|---|---|---|---|---|
| 意图判断（默认） | canaan-intent-v2:1.5b | Qwen2.5-1.5B-Instruct 微调 | Apache-2.0 | 待核（上游模型卡应为 Apache-2.0，本次未重新打开） | 可再发行，需保留许可与声明 |
| 陪聊（默认） | canaan-chat:3b | Qwen2.5-3B-Instruct（Q4_K_M GGUF） | **Qwen Research License**（非商业研究/评估用途） | 已核（2026-10-13 对照上游 [许可](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/raw/main/LICENSE)） | 商业用途须另获许可或更换允许商用的模型；研究/评估用途的再发行也须附协议、归属声明、修改通知。不能把这个模型的条件套到整个千问家族 |
| 陪聊回退 / 旧版意图 | canaan-desk:1.5b、canaan-intent:1.5b | Qwen2.5-1.5B-Instruct | Apache-2.0 | 待核 | 同上 1.5B |
| 消息总结、知识库回答 | qwen2.5:7b | Qwen2.5-7B-Instruct | Apache-2.0 | 待核（Ollama 打包版本另核） | — |
| 语音合成 | GPT-SoVITS v4 | RVC-Boss/GPT-SoVITS | 代码 MIT；底模另有条款 | 待核 | **音色模型基于游戏语音，只能个人学习，不随发行包分发**；公开产品须换有权利证明的原创音色 |
| 语音识别 | SenseVoice / Whisper | FunAudioLLM / OpenAI | SenseVoice 为 FunASR 模型许可，Whisper 为 MIT | 待核 | — |
| 唤醒词 | sherpa-onnx-kws-zipformer-wenetspeech-3.3M | k2-fsa/sherpa-onnx | 代码 Apache-2.0；模型与训练数据条款待核 | 待核 | — |
| 语音端点检测 | silero_vad.onnx | snakers4/silero-vad | MIT | 待核 | — |
| 角色形象、桌宠图集 | — | 游戏角色“团团”同人 | 无授权 | — | **公开发行须换原创或已授权的形象**，72 态素材来源逐项核实 |

仓库代码本身目前没有 LICENSE 文件；补许可证前不算开源。
