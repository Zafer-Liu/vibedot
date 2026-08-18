# VibeDot 开发文档

本文档面向开发者：系统架构、固件细节、BLE 协议、HTTP API、逆向笔记、
烧录流程与踩坑记录。用户使用指南见 [README.md](README.md)。

## 1. 系统架构

```
多 AI 工具原生 hooks (Claude/Qoder/WorkBuddy/Codex)
        │  stdin JSON 事件 (hook_event.py 静默转发)
        ▼
vibedot_server.py  (FastAPI :8266)
        ├── 多 agent 状态机 (session_id → Agent{name,state,since,last_seen})
        ├── 防抖调度器 scheduler (1s 循环: 事件防抖刷屏/3s TICK/僵尸回收/连接守护)
        ├── PIL 渲染 296×152 → 1bpp 位图 5624 B (19 B/行 × 296 行)
        ├── bleak BLE 推送 (常驻连接直推, 断线回退扫描, WinRT 缓存损坏自动重启 bthserv)
        └── Web 控制台 (web/index.html: 状态/事件流/设备管理/一键接入)
        │  BLE GATT 写入 (有响应写, WRITE_NR 在 Windows 栈会静默丢包不可用)
        ▼
ESP32-C3 固件 (vibedot/vibedot.ino, 通用 BLE→SPI 位图管道)
        │  SPI 15MHz FSPI, UC8251D 时序 (逆向自原厂固件)
        ▼
2.9" 电子纸 296×152 (原厂 LUT 全刷 1442ms / partial LUT 快刷 241ms)
```

渲染全部在 PC 端完成（中文字体/布局/进度条），固件只做位图管道，
两端独立升级。

## 2. 目录结构

| 路径 | 说明 |
|---|---|
| `vibedot/vibedot.ino` | ESP32-C3 固件 v2（BLE + UC8251D 驱动 + 快刷 + 深度睡眠循环） |
| `vibedot/lut.h` | 原厂 LUT 波形表（376 B，逆向自 DROM 0x3c0f37a4） |
| `pc/vibedot_server.py` | 状态服务器 v2：多 agent 状态机 + 防抖推送 + Web 控制台 |
| `pc/vibedot_push.py` | 命令行渲染+BLE 推送（`--loop`/`--fast`/`--invert`/`--orient`） |
| `pc/hook_event.py` | hooks 转发器（stdin/argv JSON → HTTP，静默快退） |
| `pc/vibedot_event.py` | 通用事件适配器（任意 agent 主动上报） |
| `pc/web/index.html` | Web 控制台单页 |

## 3. 固件（ESP32-C3, QFN32 v0.4, 4MB, 原生 USB-Serial/JTAG）

### 3.1 引脚（三路逆向验证：SPI config + gpio 0x100070 位掩码 + 命令序列）

| 信号 | 引脚 |
|---|---|
| MOSI | IO7 |
| SCK | IO10 |
| CS | IO6 |
| DC | IO5 |
| RST | IO4 |
| BUSY | IO3（高=空闲，读前发 CMD 0x71） |
| PWR_EN | IO20（高=面板上电，关断置输入态） |

### 3.2 屏幕参数

- 驱动：UC8251D（NVS epd_config=1）
- 分辨率：296×152，TRES(0x61)=0x98 0x01 0x28
- 帧大小：5624 B (0x15F8)，1bpp MSB first，19 B/行
- 位图方向：横屏，`--orient` 可调
- SPI：FSPI 15MHz SPI_MODE0

### 3.3 BLE 命令集（写特征 `...0002`，首字节命令码）

| 命令 | 载荷 | 说明 |
|---|---|---|
| `0x00` | — | 复位帧接收计数 |
| `0x02` | ≤243 B | 位图数据块（有响应写防丢包） |
| `0x03` | — | 全刷（原厂 LUT 1442ms）；帧收满才执行 |
| `0x04` | — | 快刷（partial LUT 241ms，旧帧=上次画面）；连续 16 次自动全刷 |
| `0x05` | — | 面板深度睡眠（屏幕断电，BLE 保持） |
| `0x06` | — | 立即深度睡眠循环（远程关闭蓝牙） |
| `0x07` | — | 常开模式（退出睡眠循环，保持持续广播） |

状态特征 `...0003`（read/notify，4 B）：`[state, rx_hi, rx_lo, 0]`，
state 1=已刷新 / 0=帧不完整（rx_hi/rx_lo 为已收字节数，PC 据此整帧重传 ≤3 轮）。

### 3.4 刷新流程

```
全刷 epd_display(): power_on → init(130ms) → 原厂五表 LUT(0x20-0x24)
  → 0x10 旧帧全白 → 0x13 新帧 → 0x12 刷新 → BUSY 等待 → power_off (1442ms)

快刷 epd_display_fast(): 同上但 partial LUT (GoodDisplay UC8x51 单相位
  波形, VCOM 44B + 0x21-0x24 各 42B) + 0x10 旧帧=prev_frame (241ms)
  仅驱动变化像素: B2W(0x23)=0x80, W2B(0x24)=0x40 (极性反了交换即可)
```

**panelSlept 机制**（关键修复）：面板深睡后 VCOM 电压丢失，partial 波形
无法翻转像素（固件仍回 state=1，PC 误判成功、屏幕冻结）。固件在
`epd_panel_sleep()` / 深睡循环 / 定时唤醒时置 `panelSlept=true`，
loop 刷新分支检测到该标志强制全刷重建 VCOM，之后恢复快刷。

### 3.5 低功耗状态机

```
上电(冷启动): 自检棋盘格+边框 → 常开广播
断连 10 分钟 → 深度睡眠循环: esp_deep_sleep(55s) → 定时唤醒广播 20s
   ├─ 窗口内无人连: 继续睡
   └─ 窗口内连上: 服务连接; 收到 0x07 则清零 wokeFromTimer 恢复常开
0x06: 立即进入深度睡眠循环 (远程关闭)
0x07: 常开 (keepalive 由 PC 每 5 分钟续命)
```

深度睡眠时 USB-Serial 断电（COM 口从系统消失），定时唤醒窗口内 COM 重新
枚举——烧录需在窗口内进行或拔插 USB。

### 3.6 Just Works 配对

```cpp
BLESecurity* sec = new BLESecurity();
sec->setAuthenticationMode(ESP_LE_AUTH_REQ_SC_BOND);  // SC + Bond
sec->setCapability(ESP_IO_CAP_NONE);                  // 免 PIN
sec->setInitEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);
sec->setRespEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);
```

Windows 系统蓝牙面板可成功配对；PC 端 bleak 直连 GATT 不依赖配对，
两通道互不干扰。

## 4. BLE 协议

- Service：`8e400001-1f31-4a3a-9a2f-3d1c0a5b7e01`
- 写特征 `...0002`：write / write-no-rsp
- 状态特征 `...0003`：read / notify
- 设备名 `VibeDot`，MAC 7c:e8:b1:7a:3d:c0
- 传输：exchange_mtu(247) → 0x00 → 0x02 数据块(≤243B)×24 → 0x03/0x04
  → 轮询状态 ≤15s，state=0 整帧重传 ≤3 轮 → 成功后写 0x07 保持常开

## 5. HTTP API（端口 8266）

| 接口 | 说明 |
|---|---|
| `POST /api/event` | 接收事件 `{type,tool,summary,input,session_id,cwd,src}` |
| `GET /api/status` | 运行时间/agent 列表/push 计数/last_push |
| `POST /api/push[?full=1]` | 立即渲染+推送（快刷/全刷） |
| `GET /preview.png` | 末次渲染预览 |
| `POST /api/hook/install` | 一键接入（claude_user/claude_project/codex_desktop/workbuddy/qoder/all） |
| `GET /api/hook/status` | 各工具接入检测 |
| `GET /api/ports` | 枚举串口（标 ESP32 VID 0x303A） |
| `POST /api/flash` + `GET /api/flash/status` | 一键编译烧录（后台线程+日志流） |
| `GET /api/ble/scan` / `POST /api/ble/test` | 蓝牙扫描 / 连接测试 |
| `POST /api/ble/on` / `POST /api/ble/off` | 常开 / 立即深睡（远程关闭） |
| `POST /api/ble/sleep` | 面板睡眠（0x05） |
| `POST /api/autostart/install\|uninstall` | 开机自启动（shell:startup/VibeDot.vbs） |

### 状态机事件映射

| 事件 | 状态 |
|---|---|
| UserPromptSubmit / SessionStart / PostToolUse | thinking |
| PreToolUse | 按工具分类（command/coding/searching） |
| PostToolUseFailure | error |
| PermissionRequest | waiting（横幅反色「需要审批」） |
| Notification | 仅含 审批/授权/permission 等关键词判 waiting，普通通知忽略 |
| Stop / SessionEnd | done / conv_end（立即移除） |

超时回收：waiting 2 分钟自动转 done；error 2 分钟移除；done/idle 60s
移除；ACTIVE 状态 15 分钟无事件视为僵尸移除。agent 名只显示工具名
（Claude/Codex/WorkBuddy/Qoder），不带 session 后缀。

### 防抖与调度

- 紧急状态（waiting/done/error）：最小间隔 2s
- 常规状态：最小间隔 8s
- agent 运行中：TICK 30s 快刷更新时长
- 快刷/全刷比例：8:1（PC 侧），固件侧 16 次快刷兜底全刷
- `do_push` 整体 `asyncio.wait_for(120s)` 超时保险 + 全局 `_ble_lock`
  互斥（hold 与 push 串行，避免 Windows 栈并发扫描冲突 0x800704C7）
- 连接守护 `_auto_hold`：last_addr=None 每 90s 扫描重连；有地址每 300s
  keepalive 写 0x07（设备断连 10 分钟才入睡，5 分钟续命保证永不睡）

## 6. 烧录与编译

```powershell
$env:ARDUINO_DIRECTORIES_DATA='E:\Tool\Arduino15'
$env:ARDUINO_DIRECTORIES_DOWNLOADS='E:\Tool\Arduino15\staging'
$env:ARDUINO_DIRECTORIES_USER='E:\Tool\ArduinoUser'

d:\temp\espc3\tools\arduino-cli.exe compile --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc `
  --build-path d:\temp\espc3\build d:\temp\espc3\vibedot\vibedot
d:\temp\espc3\tools\arduino-cli.exe upload -p COM3 `
  --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc --input-dir d:\temp\espc3\build
```

- arduino-cli 1.5.2 + esp32 core 3.3.11；固件约 644 KB（4MB flash 的 49%）
- 设备深度睡眠时 COM3 消失：轮询 `GetPortNames()` 等广播窗口（20s）再上传
- 串口日志（115200）含 `[EPD] init/FAST/FULL` 各阶段耗时与 `[BLE]` 接收计数，
  排障先看这里

## 7. 逆向笔记（原厂固件 → 本固件映射）

| 项 | 值 | 来源 |
|---|---|---|
| 引脚 | MOSI=7 SCK=10 CS=6 DC=5 RST=4 BUSY=3 PWR_EN=20 | SPI bus/dev config + gpio 0x100070 三路验证 |
| 分辨率 | 296×152, TRES(0x61)=0x98 0x01 0x28 | 命令序列 + 内联常量 li 296/152 |
| 帧大小 | 5624 B (0x15F8) | 写屏循环计数 |
| 驱动 | UC8251D (NVS epd_config=1) | 初始化序列 0x42016030 |
| 刷新 | 0x10 全白旧帧 → 0x13 新帧 → 0x12 + delay(100) + BUSY | 0x4201653e |
| LUT | 376 B: VCOM 80 / W2W 56 / B2B 80 / B2W 80 / W2B 80, CMD 0x20-0x24 | 0x420162ea, DROM 0x3c0f37a4 |
| BUSY | 高=空闲；读前 CMD 0x71 | 0x42015f52 |
| 电源 | GPIO20 高=面板上电；关断置输入 | 0x420151e8 / 0x42015218 |

完整 4MB flash 备份：`d:\temp\espc3\flash_dump.bin`（esptool 可恢复原厂）。

## 8. 踩坑记录

1. **ESP32-C3 `SPIClass(HSPI)` 指向不存在的 SPI3**——传输静默无输出，
   面板无任何反应（刷新"秒完成"、BUSY 恒空闲）。必须用 FSPI。
2. **BLE write-no-rsp 连发丢包**——24 包丢 1-2 包帧永不收满。改有响应写
   + 状态回读 + 整帧重传。
3. **面板深睡后 partial 快刷无效但固件回 state=1**——VCOM 丢失，PC 误判
   成功、屏幕冻结数小时。panelSlept 标志强制唤醒后首次全刷。
4. **scheduler 顺序循环被 BLE 挂起卡死**——某次 `do_push` 中 Windows BLE
   栈永久挂起，后续 reap/TICK/hold 全停、日志静默（API 仍响应）。
   `asyncio.wait_for(120s)` 超时保险 + BleakClient timeout=20。
5. **0x800704C7（操作已被用户取消）**——hold 与 push 并发扫描互相取消。
   全局 `_ble_lock` 串行化；单次撞错 30s 后 TICK 自动重试即恢复。
6. **bleak `discover(return_adv=True)` 返回 `{addr: (BLEDevice, adv)}`**——
   用 `.values()` 解包；必须过滤 `d.address` 为空的异常设备（出现过
   `'0000None...'` 假地址导致 int() 崩溃）。
7. **快刷 LUT 不可用 SSD1680 系数据**（微雪 2.9 V2 是 0x32 单命令 LUT）——
   UC8251D 用 IL0373 家族格式（0x20-0x24 五表，GoodDisplay 单相位波形）。
8. **pyserial 打开 COM3 会复位设备**（USB-Serial/JTAG 重枚举）——调试时
   `dsrdtr=False` 打开可避免复位；先开串口再触发事件。
9. **PowerShell 5.1**：无 `&&`（用分号）；`curl` 是 Invoke-WebRequest 别名
   （用 `curl.exe`）；`Get-Date -UFormat %s` 与 Unix epoch 差 8 小时（时区）。
10. **GitHub 直连超时**：用 `ghfast.top` 前缀镜像下载 Arduino 核心包。
11. **Windows 系统面板配对失败**——固件未注册配对能力前，系统点连接会报
    错（程序直连不受影响）。加 BLESecurity Just Works 后，删除旧条目重新
    添加即可成功。
12. **系统面板已连接会抢占设备连接槽**（ESP32 Bluedroid 单连接）——
    系统面板显示"已连接"时 hold 连接被顶掉，推送断连。固件改多连接
    （getConnectedCount()）+ 删除系统配对条目（注册表
    `HKLM\SYSTEM\...\BTHPORT\Parameters\Devices\<mac>`）双管齐下。
13. **WRITE_NR 在 Windows 栈静默丢包**——单连接时丢 1-2 包（帧不完整），
    多连接时全丢（0/5624）。不能用 response=False 传输帧数据。
14. **固件 status 特征只在刷屏分支更新**——写块后读 rx 计数是旧值，
    增量续发协议误判丢包疯狂重发。只能"写满整帧→刷屏→读 state"。
15. **WinRT 设备缓存长期使用后损坏**——连续高频 GATT 操作 ~2 分钟后
    BleakClient 报 `invalid literal: '0000None...'` 怪地址 / device not
    found，扫描能看到但连不上。服务器检测到后自动
    `Restart-Service bthserv -Force`（管理员权限, 5 分钟节流）恢复。
16. **RTS/DTR 复位不清 wakeup cause**——软复位后
    `esp_sleep_get_wakeup_cause()` 仍回 TIMER，wokeFromTimer=true 致
    20s 后入睡，串口/broadcast 间歇消失（曾误判固件卡死）。
    观察设备行为前先确认睡眠循环相位。
17. **setMTU 与 PC exchange_mtu(517)**——WinRT 栈 exchange_mtu 抛异常
    （静默吞掉），ATT MTU 保持 23B，帧分片 ~296 笔有响应写 ≈ 4.2s/帧。
    这是当前单帧传输耗时下限，固件 setMTU(517) 保留在 init 后。
18. **推送性能**：活跃期间 TICK=3s + 传输 4.2s ≈ 实际 ~4.2s/刷。更快的
    路线（未做）：差分块传输（固件加块偏移协议+位图完整性）、
    连接参数优化（3.3.11 Arduino 预编译库不暴露 esp_gap_ble_api.h）。

## 9. 调试流程（屏幕不刷新时）

1. 看服务器日志：`[push] 已刷新` = BLE 链路 OK；`error` = 连接问题
2. `POST /api/ble/test` 读状态特征，确认设备在线
3. 开串口（COM3 115200，`dsrdtr=False`）看 `[EPD]` 日志：
   - 无 `[BLE] fast/full refresh cmd` → BLE 写入没到固件（查 PC 侧）
   - 有 cmd 但无 `[EPD]` → 固件 loop 阻塞或帧不完整
   - `panel was asleep -> force full refresh` → 深睡唤醒路径正常
4. 设备深睡时 COM3 消失属正常；烧录/串口调试需等 20s 广播窗口

## 10. 固件版本

| 版本 | 变更 |
|---|---|
| v2 | 多 agent、快刷 241ms、深度睡眠循环、0x05/0x06/0x07 命令、panelSlept 强制全刷、Just Works 配对 |
| v3 | 多连接支持（getConnectedCount, 系统面板连接不抢占）、setMTU(517) |
| v4 | prevValid 机制：任何启动/复位后 prev_frame 丢失（RAM 清零）而面板保留旧画面，快刷以全黑当旧帧 → 黑色内容像素被判 B2B 不驱动 → **白屏**。首次刷新（无论快/慢命令）强制全刷重建基准；去掉开机棋盘格自检（反复复位时闪棋盘格） |
| v5 | 默认永久常开：删除"断连 10 分钟自动深睡"，BLE 持续广播；仅 0x06（网页"立即深睡"）可关闭 |
| v6 | 白色基准同步：v4 的"复位后首刷强制全刷(1442ms)"在供电边际设备上形成 全刷→电流尖峰→复位→再全刷 的恶性循环。改为：复位后首推快刷一帧全白（RAM 旧帧全黑→全像素 B2W 驱动→面板确定变白，prev 与物理一致，仅 241ms=全刷 1/6 电流），回 state=0 骗 PC"帧不完整"→PC 自动整帧重传→正常快刷显示。深睡唤醒（VCOM 丢失）仍走全刷。PC 端零改动 |
| v7 | 删除 BLESecurity Just Works 配对注册：系统蓝牙面板配对后会自动重连抢占连接（0x800704C7 写取消）并用系统 GATT 枚举污染 WinRT 缓存（invalid literal '0000None' 反复出现、bthserv 重启也清不净的根源）。bleak 直连不需要配对，系统连不上=永绝后患 |
| v8 | **放弃 v6 基准同步**：掉电重启后 panelSlept=false 但面板 VCOM 实际已丢（复位期间 PWR 引脚悬停、面板断电），无 VCOM 的快刷（含基准同步）驱动不动任何像素、**静默失败但设备仍回 state=1**，prevValid 被置真 → 此后所有快刷全部无效、屏幕冻结，只有全刷能救。v8 恢复"任何启动首刷一律全刷"。**配套决策：PC 端默认全刷模式（FULL_REFRESH_EVERY=1）彻底绕开 VCOM 依赖，快刷默认禁用** |

## 11. 白屏问题排查实录（2026-08-17）

现象：一刷新就白屏（快刷全白）。定位路径：
1. 验证 PC 帧数据：`render_state()` 帧字节 43.5% 非白 ✓（排除渲染/转换）
2. 直推测试帧（上半黑下半白+白窗黑框，全刷）**显示完美** ✓（排除 BLE 传输/固件接收/LUT/极性）
3. 锁定：快刷的 prev_frame 在设备复位后丢失 → v4 prevValid 强制全刷修复 ✓
4. 伴生问题：设备在刷新电流尖峰时 USB 掉线+BLE 断连（供电边际）。软件侧全套兜底：
   串口写失败秒回退蓝牙、离线指数退避、25s 快重连。

## 12. 2026-08-18 最终定案

- **全刷模式为默认**（FULL_REFRESH_EVERY=1，环境变量 VIBEDOT_FULL_EVERY 可调）：
  对 VCOM 丢失免疫，设备复位/掉电后无需任何干预继续正常刷新。代价 1.4s 黑白闪烁。
- 刷新节拍体系：事件即时(2s) / 常规防抖(3s) / 活跃期 15s / 空闲心跳 10min /
  失败退避 15→30s / 设备重连(hold 成功)即恢复正常节拍
- 顶栏秒级时钟：每推必变，肉眼确认链路健康（快刷无闪烁曾导致"其实在刷但
  看起来没刷"的误判）
- 状态兜底：thinking/searching 超 3min、coding/command/subagent 超 10min 无事件
  自动转 done（平台 hook 不是每回合都发 Stop 事件）
- 控制台蓝牙接口统一走 _ble_cmd（全局 _ble_lock + 常驻连接 use_cached_services=False
  + 无连接扫描回退 + 失败丢弃连接重建重试）；/api/status 暴露 device/pushing/
  pending/last_result，控制台设备栏每 2s 实时显示在线状态
- 串口守则：写超时**绝不关闭句柄**（close 活句柄会复位芯片；list_ports 在枚举
  抖动时不可靠，据此强关曾造成"每轮推送→复位→重枚举"的死循环）
- 教训：**快刷的适用前提是 VCOM 确定存活**；任何"复位后 VCOM 状态未知"的场景
  都必须全刷，也不能用快刷做基准重建（静默失败 + 假 state=1，极难排查）
