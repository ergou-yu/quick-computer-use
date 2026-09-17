# QCU 1.8 测试报告（Test Report）

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
