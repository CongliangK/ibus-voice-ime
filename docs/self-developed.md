# 自主研发部分

本项目自认是一个「胶水工程」（见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) 的完整第三方清单），但胶水之外有大量**原创设计与实现**。本文明确划出边界：哪些是自研代码，哪些是复用的成熟组件。

## 自研代码清单（`src/ibus_voice_ime/`，全部原创）

### 1. IBus 引擎本体（engine.py）

完整的输入法引擎生命周期：按键事件路由、preedit/候选框 UI（竖向 popup 与行内两种模式）、翻页与光标导航、候选点击、`Shift` 单击中英切换（含未提交输入码的落盘处理）、ASCII 直通（如 `/` 不转 `、`）、组件 XML 自描述生成（`--xml`）。

### 2. librime 的 ctypes 绑定（rime/rime_backend.py、rime/rime_deploy.py）

**没有使用 ibus-rime**，而是自己用 ctypes 定义 librime 的结构体与 API（RimeTraits/Session/Composition/Menu、修饰键掩码换算等），把 Rime 作为纯转换引擎嵌入自研引擎——这是「Rime 键盘 + 自研语音/粘贴/中英切换」能共存于一个按键路由层的根基。含部署编排：启动时检查/触发词库编译、内置运行时加载失败自动回退系统 librime。

### 3. 语音流水线编排（asr/voice.py、asr/audio_session.py）

toggle 式录音会话状态机；录音源三级链路（配置的 ALSA 直采设备 → 不存在时自动回退系统默认源 → arecord 失败回退 pw-record + ffmpeg）；录音时长与静音参数控制。

### 4. 分级音频预处理（asr/audio_preprocess.py）

sox 高通/工频陷波/归一化 + ffmpeg RNNoise(bd) 降噪的两档链路（notch / rnnoise），缺依赖自动降级、全程 fail-safe（预处理失败回退原始音频）。档位结论来自实测频谱分析（电容麦底噪 99% 为 50Hz 工频，ASR 对残余底噪鲁棒），不是拍脑袋参数。

### 5. ASR sidecar 框架（asr/sidecar_http.py、asr/qwen_asr_runtime.py、asr/qwen_asr_server.py、asr/mimo_asr_*）

一套「引擎进程外的本地推理服务」框架：

- 按需拉起、健康检查（`/health` 暴露加载错误）、预热接口（`/warm`）
- 空闲看门狗：模型迁回内存 + `torch.cuda.empty_cache()` 配合 `expandable_segments` 真正归还显存
- 双路并发首拉的进程锁、启动校验给出可读中文错误
- 仅绑定 127.0.0.1 的安全边界（有专门测试守护）

### 6. 云端 ASR 客户端（asr/mimo_cloud_asr.py、asr/volc_bigmodel_asr.py）

MiMo Token Plan（OpenAI-compatible）与火山豆包 bigmodel（异步提交 + 轮询 + base64 直传）两个客户端；火山侧含标点/ITN/DDC 开关与**热词直传**（自定义词典 → `corpus.context`，上限 5000）。

### 7. 文本后处理（text/text_postprocess.py、text/chinese_script.py）

确定性规则清理：空白归一、独立口头禅移除、语音标点口令（「换行」「逗号」…）转符号（默认只认独立口令防误伤）、自定义词典整词替换（中英文/标点边界匹配，不会误伤子串）、CJK/拉丁字符间距、OpenCC 繁→简兜底转换。

### 8. 语音热键与状态浮层（asr/voice_hotkey.py、asr/voice_overlay.py）

`Ctrl+Alt+V` 热键解析（GNOME 转发兜底场景）；可选 GTK 状态浮窗。

### 9. 用户记忆层（memory/chinese_memory.py、memory/english_memory.py、memory/voice_terms.py）

在 Rime 自身用户词库之外的确定性候选记忆：中文「输入码 → 已提交词」按频率/新近度排序为 `常用` 候选；英文 token 词库（Enter 提交即记忆，前缀召回）；三列式语音自定义词典（标准词/别名/常见误识别）的统一读取与分发。

### 10. 剪贴板粘贴链路（clipboard_paste.py、keyboard_type_clipboard.py、scripts/voice-toggle.sh、scripts/clipboard-paste.sh、scripts/keyboard-paste.sh）

- **主链路**：GNOME 全局热键 → 外部脚本读剪贴板 + 「正在粘贴」通知 → Unix socket IPC 通知引擎 → 引擎等待焦点回稳后走与语音结果**同款** `commit_text()` 提交路径 → 自动切输入源恢复被网页弄坏的 IBus 状态。
- **兜底链路**：`/dev/uinput` 虚拟键盘逐字键入，用于不接受 IBus commit 的场景（`--check` 自检权限）。
- 引擎侧 IPC 服务器（per-connection 异常隔离）也在此实现。

### 11. 工程化设施

- `run-engine.sh`：环境装配层（environment.d 加载、全量 `VOICE_IME_*` 默认值、CUDA pip 库路径、BWS 密钥运行时注入）
- `install.sh` / `uninstall.sh` / `scripts/check-environment.sh`：安装注册、卸载、新机器环境预检（OK/WARN/FAIL 分级）
- `log_trim.py`：日志保留策略（默认最近 1000 行，启动与写入双端裁剪）
- `tools/validate.py` + CI 工作流：三级验证门禁（见 [validation.md](validation.md)）

## 复用与捆绑的第三方组件（非自研）

| 组件 | 用途 | 获取方式 |
|---|---|---|
| librime + Rime 官方数据（brise） | 键盘输入引擎与基础方案 | 仓库内捆绑 `vendor/rime/` |
| 雾凇拼音 rime-ice（+ 可选 zhwiki / moegirl） | 默认拼音方案与词库 | 核心 schema 捆绑；大词库脚本下载 |
| OpenCC | 繁简转换 | 仓库内捆绑（lib + 数据） |
| Qwen3-ASR / MiMo-V2.5-ASR 模型 | 识别推理 | 安装脚本下载，不进 git |
| faster-whisper（CTranslate2）、vosk、torch/transformers | 识别推理实现 | pip 安装 |
| llama.cpp | 遗留本地 LLM sidecar（已被云端 OpenAI 兼容后处理取代，默认禁用） | 安装脚本下载 |
| 小米 / 火山云 ASR | 云端识别服务 | 仅 API 集成 |

一句话总结：**输入法框架接入、librime 绑定、语音全链路（录音→预处理→sidecar→后处理→提交）、记忆与粘贴增强、安装与验证工具链是自研；拼音算法与词库、识别模型本体、繁简数据是复用。**
