# 测试平台与验证门禁

本文是本地与 CI 验证规则的权威说明（`tools/validate.py` 的 REQUIRED_FILES 检查引用本文件，**不可重命名**）。同时如实划出验证边界：哪些有自动化保障，哪些只靠作者机器实测。

## 测试平台

### 完整验证环境（唯一实测平台）

| 项 | 值 |
|---|---|
| 发行版 / 桌面 | Fedora 44 Workstation + GNOME（Wayland） |
| 架构 | x86_64 |
| GPU | NVIDIA GeForce RTX 4080（本地 Qwen3-ASR 识别） |
| 音频栈 | PipeWire（arecord 直采 + pw-record 兜底） |
| 声卡 | MOTU M2 USB（直采配置示例 `plughw:M2,0` 的来源，其他机器不适用） |
| Python | 3.14（开发日用；语法下限 3.10） |

**如实声明**：键盘 + 语音的完整端到端体验只在上表这一台机器上验证过。其他发行版、桌面环境（KDE / X11 / sway）、ARM、无 NVIDIA GPU 场景均未实测——理论上 IBus + GNOME + PipeWire 组合可运行，安装脚本也做了降级容错，但作者没有跑过。详见 [limitations.md](limitations.md)。

### CI 平台

GitHub Actions（`ubuntu-latest`），push / PR / 手动触发均运行 **release 门禁**：

- Python 矩阵 **3.10 / 3.12 / 3.13**（3.10 是代码语法下限——PEP 604 联合类型；3.12/3.13 抽样验证通用环境）
- 额外安装 `sox`（否则 audio_preprocess 相关测试会在 runner 上整体 skip）
- 工作流只读：不安装输入法、不启动 IBus、不接触任何 ASR/LLM 服务与密钥

## 验证门禁（gates）

本地通过 mise（或直接 `python3 tools/validate.py <mode>`）运行三级门禁：

| 门禁 | 命令 | 范围 |
|---|---|---|
| 快速 | `mise run dev` | 全部 Python 语法（`ast.parse`，不写字节码缓存）+ 全部 shell 语法（`bash -n`）+ 单元测试 |
| 增量 | `mise run affected` | 仅变更文件（git 对比），随后**回退跑全量单测**（仓库尚无覆盖率映射，这是安全兜底） |
| 发布 | `mise run release` | 全量语法 + 单元测试 + 必需文件存在性检查（`mise.toml`、本文件、CI 工作流） |

**失败条件**：任一被检 Python 文件语法非法、任一 shell 脚本 `bash -n` 失败、任一单元测试失败、必需自动化文件缺失、验证命令非零退出。

确定性与依赖约束：验证只用标准库，本地与 CI 跑同一条 `tools/validate.py release` 命令，行为一致；除非未来在 `mise.toml` 固化工具，门禁保持 stdlib-only。

## 单元测试覆盖（tests/，170 个用例）

| 测试文件 | 用例数 | 守护的内容 |
|---|---|---|
| test_qwen_asr_model_manager.py | 27 | sidecar 模型管理器（加载 / 切换 / 错误面） |
| test_llm_cloud_config.py | 28 | LLM 云端 JSON 配置（字段/权限/优先级） |
| test_audio_preprocess.py | 12 | 降噪链（陷波 / RNNoise 档、降级路径、fail-safe） |
| test_volc_bigmodel_asr.py | 12 | 火山后端（请求构造、热词直传、轮询解析） |
| test_llm_prompt_markdown.py | 13 | LLM 提示词与 Markdown 输出约定 |
| test_arecord_device_fallback.py | 9 | 录音设备不存在时的默认源回退 |
| test_qwen_asr_warm.py | 8 | sidecar 预热 / 就绪判定 |
| test_log_trim.py | 7 | 日志裁剪（保留行数、残行保护、异常安全） |
| test_rime_schema_fallback.py | 7 | 词库缺失时的方案回退 |
| test_secret_persistence_policy.py | 7 | API Key 不落盘策略 |
| test_cjk_latin_space.py | 6 | 中西文间距后处理 |
| test_replacement_table.py | 6 | 词典整词替换边界（不误伤子串） |
| test_voice_hotkey.py | 15 | 主/原文语音热键解析、去重、匹配与组合停止提示 |
| test_engine_ipc_dispatch.py | 5 | 引擎 IPC 命令分发（toggle / toggle-raw，stub GLib） |
| test_voice_postprocess_skip_llm.py | 3 | 原文模式彻底跳过 LLM 后处理 |
| test_sidecar_security.py | 3 | sidecar 仅绑定回环地址 |
| test_validation_smoke.py | 2 | 验证工具自身冒烟 |

## 自动化测试测不到什么（诚实边界）

- **引擎与 IBus/GNOME 的真实交互**：按键转发、候选框行为、输入源切换——CI 上没有图形会话与 IBus daemon，只能靠作者机器实测。
- **语音端到端质量**：录音 → 识别 → 提交的准确率与延迟依赖真实麦克风与 GPU，无自动化覆盖。
- **跨发行版 / 桌面兼容性**：无第二台验证环境（见上文声明）。
- **词库候选质量**：雾凇拼音体验属上游方案范畴。

报 bug 时请附 `./scripts/check-environment.sh` 输出与三份日志（`engine.log` / `error.log` / `qwen-asr-server.log`，默认已自动裁剪到最近 1000 行）。
