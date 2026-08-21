# QCU（Quick Computer Use）技术解析与性能评测

> 版本：1.6 · 评测日期：2026-08-21 · 评测机器：macOS (darwin 25.2.0, Apple Silicon)
> 代码仓库：https://github.com/ergou-yu/quick-computer-use
>
> 本文所有数字均为本次实测或来自本机积累的真实遥测数据（`~/.qcu/telemetry.jsonl`，1280 条记录），非营销口径。
> 1.6 版修复了初稿评测中发现的全部 6 个缺陷（见第 6 节），桌面点击验证路径从 4.5–8.8 s 降到 **64–84 ms**。

---

## 一句话总结

QCU 把计算机操作 agent 的"眼睛"从**截图 + 云端视觉大模型**换成了**操作系统自带的可达性树（Accessibility Tree）**：屏幕上每个按钮、输入框、菜单项，操作系统本来就知道它是什么、在哪、能不能点。QCU 直接读这份结构化数据、直接对元素本身发起动作，从而把每一步的耗时从**秒级（3–6 s）压到百毫秒级（60–350 ms）**，且定位是确定性的——不存在"看错坐标"这回事。

---

## 1. 背景：主流"Computer Use"方案的底层逻辑

以 GPT-4o / Claude / Operator 类产品为代表的主流计算机操作方案，走的都是**视觉回路**：

```
┌────────────┐   截图(≈300ms)   ┌──────────┐  上传网络往返   ┌─────────────┐
│  目标屏幕   │ ───────────────> │  截图 PNG │ ─────────────> │  云端 VLM    │
└────────────┘                  └──────────┘                 └─────────────┘
      ▲                                                           │
      │        合成鼠标/键盘事件（坐标 = 模型"猜"出来的像素位置）          │
      └───────────────────────────────────────────────────────────┘
```

每一步（观察→决策→执行）都要支付四笔固定成本：

1. **截图**：全屏 Retina PNG 约 284 ms、约 9 MB（实测）；
2. **上传**：网络往返，几百 ms 到数秒；
3. **VLM 推理**：大模型逐像素分析"登录按钮在哪"，1–3 s；
4. **概率性定位**：输出的是坐标猜测，小字、密集 UI、非标准控件上会点错，错了就要再截一轮图——错误恢复进一步放大延迟。

这不是实现质量问题，是**范式**问题：视觉模型在重新推导操作系统已经知道的信息。

## 2. QCU 的底层逻辑：不截图，读结构

QCU 的洞察是：**macOS、Chromium 都内建了"无障碍树"（Accessibility / ARIA tree）**——这是给屏幕阅读器（VoiceOver、NVDA）用的 API，每个 UI 元素在其中都有角色（button/textfield/menu_item）、名称、值、坐标、启用状态。屏幕上是 26 个元素还是 230 个，读出来的是同样干净的结构化列表。

```
┌────────────┐  AX API / CDP 协议（本地调用, 6–400ms）  ┌──────────────────┐
│  目标应用   │ ───────────────────────────────────────> │ 结构化元素列表     │
└────────────┘                                          │  ref_42 button    │
      ▲                                                 │  "Sign in" (x,y)  │
      │   直接对元素本体执行：AXPress / locator.click()     └──────────────────┘
      └───────────────────────────────────────────────────（无截图、无 VLM、无网络）
```

一次 `qcu observe` 返回的就是这样的 JSON（真实输出节选）：

```json
{"ref": "obs_13:ref_1", "role": "edit", "name": "Username",
 "enabled": true, "focused": false, "bounds": {"x": 288.5, "y": 150.0, ...}}
```

LLM 拿到的不是一张需要"看图找按钮"的截图，而是一份可以直接引用的清单——`ref_1` 就是 Username 输入框，**引用是确定的，不是猜的**。

### 什么时候仍然用截图？

QCU 把截图降级为**最后的兜底**（`screenshot_fallback` 层）：目标是 canvas/WebGL 游戏、或 a11y 树确实看不见时才走。且即便走视觉，也是**本地** Apple Vision OCR（`VNRecognizeTextRequest`）优先，不默认依赖云端 VLM。

## 3. 具体实现：四层架构

```
                 ┌─────────────────────────────┐
   qcu observe / │  CLI → 常驻 Python daemon     │   RPC over loopback TCP
   qcu act       │  (进程复用: 省 ~300ms/命令)   │   (~1ms)
                 └──────────────┬──────────────┘
                                │
                     ┌──────────▼──────────┐
                     │  Router（规则路由）    │  priority 0–100 的规则链
                     │  canvas→截图 / web→CDP │  按 context + 特征选层
                     └──────────┬──────────┘
        ┌───────────────┬───────┴────────┬────────────────┐
        ▼               ▼                ▼                ▼
  ┌───────────┐  ┌────────────┐  ┌──────────────┐  ┌──────────────────┐
  │ web_a11y  │  │ desktop_ax │  │ desktop_     │  │ screenshot_      │
  │ CDP 接入   │  │ macOS AX   │  │ appleevents  │  │ fallback         │
  │ 常驻Chromium│ │ API 原生操作│  │ AppleScript  │  │ 截图+OCR(兜底)    │
  └───────────┘  └────────────┘  └──────────────┘  └──────────────────┘
```

### 3.1 web_a11y 层（网页）

- **常驻 Chromium daemon**：首次用时 detached 拉起一个 Chromium（`browser_daemon.py`，端口从 9222 起探空位），之后每个 `qcu` 命令都用 Playwright `connect_over_cdp` 附着到**同一个**浏览器——不新开浏览器、不重置 DOM，表单草稿和焦点跨命令存活。
- **观察**：CDP `Accessibility.getFullAXTree` 一次拿全树（`web_a11y.py:507`），Python 侧按角色分级裁剪：交互元素全保留，文本容器限长（HN 230 元素 → raw_tree 23.6 KB，塞得进上下文）。
- **动作**：主路径 Playwright locator（`[data-llm-ref="ref_N"].click()`），坐标点击为兜底。
- **并发优化**：元素几何信息用 `asyncio.gather` 并发拉取，把原来串行 360 ms 的往返压掉（`web_a11y.py:772-831`）。

### 3.2 desktop_ax 层（macOS 原生应用，主力路径）

- **元素获取**：`AXUIElementCreateApplication(pid)` 拿 app 级引用，递归 `AXChildren` 遍历（`desktop_ax.py:359-495`）。关键优化是 **role-first**：先读最便宜的 `AXRole` 决定是否值得读更贵的属性——实测 Calculator 全树 1166 节点，朴素遍历 1227 ms，QCU 遍历 **400 ms（3 倍速）**。
- **动作**：优先对元素本体执行 `AXUIElementPerformAction(elem, "AXPress")`（实测单次 **5.8 ms**）——不依赖坐标、不依赖前台焦点，后台窗口也能点。`fill` 直接 `AXValue=` 设置文本，不模拟键盘。
- **兜底链**：AXPress → Apple Events → CGEvent 坐标点击，层层降级。
- **动作验证**：每次 click/fill 前后做 AX 状态快照 diff（`_ax_verify.py`），无前置 sleep、100 ms 轮询、命中即早退——专治 Electron 应用"err=0 但实际没反应"的 stub no-op。实测命中时中位 **25 ms**。
- **点击安全门**：坐标兜底前用 Quartz 窗口列表做命中测试，确认点击会落在目标窗口而不是更高层级的悬浮窗/通知上（`_click_safety.py`），杜绝"点进飞书"类事故。

### 3.3 daemon 与会话

- **Python CLI daemon**：每条 `qcu` 命令原本要付 ~300 ms 解释器启动 + import；daemon 常驻后每命令只付 RPC 往返（实测 CLI 墙钟 = 内部耗时 + ~47 ms）。
- **会话状态**（`~/.qcu/session.json`）：当前 URL、元素 ref 缓存、目标 app、observation generation（防陈旧 ref）。
- **遥测**：每次 observe/act 追加一条 JSONL，按层聚合 p50/p95——本文的历史数据就来自它。

### 3.4 规则路由（router）

不是所有任务都该走同一条路。router 是一张优先级规则表（`router/rules.py`）：

| 优先级 | 规则 | 走哪层 | 触发条件 |
|---|---|---|---|
| 100 | canvas_target | 截图 | 目标在 canvas/WebGL 上 |
| 95 | visual_confirm | 截图 | 关键动作（如付款）强制视觉复核 |
| 70 | web_a11y | CDP | web 上下文（默认最快路径） |
| 66 | desktop_webview_blind | desktop_ax+提示 | 桌面应用是网页壳（如 Electron），提示 LLM 切 T3 |
| 65 | desktop_ax | AX API | 桌面上下文 + AX 权限可用 |
| 60 | desktop_appleevents | AppleScript | AX 未授权但 Automation 可用 |
| 55 | desktop_no_ax | 截图 | 两条 a11y 路都不通（罕见） |

每次决策（含所有备选）写入遥测——这是未来把规则链换成 ML 分类器的免费训练数据。

## 4. 量化性能测试

### 4.1 Web 路径（本次实测，QCU 自带 Chromium，daemon 常驻）

| 操作 | n | p50 | p95 | 说明 |
|---|---|---|---|---|
| `qcu --version`（纯进程开销） | 5 | **47 ms** | 48 ms | CLI 墙钟 = 内部耗时 + 这 47 ms |
| observe 本地表单页（26 元素） | 10 | **77 ms** | 81 ms | 内部仅 19.5 ms |
| observe example.com | 5 | **66 ms** | 66 ms | 内部 6.1 ms |
| observe Hacker News（230 元素） | 5 | **213 ms** | 215 ms | 内部 151 ms，含整页文本 |
| act: fill 输入框 | 5 | **64 ms** | 75 ms | locator 主路径 |
| act: click 复选框（含验证） | 5 | **77 ms** | 79 ms | |
| navigate（热浏览器） | 1 | 1786 ms | — | 页面加载为主 |

对照遥测（近 30 天 352 次真实调用）：web_a11y p50 **82 ms**，成功率 **100%**。

### 4.2 桌面路径（macOS Calculator，273 个 AX 元素）

| 测量项 | 结果 | 说明 |
|---|---|---|
| QCU observe（墙钟） | **p50 463 ms**（n=8） | 内部 400 ms |
| 朴素 AX 全树遍历（对照组） | p50 1227 ms | 同 1166 节点，每节点读全部属性 |
| **遍历加速比** | **≈3×** | role-first 按需读属性 |
| 裸 AXPress（对照组） | p50 **5.8 ms** | OS 层面动作执行成本 |
| 动作验证（命中时） | p50 25 ms（遥测 49 次） | 无前置 sleep，命中早退 |

对照遥测（716 次真实调用）：desktop_ax p50 **327 ms**，p95 3561 ms，成功率 **92%**。

### 4.3 视觉回路底线（同机实测，"截图流派"的本地部分）

| 环节 | 耗时 |
|---|---|
| 全屏 Retina 截图（Quartz，9.2 MB PNG） | p50 284 ms |
| Apple Vision 本地 OCR | p50 1149 ms |
| **本地小计（还没开始"理解"）** | **1433 ms** |
| 云端 VLM 推理（GPT-4o/Claude 类） | +1000～3000 ms |
| 上传/往返网络 | +数百 ms～数秒 |
| **典型单步合计** | **3–6 s**（与 SKILL.md 宣称一致） |

### 4.4 总对比表

| 方案 | 单步观察+执行 | 定位方式 | 失败模式 | 离线可用 |
|---|---|---|---|---|
| **QCU（web/T1 桌面）** | **0.06–0.5 s** | 结构化 ref（确定性） | 元素不在 a11y 树（罕见，明确报错） | ✅ |
| 视觉流派（GPT-4o/Claude/Operator 类） | 3–6 s | 像素坐标（概率性） | 看错位置→静默点错→重试循环 | ❌ |
| AppleScript/手动脚本 | 0.1–1 s | 硬编码路径 | 脆弱，每个 app 单写 | ✅ |
| 人工操作 | 0.3–0.8 s/步 | 眼+手 | 疲劳 | — |

**加速来源分解**（以 HN 页面一步为例，QCU 213 ms vs 视觉流派 ~4000 ms）：

| 省掉的环节 | 省掉的成本 |
|---|---|
| 不截图 | −284 ms |
| 不上传 | −数百 ms |
| 不做 VLM 推理 | −1000～3000 ms |
| 结构化列表 vs 图像 token | 23.6 KB 文本 ≈ 6K tokens vs 9 MB 图像 ≈ 数万 token（决策更快更便宜） |
| daemon 复用 | −300 ms/命令（进程启动） |
| AXPress 直达元素 | 5.8 ms（vs 合成鼠标事件再等 UI 响应） |

## 5. 为什么"比 GPT 那些东西"强——以及诚实的边界

**强在哪里（实测支撑）：**

1. **快 10–60 倍的单步延迟**：百毫秒 vs 秒级（上表）。15 步任务 QCU ≈ 10 s 走完控制回路，视觉流派仅截图+推理就要 1 分钟以上。
2. **确定性定位**：`ref_42` 就是那个按钮。不存在视觉模型把"取消"看成"确认"。实测 web 路径 100% 成功率。
3. **反馈带宽高**：返回的是全页结构化清单（可分页/过滤/compact），LLM 一次看到的东西又多又准，而截图受分辨率与 token 上限制约。
4. **每步免费验证**：AX 状态 diff 让"点了但没生效"立即暴露（25 ms），视觉流派得再截一张图"看看变了没"。
5. **省钱省隐私**：不调视觉 API、不出网。屏幕内容以结构化文本形式留在本地。

**诚实的边界（不强于 GPT 的地方）：**

- QCU 不替代智能——驱动它的仍是 LLM（本 skill 跑在 agent 里）；QCU 替换的是**感知与执行通道**，不是决策。
- canvas/WebGL/游戏（T4）没有 a11y 树，QCU 目前明确不支持（报错而非硬猜），这是它主动放弃的场景。
- Electron 应用（Slack/Notion 等）的 AX 树是 stub（系统级限制，实测 8 款确认），QCU 的对策是检测到后提示切到 T3 web 接管，而不是假装能操作。
- 依赖 macOS 三项 TCC 权限（Accessibility / Input Monitoring / Screen Recording），首次配置有门槛。

## 6. 初稿评测发现的问题 —— 已全部在 1.6 修复并复测

初稿（针对 1.5）实测发现 6 个真实缺陷。**1.6 已全部修复**，每项都有复现验证：

| # | 问题（1.5 实测发现） | 根因 | 1.6 修复 | 修复后实测 |
|---|---|---|---|---|
| 1 | `observe` 遇到 `<input type=number/range>` 整体崩溃 | CDP 把数值 value 序列化为 **int**，`clean_text` 的 `re.sub` 收到 int 抛 TypeError | `clean_text` 类型宽容（bool→"true"/"false"，0→"0"） | 数值页 observe 正常，spinbutton='7'、slider='50' ✅ |
| 2 | daemon 里 app 重启后 observe 返回 0 元素 | NSWorkspace 进程列表在长驻进程里**永不刷新**，名字匹配到死 pid | 每个候选 AXWindows 探活；全死则从 Quartz 窗口表解析活 pid | 同 daemon 跨 quit+reopen：276→0（坏）→**276**（修复）✅ |
| 3 | SwiftUI 按钮全部匿名（name 空） | 标签在 `AXDescription`，QCU 只读 `AXTitle` | 空 title 的交互元素回退读 AXDescription | Calculator 命名按钮 1 → **54** 个 ✅ |
| 4 | 点击实际生效却报 `verify=no`（假阴性），单次 4.5–8.8 s | **before 快照在 AXPress 之后才拍**——快速效果已落地，before==after 永不匹配 | 快照提前到动作前传入；app pid 改由元素自身推导（不会被陈旧列表污染） | 前台 **64 ms** / 后台（激活重试）**84 ms**，8/8 verified ✅ |
| 5 | 后台 app 的 AXPress 静默 no-op（err=0 无效果） | 部分 app（Calculator 键盘）忽略后台 AXPress | verify 失败后：激活目标 app → 重按 → 2.5s 宽窗验证，再走慢兜底 | 后台点击 84 ms verified ✅ |
| 6 | 无 session 时 `observe --app` 路由去 web 层 | 自动建会话默认 web，忽略显式桌面 scoping | `--app/--pid/--window` 蕴含 desktop 上下文 | 无 session `--app` observe 276 元素 ✅ |

修复后基准（Calculator，daemon 常驻，8 次连续数字键点击）：

| 指标 | 1.5（修复前） | 1.6（修复后） |
|---|---|---|
| 桌面 observe（275 元素） | p50 463 ms | p50 **337 ms** |
| 单次点击（含验证）·前台 | 4500+ ms 且报 ok=false | **64 ms**，ok=true |
| 单次点击（含验证）·后台 | ~8800 ms 且报 ok=false | **84 ms**，ok=true |
| 点击验证命中率 | 0/8（全假阴性） | **8/8** |
| 命名按钮可见性 | 1/57 | **54/57** |

回归测试：434/434 通过（422 旧 + 12 新增回归，覆盖上述每个修复点）。

## 7. 结论

QCU 的提速不是"优化了截图流程"，而是**换了信息源**：操作系统与浏览器早已维护着完整、精确、实时更新的 UI 结构树，QCU 直接消费它，把感知（截图→VLM→坐标猜测）换成读取（AX/CDP→结构化清单→确定引用），把执行（坐标合成事件）换成直达（AXPress/locator）。在这个范式下，单步百毫秒、100% 定位成功率、本地零网络，是结构带来的，不是调优调出来的。

---

*评测方法备忘：web 基准用 `file://` 本地表单页（26 元素）+ example.com + Hacker News，各 5–10 轮取 p50/p95；桌面基准用 macOS Calculator（273 元素）+ 裸 pyobjc 对照组；视觉底线用 QCU 同款 Quartz 截图管线 + Apple Vision OCR 各 5 轮；历史数据来自本机 30 天 1280 条真实遥测。测试环境：Python 3.12.11 / uv venv / Playwright Chromium，macOS 三项 TCC 权限齐备。*
