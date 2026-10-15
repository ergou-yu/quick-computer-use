# QCU 测试报告（Test Report）

## 1.11.0 GNOME Wayland 验证 — 2026-10-15

本轮在实际 GNOME Shell/Mutter Wayland 合成器中运行 GTK3、AT-SPI2 和
QCU 常驻 daemon；这属于 **headless 虚拟桌面集成测试，不是物理 Linux 真机测试**。
测试使用独立 DBus、QCU_HOME、临时扩展目录和一次性窗口，`DISPLAY` 未设置，
GTK 明确使用 Wayland，合成器禁用 XWayland。

| 验证项 | 结果 |
|---|---|
| Ubuntu 24.04 / GNOME 46.2 / Python 3.12.3 | **29 条记录命令，完整流程通过** |
| Ubuntu 26.04 / GNOME 50.1 / Python 3.14.4 | **29 条记录命令，完整流程通过** |
| Ubuntu 24.04 GNOME 容器对照 | **29 条记录命令，完整流程通过** |
| X11 / GTK / Xvfb / Openbox 回归 | **25 条记录命令，完整流程通过** |
| Linux 完整回归 | **783 passed、10 skipped，27.21 秒** |
| Windows 完整回归 | **792 passed、1 skipped，62.35 秒** |
| GNOME JavaScript 故障边界 | **12 项通过，mock host，单独计数** |

29 条命令包含语义中文/emoji 填充、勾选、Selection、batch、旧 ref 拒绝、
目标窗口 PNG、原生点击/F6、Unicode 追加、悬停/右键/双击/滚轮/拖拽、
screenshot fallback、Shell 表面遮挡和非前台拒绝、窗口移动后截图原点与
原生 ref 点击、扩展停用拒绝、重新启用后的旧实例拒绝，以及扩展失效时
继续使用语义填充。命令数包含预期拒绝和清理，不含只读就绪轮询。
Ctrl+a/End 未单独指定后置条件，记录为发送且结果未知，不计作独立结果验证。

可核验记录：[GNOME 46/50 三环境完整流程](https://github.com/ergou-yu/quick-computer-use/actions/runs/38678610932)、
[GNOME 46 JSON、PNG、日志](https://github.com/ergou-yu/quick-computer-use/actions/runs/38678610932/artifacts/11947242250)、
[GNOME 50 JSON、PNG、日志](https://github.com/ergou-yu/quick-computer-use/actions/runs/38678610932/artifacts/11947146397)、
[Linux/X11 回归与打包](https://github.com/ergou-yu/quick-computer-use/actions/runs/38678610973)。

GNOME 46 本轮单次后端耗时：语义 fill 28.06 ms、toggle 8.57 ms、select
6.24 ms；batch 两次填充、一次点击与观察共 66.78 ms；原生点击含读回
35.89 ms、F6 32.88 ms、拖拽 24.83 ms，截图 41.20 / 24.62 ms。
GNOME 50：语义 fill 22.74 ms，batch 50.09 ms，原生点击含读回 58.74 ms、
拖拽 19.86 ms，截图 39.11 / 20.07 ms。
这些是该虚拟机上的单次样本，不含模型推理和 CLI 进程成本，不能据此
宣称与常规 computer use 相比有固定倍数的优势。

QCU 使用报告：实际层为 `desktop_linux`、`screenshot_fallback`，并运行
DOM/CDP、缓存与 batch 回归。新增原生通道为常驻 GNOME D-Bus 和 Mutter
VirtualInputDevice；普通语义操作仍不截图、不 OCR、不加载原生适配器、
不启动逐事件子进程，也没有新增固定动作等待。PNG 仅用于显式截图请求；
按键/点击是否成功仍以 AT-SPI 后置条件读回为准。

本轮修复 Clutter 默认键盘焦点返回 stage 的判断、GTK 客户端阴影导致的
buffer/frame 尺寸差异、非交互 Shell 层被误判为鼠标遮挡的问题。
测试环境提前建立虚拟键鼠 seat，以模拟桌面已有输入设备；测试控制器仅
移动/聚焦自有窗口并创建遮挡表面，不替代 QCU 的原生输入或结果读回。
该控制器不安装到用户桌面、不包含在 wheel 中。
GNOME 50 兼容了 Wayland-only 版本移除旧探测函数的变化，并使用新的
pointer sprite 坐标。原生通道首次绑定等待虚拟设备注册完成，后续重复使用。

已知限制：GNOME 50 的 headless 指针从屏幕边缘出发时，开启热角会遇到
Mutter 指针屏障，运动未到达目标，QCU 返回 unknown 并停止在点击之前。
完整成功流程在临时设置中关闭了热角和动画；生产代码不修改这两项设置，
不绕过屏障、不自动重发。本项属于实际测到的桌面输入约束，不能宣称
GNOME 50 所有默认屏障/起点组合均已通过。

限制：原生后备按所支持的 GNOME 主版本启用，KDE/wlroots 原生适配仍未
实现；混合 DPI、多显示器、特殊键盘布局和受保护/自绘控件仍需逐目标验证。
首次使用需显式安装并启用随包 GNOME 扩展；停用扩展立即失去原生通道，
AT-SPI 语义操作继续可用。未修改用户 portal、系统权限或剪贴板。
安装与复跑方法见 [Linux](references/linux.md)。

---

## 1.10.0 Linux 验证 — 2026-10-15

环境：Ubuntu 24.04、Python 3.12.3、系统 GI/AT-SPI2/GTK3、Xvfb :99、
Openbox、python-xlib 0.33。所有操作使用独立 QCU_HOME 和一次性 GTK 窗口。
这是实际 Linux 虚拟桌面测试，**不是物理机、完整 GNOME/KDE 或 Wayland 真机验证**。

- **Linux 完整回归：771 passed、10 skipped，27.48 秒，无失败。**
  跳过项按平台/显式真机开关区分；GTK 集成另外单独运行。
- **GTK/AT-SPI/X11 完整流程：25 条记录命令，主流程通过。**
  包含语义填充、中文/emoji、勾选、Selection、batch、旧 ref 拒绝、
  X11 PNG、原生点击/F6、Unicode 追加、悬停/右键/双击/滚轮/拖动、
  screenshot fallback、遮挡/非前台拒绝、移动后的截图原点及后续语义填充。
  命令数包括拒绝和清理，不含只读就绪轮询；未单独指定后置条件的
  Ctrl+a/End 只记录发送，不计作独立业务结果验证通过。
- wheel/sdist 在 Linux 构建通过；源码 Bash 入口保留 Unix 可执行权限。
- **Windows 最终全量回归：780 passed、1 skipped，60.35 秒，无失败。**
  唯一跳过为 Linux 真机用例；Windows/macOS 的原后端未替换。

可核验运行：[Ubuntu CI 38669922456](https://github.com/ergou-yu/quick-computer-use/actions/runs/38669922456)，
[原始 JSON、截图、日志与构建包](https://github.com/ergou-yu/quick-computer-use/actions/runs/38669922456/artifacts/11943550809)。

本轮单次后端耗时（不是多轮中位数，CLI 总耗时另计）：观察 22.58 ms、
语义 fill 10.62 ms、toggle 14.86 ms、select 6.97 ms；
batch 两次填充、一次提交及观察共 55.81 ms（完整 CLI 159.87 ms）。
原生点击含结果读回 33.10 ms，拖动含结果读回 107.15 ms；
两次截图 55.86/25.20 ms。这里只是该虚拟机的小样本，不包含模型推理，
不据此宣称相对其他 computer use 的固定倍数。

QCU 使用报告：实际层为 `desktop_linux`、`screenshot_fallback`，并运行网页
DOM/CDP 回归。结构化 ref、常驻 worker、缓存/compact 与有界 batch 保留；
普通语义操作不加载 X11、不截图、不插入固定延迟。发现并修复 GI 的
Accessible/Text 同名方法冲突、ATK 插入长度必须按 UTF-8 字节计算的问题。
GTK ListBox 在此环境不暴露 Selection，因此 fixture 使用实际暴露 Selection
的 IconView；生产后端对缺少该接口的控件明确拒绝。

Ubuntu runner 的 Chromium 启动诊断确认 `No usable sandbox`，随后只为
runner 的准确测试浏览器路径加载 AppArmor userns profile；沙箱探测及网页
回归均通过。QCU 不自动关闭 Chromium 沙箱，也不修改用户主机策略。

限制：Wayland 原生截图/输入尚无 portal/compositor 适配器；可暴露的 AT-SPI
语义操作仍可使用。混合 DPI/多显示器/键盘布局、受保护或自绘控件均需
逐目标验证；本次没有声称物理 Linux 桌面或所有发行版互操作完成。
安装、复跑与接口边界见 [Linux](references/linux.md)。

---

## 1.9.2 Windows 验证 — 2026-10-15

环境：Windows 10 build 19045、Python 3.12、uiautomation 2.0.29、
comtypes 1.4.17、Playwright 1.64.0、Chromium 156.0.8078.4。
真机测试使用独立 `QCU_HOME`、一次性 WinForms 窗口及 Chromium profile。

| 验证项 | 结果 |
|---|---|
| 完整回归套件 | **745 passed，57.29 秒，无失败或跳过** |
| Windows 原生真机流程 | **26 条 CLI 命令，主流程通过**；包含按预期拒绝的请求及独立 SendInput 诊断 |
| 原有 UIA 真机回归 | **6 项直接检查、16 条 CLI 命令通过** |
| 打包与入口 | wheel/sdist、独立 wheel 安装、PowerShell/CMD JSON 管道及 skill 格式校验通过 |

本次真机覆盖绑定 HWND 的 PrintWindow/GDI PNG、窗口移动后的物理像素原点、
点击/双击/右键/悬停/滚轮/拖动、命名按键、标准 Edit Ctrl+A 选择读回、
中文与 emoji 输入、截图 fallback、遮挡/前台/旧 ref 拒绝，以及原生操作后的语义填充。
回归套件包含真实 Windows 进程/端口与 Chromium CDP 测试；本次 macOS 用例为
contract/mock，不能视为 macOS 真机复测。

**默认 HWND 消息通道已验证；物理 SendInput 在本机未确认投递。**
独立 F7 探测返回接受两个事件，但窗口没有出现预期结果，记录为 `outcome:unknown`。
原因未确定，没有换通道重发。26 条命令不是 26 次业务结果验证通过。
安全桌面不支持；混合 DPI/多显示器组合未做真机验证。Raw Input/DirectInput、
游戏、自绘或受保护窗口仍需逐目标验证。

普通 UIA ref click/fill 保留直接 Pattern 调用；DOM/CDP、常驻 daemon、compact、
缓存 find/inspect、有界 batch 与条件就绪保留。截图及原生输入按需加载，
普通操作没有新增强制截图或固定等待。

同一六字段网页表单，每个场景/模式三轮，共 12 个任务成功。
统计 observe、fill、submit 与 verify 的 CLI 总耗时，排除导航、冷启动及模型推理。

| 场景 | 逐条调用中位数 | batch 中位数 | 加速 | 调用次数 |
|---|---:|---:|---:|---:|
| 静态网页 | 3858 ms | 1231 ms | 3.14× | 9 → 3 |
| 持续联网页面 | 3574 ms | 3091 ms | 1.16× | 9 → 3 |

持续联网页面两轮 batch 的点击/验证阶段约 1.9 秒，存在等待抖动，原因未确定。
这组小样本是 QCU 逐条调用与 batch 的对比，不是与常规 computer use 的同场测试，
不支持对所有应用宣称固定速度优势。

从源码根目录复跑（先按 [INSTALL.md](INSTALL.md) 安装 Windows 开发依赖）：

```powershell
& .\.venv\Scripts\python.exe -X utf8 -m pytest -q
& .\.venv\Scripts\python.exe scripts\verify_windows_native.py --output native.json --check-send-input
& .\.venv\Scripts\python.exe scripts\verify_windows_uia.py --output uia.json
& .\.venv\Scripts\python.exe scripts\benchmark_ui.py --mode separate --rounds 3 --output separate.json
& .\.venv\Scripts\python.exe scripts\benchmark_ui.py --mode batch --rounds 3 --output batch.json
```

---

## 1.8 历史报告 — 2026-09-16

> 测试日期：2026-09-16 · 版本：**qcu 1.8**（发布版）
> 环境：macOS darwin 25.2.0（Apple Silicon，3456×2234 Retina）· Python 3.12.11（uv venv）
> · Playwright Chromium 149 · macOS TCC 三项权限齐备 · daemon 常驻
> 方法：全部为本机真机实测；所有基准使用独立 `QCU_HOME` 与一次性 Chromium profile，
> 未触碰日常浏览器 profile 与真实业务数据。**Windows UIA 真机未验证；云端 VLM 延迟未测。**

## 0. 结论摘要

| 验证项 | 结果 |
|---|---|
| 回归测试套件 | **663 / 663 通过**（24.8 s，含 mock 契约、Chromium 集成、macOS AX 用例） |
| web 路径整任务（batch 模式） | 5/5 静态 + 5/5 忙页面（100 ms 轮询干扰），结果文本与字段回读断言全过 |
| 桌面 AX 整任务（真实 Calculator） | 3/3 轮 `7+3=10`，最终动作 `dispatch_state: sent, outcome: verified` |
| 单步延迟（web observe/fill/click） | **74.5 / 79.7 / 93.1 ms**（含 CLI 进程开销；daemon 内部 10–26 ms） |
| 对照：视觉范式本地感知底线 | 截图+编码+OCR 合计 **255–463 ms**（不含上传与云端 VLM） |
| 感知 payload | 结构化 JSON **3.25 KB** vs 全屏 PNG **3.34 MB**（≈1/1000） |

性能结论：本地可实测部分 QCU 单步快 **3–6×**；按行业典型 VLM 延迟（1–3 s+/步，未实测）
外推整步差距为十几到几十倍量级。**不宣称普适速度优势**：fixture 偏简单、不含模型推理、
canvas/游戏场景不在 a11y 范式内。

## 1. 回归测试套件

```bash
QCU_HOME="$(mktemp -d)" python -m pytest -q
# → 663 passed in 24.81s
```

覆盖 mock 契约（refs/outcome/verification）、Chromium 临时 profile 集成回归、
macOS AX 真实用例。Windows UIA 仅有 mock 契约与验证脚本，**不代表真机互操作**。

## 2. 性能测评（2026-09-16 实测）

### 2.1 QCU web 路径（web_a11y/CDP 层）

fixture：本地 26 元素表单页（6 输入框 + 提交按钮），`scripts/benchmark_ui.py`，
n=10 轮（separate/batch 各 5），10/10 断言通过：

| 操作 | 墙钟 p50 | 内部 p50 | payload |
|---|---|---|---|
| CLI 进程开销（`qcu --version`） | 49.6 ms | — | — |
| observe --compact | 74.5 ms | 11.0 ms | 3.25 KB |
| observe（全量） | 70.4 ms | 11.0 ms | 10.5 KB |
| fill（含回读） | 79.7 ms | 9.7 ms | 4.4 KB |
| click + text 验证 | 93.1 ms | 26.1 ms | — |
| navigate（热浏览器） | 93.3 ms | — | 冷启动 outlier 2.8 s |
| batch（6 fill + 1 click） | 167.0 ms | 107.1 ms | — |

整任务（6 字段 + 提交 + 验证）：separate 模式 9 次调用 **673 ms**（忙页面 1109 ms）；
batch 模式 3 次调用 **310 ms**（忙页面 315 ms）。

### 2.2 QCU 桌面路径（desktop_ax 层，真实 Calculator）

`scripts/benchmark_desktop_ax.py`，3/3 轮 verified：

| 操作 | 墙钟 p50 | 内部（daemon） |
|---|---|---|
| observe --compact（58 交互元素） | 175.8 ms（首轮含冷启动；热态 ~132 ms） | 热态 ~85 ms |
| click（AXPress，无条件） | 172 ms | 87–123 ms |
| click + text 验证 | 195 ms | — |
| 一整轮（observe + 4 clicks 含验证） | — | 热态 ~854 ms |

### 2.3 对照组 A：视觉回路本地底线（"截图流派"感知通道）

`scripts/benchmark_vision_baseline.py`（同 fixture 页面可见，headful Chromium）：

| 环节 | p50 | 产物 |
|---|---|---|
| Quartz 全屏截图（3456×2234） | 14.8 ms | CGImage |
| PNG 编码 | 116.4 ms | **3.34 MB** |
| 窗口区域截图+编码（变体） | 46.2 ms | 142 KB |
| Apple Vision OCR accurate | 332 ms（46 段文本） | — |
| Apple Vision OCR fast | 124 ms | — |
| **感知合计** | **463 ms（accurate）/ 255 ms（fast）** | — |

上传网络与云端 VLM 推理**未测**（无 API，不计入实测数）。纯 VLM 范式不做本地 OCR，
但需上传整图并等待云端推理；本地 OCR 在此仅作"感知底线"的保守替代。

### 2.4 对照组 B：裸 Playwright 下限（自动化物理下限）

`scripts/benchmark_playwright_floor.py`（同任务、进程内、无 CLI/daemon）：
fill 2.4 ms/个、click+verify 18.2 ms、整任务 **37.9 ms**。

### 2.5 开销分解与加速比

| 对比 | QCU | 对方 | 倍数 |
|---|---|---|---|
| 单步感知（observe vs 截+编+OCR） | 74.5 ms | 255–463 ms | **3.4–6.2×** |
| 单步感知+执行（vs 含 VLM 典型 1.5–4 s*） | 93 ms | 1500–4000 ms | *外推 16–43× |
| 感知 payload | 3.25 KB | 3.34 MB | **≈1/1000** |

\* 含 `*` 为按行业典型量级外推，非实测结论。

QCU 每步 75–93 ms 中内部执行仅 10–26 ms，其余 ~50–70 ms 为 CLI 子进程 + RPC 开销
（对照裸 Playwright 整任务 38 ms 可知：剩余瓶颈是进程边界而非 a11y 读取）。
batch 模式把 7 个动作压到 167 ms（~24 ms/步），是长任务的推荐用法。

## 3. 复现步骤

```bash
QCU_HOME="$(mktemp -d)" python -m pytest -q                                   # 回归套件
python scripts/benchmark_ui.py --mode separate --rounds 5 --output web.json   # web 主基准
python scripts/benchmark_ui.py --mode batch     --rounds 5 --output web2.json
python scripts/benchmark_playwright_floor.py   --rounds 5 --output floor.json # 下限
python scripts/benchmark_vision_baseline.py    --rounds 5 --output vision.json # macOS 视觉底线
python scripts/benchmark_desktop_ax.py         --rounds 3 --output dt.json    # macOS 桌面 AX
python scripts/verify_macos_ax.py --output ax.json                            # macOS AX 冒烟
```

桌面/视觉脚本会短暂打开真实窗口（Calculator / headful Chromium），结束自动关闭；
各脚本自建 daemon 并在退出时停止，不触碰用户日常 profile。

## 4. 已知限制与未覆盖项

1. **Windows UIA 仅为预览**：mock 契约 + 验证脚本就绪，真机未跑
   （`scripts/verify_windows_uia.py` 需在真实 Windows 交互桌面执行）。
2. **云端 VLM 延迟未测**："整步 16–43×"为外推，仅本地感知/执行为实测。
3. **fixture 偏简单**（26 元素表单 / 58 元素 App）；重页面历史量级 observe ~213 ms（230 元素）。
4. **canvas/WebGL/游戏**：无 a11y 树，QCU 明确不支持（报错而非猜测坐标）。
5. Electron 应用 AX 树为系统级 stub 的场景，建议切 web 层接管，桌面层不硬操作。
6. 本机 OCR 数字（332 ms accurate）比 1.6 时期记录（1149 ms）快，因屏幕内容不同，量级结论不变。

## 5. 原始数据

本轮原始 JSON 记录见随发布归档的 `bench-results/`（web-separate/web-batch/
vision-baseline/raw-playwright/desktop-ax，含每次调用的墙钟、内部耗时、payload 字节与
完整返回），或用第 3 节命令重新生成。
