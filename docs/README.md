# 项目文档

本目录是项目的深度文档，面向想理解**设计与实现**的读者。日常安装使用请看仓库根目录的 [README.md](../README.md)（快速开始、环境要求、配置项与故障排查以根 README 为准）。

## 文档地图

| 文档 | 内容 |
|---|---|
| [architecture.md](architecture.md) | 技术框架与选型：为什么选 IBus、Rime + 雾凇拼音、默认语音模型 Qwen3-ASR；组件结构与语音流水线 |
| [asr-backends.md](asr-backends.md) | 语音后端支持：七种识别后端的形态、依赖、切换方式与选型建议 |
| [self-developed.md](self-developed.md) | 自主研发部分：自研代码清单与复用的第三方组件边界 |
| [validation.md](validation.md) | 测试平台与验证门禁：实测环境、CI、本地门禁与单元测试覆盖 |
| [limitations.md](limitations.md) | 目前的不足：适配性、硬件门槛、功能与工程层面的客观局限 |
| [paste-postmortem.md](paste-postmortem.md) | Ctrl+Alt+P 粘贴失效复盘（2026-08-28）：Wayland 剪贴板读取引发焦点抖动的根因、三检查点排查方法与修复 |

## 一句话了解本项目

一个用 Python 写的 Linux IBus 输入法引擎：**键盘输入**复用 Rime（librime + 雾凇拼音），**语音输入**（`Ctrl+Alt+V`，或跳过 LLM 后处理的原文模式 `Ctrl+Alt+B`）走自研的「录音 → 音频预处理 → ASR 后端 → 规则清理 → 提交」流水线，默认后端是本地 Qwen3-ASR 1.7B sidecar。

## 文档维护约定

- 本目录文档描述**为什么这样设计**与**整体结构**；具体环境变量、命令用法以根 README 为准，避免两处维护同一份配置清单。
- 文档声明的事实必须能在代码或提交历史中找到依据；行为变更时同步更新对应文档。
- `validation.md` 的文件名被 `tools/validate.py` 的 REQUIRED_FILES 检查引用，不可重命名。
