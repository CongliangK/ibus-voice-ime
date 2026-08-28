# Ctrl+Alt+P 粘贴功能失效复盘（2026-08-28）

本文记录一次跨越数月、当晚定案的故障排查：模拟输入粘贴（`Ctrl+Alt+P`）自某次系统升级后**静默失效**，引擎侧日志一切正常，文字却从不上屏。完整记录排查方法论、证据链、根因与修复，供后续类似故障（尤其是「引擎日志正常但用户看不到结果」类问题）复用。

## 摘要

| 项 | 结论 |
|---|---|
| 症状 | 按下 `Ctrl+Alt+P` 后桌面通知正常弹出，但文字从不上屏；引擎日志显示 `commit_text` 已调用且无异常 |
| 主根因 | Fedora 44 GNOME (Wayland) 下，**粘贴瞬间新建的原生 Wayland 剪贴板客户端（wl-paste）触发 compositor 级输入上下文焦点抖动**（实测每次 5-11 次 `FocusOut`、IBus 引擎实例被销毁重建），随后的 `commit_text` 投递全部静默丢失 |
| 帮凶 | 「恢复舞蹈」（每次粘贴 2 秒后 `ibus engine` 切走再切回）累积性毒化引擎实例与输入上下文的绑定（一晚实例号从 /1 滚到 /29） |
| 修复 | 剪贴板读取改为 **xclip 经 XWayland 优先**（常驻 X 服务转发、不新建 Wayland 客户端、实测零抖动）；删除恢复舞蹈；提交延迟 1000ms→300ms 与语音同节奏 |
| 为何语音不受影响 | 语音链路从不读取剪贴板；且其提交发生在触发数秒后、焦点早已稳定的时刻 |

## 排查方法论（可复用）

这次能定案，靠的是三件自建工具，均已入库：

1. **三检查点 + 内容指纹**（`clipboard_paste.content_fingerprint`，`scripts/diagnose-paste.sh`）：
   - CP1 剪贴板→暂存文件（助手侧）；CP2 暂存文件→引擎（IPC `paste-check` 只读不提交）；CP3a 引擎 `commit_text` 已调用；CP3b 总线上 CommitText 信号观测。
   - 指纹（sha1 前 8 位 + 行数 + 长度）跨进程一致，可逐跳验证内容完好性。本次排查中 CP1/CP2/CP3a 从始至终全绿，**第一时间把故障定位到 CP3a 之后**——那是唯一没有观测手段的一段。
2. **dbus-monitor 总线旁路**：IBus 的总线地址在 `~/.config/ibus/bus/*-unix-wayland-0`，`dbus-monitor --address <addr>` 可完整观测 FocusIn/Out、Enable/Disable、CommitText 等信号的精确时序——根因（焦点抖动）与机制（实例重建）全部来自这里。
3. **差分测试矩阵**：以「一直稳定的语音链路」为基线，逐个加回嫌疑变量（多行内容 / 1000ms 延迟 / 恢复舞蹈 / 通知 / 剪贴板接触），一次只动一个。

## 证据链（关键实验）

统一环境：干净引擎 + 焦点确认在文本输入框。✓=文字上屏，✗=CP3a 日志通过但文字未出现。

| 实验 | 触发 | 剪贴板接触 | 恢复舞蹈 | 总线 FocusOut | 投递 |
|---|---|---|---|---|---|
| LIVE_REPRO / B2 / C2 / J3 | socket 直连 | 无 | 无 | 0 | ✓ |
| D2（=真实按键配置） | 助手 | wl-copy + wl-paste | 有 | 风暴 | ✗ |
| E2 / G（无舞蹈无通知） | 助手 | wl-copy + wl-paste | 无 | ~10 | ✗ |
| N（无 wl-copy，纯 wl-paste 读） | 助手 | 仅 wl-paste | 无 | 5 | ✗ |
| xclip 单读 ×2 | 直连命令 | 仅 xclip（XWayland） | 无 | 0 | —（内容正确） |
| P（最终验收，默认配置） | 助手 | 仅 xclip | 无（已删） | **0** | ✓ |

关键推理节点：

- 直连成功 vs 助手失败的唯一稳定差异是**剪贴板接触**，而非内容、延迟、通知或舞蹈；
- GTK 后台进程读剪贴板返回 `None`（Wayland 无窗口进程没有有效 serial）——排除了「引擎内直接读」方案；
- `wl-paste --watch` 常驻镜像方案被排除：GNOME mutter 不支持 `zwlr_data_control` 协议；
- xclip 走 XWayland：选择转发由常驻 X 服务承担，读取**不新建 Wayland 客户端**，实测零抖动且内容正确——方案成立。

## 修复内容

| 改动 | 文件 |
|---|---|
| 读取顺序 xclip(XWayland)→wl-paste→GTK→xsel；`DISPLAY` 缺失时跳过 xclip | `src/ibus_voice_ime/clipboard_paste.py` |
| 删除恢复舞蹈及 `VOICE_IME_CLIPBOARD_RECOVER_*` 配置 | `scripts/clipboard-paste.sh`、`install.sh`、`run-engine.sh` |
| 提交延迟默认 1000ms→300ms | `src/ibus_voice_ime/engine.py` |
| 「📋 正在粘贴……」IBus 辅助提示条（与「正在录音」同通道；提交前先清提示，信号顺序对齐语音完成路径） | `src/ibus_voice_ime/engine.py` |
| IPC 协议新增 `paste-check`（CP2 只读隔离）、`paste-file` 可选 `delay_ms`（时序二分） | `src/ibus_voice_ime/engine.py` |
| 三检查点诊断脚本 + 指纹单元测试 | `scripts/diagnose-paste.sh`、`tests/test_clipboard_fingerprint.py` |
| 顺带清理：引擎内部热键路径、uinput 键盘模拟（380 行）、IPC 死命令（约 700 行死代码） | 见提交 |

依赖变化：新增系统包 `xclip`（`sudo dnf install xclip`，Fedora；其他发行版同名包）。

## 经验教训

1. **「发射后不管」的通道必须有旁路观测**：`commit_text` 是 D-Bus 信号，引擎永远不知道文字是否到达应用。没有 dbus-monitor 旁路，这个故障不可诊断。今后任何「下游不可确认」的链路都应预置观测点。
2. **时序补丁（sleep/切换重置）是负债**：恢复舞蹈当年是治「网页弄坏 IBus 状态」的止痛药，系统升级后反而成了毒药。删掉它后问题更少。
3. **对照组要选「一直稳定的最相似链路」**：语音与粘贴共享同一提交通道，是天然的对照组——把嫌疑变量从七个收敛到三个再到一个。
4. **升级会静默改变系统行为契约**：6 月的代码在 8 月的桌面上，连「读一次剪贴板」这种小事的副作用都变了。`scripts/check-environment.sh` 值得加入 xclip 存在性检查（遗留项）。

## 遗留项

- `check-environment.sh` 尚未加入 xclip 检查（xclip 缺失时回退 wl-paste，功能可用但可能复现本故障）；
- 若未来在无 XWayland 的纯 Wayland 会话运行，需重新评估读取方案（mutter 支持 `zwlr_data_control` 后可回到常驻镜像方案）。
