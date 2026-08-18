# -*- coding: utf-8 -*-
"""
VibeDot Server v2 — 监听多个 AI agent 事件, 推送水墨屏 + Web 控制台

架构:
  Claude Code / Codex / WorkBuddy / Qoder ... -> hook_event.py / vibedot_event.py
       -> POST /api/event -> 多 agent 状态机 (session_id 区分)
       -> 防抖渲染 (agent 列表 / 已运行时间 / git 周图表)
       -> BLE 推送水墨屏 (快刷 0x04 为主, 周期性全刷 0x03 清残影)
       -> Web 控制台 (GET /) 实时状态/agent 列表/事件流/预览/一键接入/开机自启

用法:
  python vibedot_server.py --project D:\\code\\myapp [--port 8266]
"""
import argparse
import asyncio
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vibedot_push as vp

BASE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE, "web")
PREVIEW_PNG = os.path.join(WEB_DIR, "preview.png")
DEFAULT_PORT = 8266

# ---------------- 状态机 ----------------
STATE_TEXT = {
    "idle":      "空闲",
    "thinking":  "思考中",
    "coding":    "编码中",
    "command":   "执行命令",
    "searching": "检索中",
    "subagent":  "子任务",
    "waiting":   "! 等待确认",
    "done":      "OK 完成",
    "error":     "x 出错",
    "conv_end":  "对话结束",
}
IMMEDIATE_STATES = {"waiting", "done", "error"}
BANNER_INVERT = {"waiting", "error"}
ACTIVE_STATES = {"thinking", "coding", "command", "searching", "subagent"}

TOOL_CLASS = {
    "Edit": "coding", "Write": "coding", "MultiEdit": "coding", "NotebookEdit": "coding",
    "Bash": "command",
    "Read": "searching", "Grep": "searching", "Glob": "searching",
    "WebSearch": "searching", "WebFetch": "searching", "TodoWrite": "coding",
    "Task": "subagent", "Agent": "subagent",
}

MIN_PUSH_INTERVAL = 3        # 常规状态最小刷屏间隔 (hook 事件后 ~3s 内上屏)
IMMEDIATE_MIN_INTERVAL = 2   # 立即状态最小间隔
TICK_INTERVAL = 15           # 活跃期间周期重刷: 检测到对话 (agent 活跃) 每 15s
                             # 蓝牙快刷更新状态; 3s 节拍下每 ~25s 就要一次全刷
                             # (大电流, 供电边际设备会弹跳), 15s + 对齐的全刷
                             # 间隔把全刷降到 ~4 分钟一次
FULL_REFRESH_EVERY = int(os.environ.get("VIBEDOT_FULL_EVERY", 1))
                             # 全刷/快刷比例: N 次里 1 次全刷。默认 1 = 全部
                             # 全刷 (最稳: 对 VCOM 丢失免疫, 设备复位后依然
                             # 正常刷新; 代价 1.4s 黑白闪烁)。烧录 v8 固件后
                             # 可改回 16 恢复无闪烁快刷 (15s 节拍下 4 分钟
                             # 一次全刷清残影)
IDLE_HEARTBEAT = 600         # 空闲心跳: 无 agent 活跃时每 10 分钟仍刷一次,
                             # 让顶栏时钟走字/状态不过夜 (否则懒加载下时钟
                             # 冻结在最后一次推送, 早上看像"坏了")
SERIAL_PUSH_TIMEOUT = 25     # 串口推送硬上限 (超时 = usbser 写 IRP 挂起: 冷却串口并立刻回退蓝牙)
BLE_PUSH_TIMEOUT = 120       # 蓝牙推送硬上限 (含等锁时间: 连接守护卡死时推送不被无限阻塞)


class Agent:
    """一个正在运行的 agent 会话 (session_id 区分, 支持多 agent 并行)"""
    def __init__(self, key):
        self.key = key
        self.name = key                       # 展示名: Claude·a3f2
        self.state = "thinking"
        self.since = time.time()
        self.started = time.time()
        self.summary = ""
        self.last_seen = time.time()

    def to_dict(self):
        return {"key": self.key, "name": self.name, "state": self.state,
                "state_text": STATE_TEXT.get(self.state, self.state),
                "since": self.since, "started": self.started,
                "summary": self.summary, "last_seen": self.last_seen}


class Hub:
    def __init__(self, project):
        self.project = project
        self.started = time.time()            # 服务器启动时刻 -> 已运行时间
        self.agents = OrderedDict()           # session_id -> Agent
        self.events = deque(maxlen=200)
        self.counts = {"coding": 0, "command": 0, "searching": 0, "waiting": 0, "turns": 0}
        self.last_push = 0.0
        self.push_eligible_at = None
        self.push_fast = True
        self.push_count = 0
        self.last_addr = None
        self.pushing = False
        self.last_push_result = None          # 最近一次推送结果 {ok,path,dur,ts}
        self.push_fail_streak = 0             # 连续推送失败次数 (离线退避用)

    # ---- 事件分类 (hook 事件或直接状态; Claude Code / Qoder / WorkBuddy 同构) ----
    # 注: 不采集具体执行内容 (命令/文件/prompt), 只用状态与工具名
    def classify(self, ev):
        t = ev.get("type", "")
        tool = ev.get("tool", "")
        # vibedot_event.py 直接上报状态 (coding/waiting/...)
        if t in STATE_TEXT:
            return t, ""
        if t == "UserPromptSubmit":
            return "thinking", ""
        if t == "PreToolUse":
            return TOOL_CLASS.get(tool, "coding"), ""
        if t == "PostToolUse":
            return "thinking", ""
        if t == "PostToolUseFailure":
            return "error", ""
        if t == "PermissionRequest":     # 真实审批请求 (Qoder/WorkBuddy/Claude)
            return "waiting", ""
        if t == "Notification":
            # 只有明确的权限类通知才算审批, 普通通知忽略 (不改状态)
            msg = (ev.get("summary") or "").lower()
            if any(k in msg for k in ("permission", "approval", "approve", "授权", "审批", "允许", "权限")):
                return "waiting", ""
            return None
        if t == "SessionStart":
            return "thinking", ""
        if t == "Stop":
            return "done", ""
        if t == "SessionEnd":
            return "conv_end", ""
        return None

    def _agent_name(self, ev, key):
        # 只显示工具名, 不带 session 后缀 (数字/短 id 后缀无意义且难看)
        src = ev.get("src") or ""
        return {"claude": "Claude", "codex": "Codex", "workbuddy": "WorkBuddy",
                "qoder": "Qoder", "kimi": "Kimi", "minimax": "MiniMax"
                }.get(src.lower(), src or "Agent")

    def on_event(self, ev):
        key = ev.get("session_id") or "default"
        r = self.classify(ev)
        # 事件流只记录 类型/工具, 不记录具体内容
        self.events.appendleft({
            "ts": datetime.datetime.now().strftime("%H:%M:%S"),
            "type": ev.get("type", "?"), "tool": ev.get("tool", ""),
            "summary": "",
            "agent": key[:8],
        })
        if r is None:
            return
        state, detail = r
        ag = self.agents.get(key)
        if ag is None:
            ag = self.agents[key] = Agent(key)
            ag.state = state
            ag.started = time.time()
        else:
            if state == "conv_end":           # 会话结束: 移出运行列表
                self.agents.pop(key, None)
                return
            if state != ag.state:
                ag.state = state
                ag.since = time.time()
        ag.name = self._agent_name(ev, key)
        ag.summary = detail
        ag.last_seen = time.time()
        if state == "waiting":
            self.counts["waiting"] += 1
        elif state in ("coding", "command", "searching"):
            self.counts[state] += 1
        if ev.get("type") == "UserPromptSubmit":
            self.counts["turns"] += 1
        # 计划刷屏
        now = time.time()
        gap = now - self.last_push
        if state in IMMEDIATE_STATES:
            self.push_eligible_at = now + max(0, IMMEDIATE_MIN_INTERVAL - gap)
        else:
            self.push_eligible_at = now + max(0, MIN_PUSH_INTERVAL - gap)

    def overall(self):
        """全部 agent 的综合状态 (waiting/error 优先, 其次 running)"""
        if not self.agents:
            return "idle"
        states = {a.state for a in self.agents.values()}
        for s in ("waiting", "error"):
            if s in states:
                return s
        if states & ACTIVE_STATES:
            return "processing"
        return "idle"

    def reap_idle(self):
        """回收与状态兜底:
        - waiting/error 卡住 2 分钟自动解除
        - ACTIVE 状态超时无事件 -> 先降级为 done (对话结束但平台没发 Stop/
          SessionEnd 事件时, 屏幕不能永远显示"思考中"; hook 事件覆盖不全的
          服务端兜底), 降级后 60s 无新事件再移除
        - 僵尸 agent 15 分钟无事件移除 (agent 异常退出后 hook 不再发事件)"""
        now = time.time()
        # 状态降级时限: 思考/检索 3 分钟, 编码/命令/子任务 10 分钟
        stale_limit = {"thinking": 180, "searching": 180,
                       "coding": 600, "command": 600, "subagent": 600}
        for key in list(self.agents):
            a = self.agents[key]
            if a.state == "waiting" and now - a.since > 120:
                a.state = "done"          # 审批超时: 视为已处理, 退出横幅
                a.since = now
                self.push_eligible_at = now + 2   # 刷掉横幅
            elif a.state == "error" and now - a.since > 120:
                self.agents.pop(key, None)
                self.push_eligible_at = now + 2
                continue
            lim = stale_limit.get(a.state)
            if lim and now - a.last_seen > lim:
                # 超时无事件: 平台漏发 Stop 的兜底, 降级 done 让屏幕显示完成
                print(f"[hub] {a.name} {a.state} 超 {lim}s 无事件 -> done")
                a.state = "done"
                a.since = now
                self.push_eligible_at = now + 2
            if a.state in ACTIVE_STATES and now - a.last_seen > 900:
                self.agents.pop(key, None)          # 僵尸 agent: 15 分钟无事件
                self.push_eligible_at = now + 2
                continue
            if a.state not in ACTIVE_STATES and a.state != "waiting" \
                    and now - a.last_seen > (60 if a.state in ("done", "idle") else 600):
                self.agents.pop(key, None)
                self.push_eligible_at = now + 2   # 列表变化刷屏

    def any_active(self):
        return any(a.state in ACTIVE_STATES or a.state == "waiting" for a in self.agents.values())


hub: Hub = None
app = FastAPI()


class Event(BaseModel):
    type: str = ""
    tool: str = ""
    summary: str = ""
    input: dict = {}
    session_id: str = ""
    cwd: str = ""
    src: str = ""             # 来源工具: claude/codex/workbuddy/qoder


# ---------------- 渲染 ----------------
def fmt_dur(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec}s"


def render_state():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("L", (vp.EPD_W, vp.EPD_H), 255)
    d = ImageDraw.Draw(img)
    f_big = vp.find_font(size_bold=22)
    f_text = vp.find_font(size=13)
    f_small = vp.find_font(size=11)

    # ---- 顶部横幅 ----
    # 特殊事件: 某个 agent 需要审批 / 运行失败 (反色突出 + agent 名字)
    # 正常: AGENTS ×N 运行中 / AGENTS 0; 右侧秒级刷新时间戳 (快刷无闪烁,
    # 时间戳每推必变, 肉眼可确认屏幕活着)
    banner = None
    for a in hub.agents.values():
        if a.state == "waiting":
            banner = f"{a.name} 需要审批"
            break
        if a.state == "error":
            banner = f"{a.name} 运行失败"
            break
    n_active = sum(1 for a in hub.agents.values() if a.state in ACTIVE_STATES)
    clock = time.strftime("%H:%M:%S")
    if banner:
        d.rectangle([0, 0, vp.EPD_W, 34], fill=0, outline=0)
        d.rectangle([2, 2, vp.EPD_W - 3, 32], outline=255, width=2)
        d.text((8, 6), banner, font=f_big, fill=255)
        tw = d.textlength(clock, font=f_small)
        d.text((vp.EPD_W - 8 - tw, 10), clock, font=f_small, fill=255)
    else:
        d.rectangle([0, 0, vp.EPD_W, 34], fill=0)
        d.text((6, 6), f"AGENTS ×{n_active} 运行中" if n_active else "AGENTS 0",
               font=f_big, fill=255)
        tw = d.textlength(clock, font=f_small)
        d.text((vp.EPD_W - 8 - tw, 10), clock, font=f_small, fill=255)

    # ---- Agent 列表 (核心区, 直到距底部 22px; 不显示具体执行内容) ----
    y = 42
    agents = list(hub.agents.values())[:4]
    if not agents:
        d.text((6, y), "没有正在运行的 agent", font=f_text, fill=0)
        d.text((6, y + 18), "在 Claude/Qoder/WorkBuddy 里开工即自动显示", font=f_small, fill=0)
    for a in agents:
        dur = fmt_dur(time.time() - a.started)
        mark = "!" if a.state in ("waiting", "error") else (">" if a.state in ACTIVE_STATES else "*")
        d.text((6, y), f"{mark} {a.name}", font=f_text, fill=0)
        st = STATE_TEXT.get(a.state, a.state)
        tw = d.textlength(st, font=f_text)
        d.text(((vp.EPD_W - tw) // 2, y), st, font=f_text, fill=0)
        tw = d.textlength(dur, font=f_small)
        d.text((vp.EPD_W - 6 - tw, y + 1), dur, font=f_small, fill=0)
        y += 24

    # ---- 底部: 已运行时间 (右对齐) ----
    uptime = fmt_dur(time.time() - hub.started)
    d.line([(0, vp.EPD_H - 22), (vp.EPD_W, vp.EPD_H - 22)], fill=0, width=1)
    d.text((6, vp.EPD_H - 18), "vibedot", font=f_small, fill=0)
    tw = d.textlength(f"已运行 {uptime}", font=f_small)
    d.text((vp.EPD_W - 6 - tw, vp.EPD_H - 18), f"已运行 {uptime}", font=f_small, fill=0)
    return img


_pending_push_full = False   # 推送排队: pushing 期间的全刷请求完成后补推
_pending_push_any = False    # 推送排队: pushing 期间的普通推送请求完成后补推 (点击不丢)
_persist_ser = None         # 常驻串口: 反复开关 COM3 会触发 DTR/RTS 跳变复位 ESP32
                            # (CDC 重枚举 -> 写超时), 打开一次常驻复用
_ser_keep = True            # 串口常驻守护开关 (烧录期间置 False, 避免与 esptool 抢端口)
_ser_ready_at = 0.0         # 串口就绪时刻: 设备唤醒后 esp_restart+自检 ~15s,
                            # 过早写会撞半死窗 (超时/error 22); 收到设备横幅
                            # ([VIBEDOT] BLE advertising) 即提前就绪
_ser_poison_until = 0.0     # 串口冷却期: 写 IRP 挂起后一段时间内跳过串口直走蓝牙
_ser_io_lock = threading.Lock()   # 串口 IO 互斥: keeper / 推送线程共享句柄,
                                  # 挂起的写不与新 IO 并发 (pyserial 非线程安全)


def _get_serial():
    """获取常驻串口 (失败/设备断开返回 None); 常驻复用, 不 close。
    关键: dtr/rts 保持低电平——板载自动复位电路由 DTR/RTS 上升沿触发,
    pyserial open 默认拉高两者会复位 ESP32 (CDC 重枚举 -> 写失败 ERROR_NOT_SUPPORTED,
    且复位同时杀掉已建立的 BLE 连接)"""
    global _persist_ser
    import serial
    if not _ser_keep:                    # 烧录窗口: 不与 esptool 抢端口
        return None
    if _persist_ser is not None and _persist_ser.is_open:
        return _persist_ser
    port = None
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.vid == 0x303A:          # ESP32 原生 USB
                port = p.device
                break
    except Exception:
        return None
    if port is None:
        return None                      # 端口未连接 -> 立刻回退蓝牙
    try:
        _persist_ser = serial.Serial(None, 115200, timeout=0.5, write_timeout=3)
        _persist_ser.port = port
        _persist_ser.dtr = False         # 不拉高 DTR/RTS, 不产生复位脉冲
        _persist_ser.rts = False
        _persist_ser.open()
        global _ser_ready_at
        _ser_ready_at = time.time() + 30   # 观察期: 收到设备横幅即提前就绪
        print(f"[ser] open {port} handle={_persist_ser._port_handle} "
              f"t={time.strftime('%H:%M:%S')}")
        time.sleep(0.3)                  # 打开后稍候 (设备侧 CDC 就绪)
        return _persist_ser
    except Exception as e:
        print(f"[push] 串口打开失败 {port}:", e)
        _persist_ser = None
        return None


def _release_serial(force=False):
    """释放常驻串口。
    ESP32-C3 的 USB-Serial-JTAG 外设在主机关闭活句柄时会复位芯片, 复位后
    USB 不再枚举 (应用照跑但 USB 死) 直到下一次深睡循环——写失败后盲目
    close 会形成 "写失败->close->复位->USB死->睡眠" 恶性循环。
    因此默认仅当端口确认消失 (设备已睡/拔出, list_ports 找不到 ESP32)
    才 close; 此时句柄已死, close 安全。force=True (烧录前/主动恢复)
    无条件 close。"""
    global _persist_ser
    if _persist_ser is None:
        return
    if not force:
        try:
            from serial.tools import list_ports
            if any(p.vid == 0x303A for p in list_ports.comports()):
                print("[ser] 端口仍在, 保持句柄不 close (close 会复位芯片)")
                return
        except Exception:
            pass
    try:
        _persist_ser.close()
    except Exception:
        pass
    _persist_ser = None


def _is_dead_handle_err(e):
    """判断异常是否表示句柄已死 (设备重枚举/拔出后旧句柄失效):
    死句柄 close 安全 (不会复位芯片); 活句柄 (超时/半死) 绝不能 close"""
    if isinstance(e, (PermissionError, OSError)):
        w = getattr(e, "winerror", None)
        if w in (22, 6, 1167, 433, 995):
            # ERROR_BAD_COMMAND / INVALID_HANDLE / DEVICE_NOT_CONNECTED
            # / ERROR_NO_SYSTEM_RESOURCES? / ERROR_OPERATION_ABORTED
            return True
    return False


def _port_gone():
    """ESP32 串口是否已从系统消失 (写超时后判断句柄死活的旁证)"""
    try:
        from serial.tools import list_ports
        return not any(p.vid == 0x303A for p in list_ports.comports())
    except Exception:
        return False


def _serial_keeper_tick(heartbeat: bool):
    """一次守护 IO (在线程池执行, 绝不在事件循环里直接碰串口):
    排空 RX + 周期心跳 0x00。usbser 写 IRP 挂起时本函数卡在线程里,
    事件循环照常运行; 下一轮 tick 因 _ser_io_lock 被占而自动跳过。"""
    global _ser_ready_at
    ser = _get_serial()
    if ser is None:
        return
    if not _ser_io_lock.acquire(blocking=False):
        return                      # 上一轮 IO 仍挂起: 跳过本轮
    try:
        n = ser.in_waiting
        if n:
            txt = ser.read(n).decode("utf-8", errors="replace")
            for line in txt.splitlines():
                line = line.strip()
                if line:
                    print("[dev]", line)
            if ("BLE advertising" in txt or "[PWR]" in txt
                    or "[SER] always-on" in txt):
                # setup 完成/设备确认常开: 数据通路已通, 串口立即可推
                _ser_ready_at = time.time() + 1
                print("[ser] 设备就绪")
        if heartbeat:
            ser.write(b"\x00")     # ~6s 心跳, 保持设备端 serLastRxMs 新鲜
    except Exception as e:
        print("[ser] keeper 异常:", e)
        # 句柄已死 (设备重枚举/睡) -> close 安全; 活句柄 (超时) 保守不 close
        _release_serial(force=_is_dead_handle_err(e))
    finally:
        _ser_io_lock.release()


async def _serial_keeper():
    """串口常驻守护: 服务器存活期间始终持有 COM 口打开句柄。
    持句柄 -> USB 链路活跃 (SOF 持续) -> 设备端 Serial.isPlugged() 为真
    -> 固件保持常开不入睡; 无句柄时 Windows 会挂起 USB 链路, 设备端
    误判"未插线"而进入深睡循环 (COM3 消失/写超时的根源)。
    同时周期性排空 RX 缓冲 (只读不排空会让设备端 TX 反压卡住 loop),
    每 ~6s 写心跳 0x00: 设备以串口数据活动判定 USB 存活。
    所有阻塞 IO 经线程池执行且带超时: 单次 IO 挂起不会冻结事件循环
    (此前 ser.write 在事件循环内直接调用, usbser 挂起时整个服务器假死)。"""
    tick = 0
    loop = asyncio.get_running_loop()
    while True:
        if _ser_keep and not hub.pushing and time.time() >= _ser_poison_until:
            tick += 1
            heartbeat = tick >= 3
            if heartbeat:
                tick = 0
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, _serial_keeper_tick, heartbeat),
                    timeout=8)
            except asyncio.TimeoutError:
                print("[ser] keeper IO 超时 (写 IRP 挂起?), 本轮跳过")
            except Exception as e:
                print("[ser] keeper 调度异常:", e)
        await asyncio.sleep(2)


def _push_serial_sync(data, fast=False):
    """USB 串口直推 (在线程中运行, 阻塞 IO); 无 ESP32 端口/失败返回 False
    (调用方立刻回退蓝牙)——端口检测用 VID 0x303A, 与 /api/ports 一致;
    设备唤醒后 esp_restart+自检 ~15s (USB 半死窗), 就绪前直接回退蓝牙"""
    global _ser_poison_until
    if time.time() < _ser_poison_until:
        return False                 # 写 IRP 挂起冷却期: 直接走蓝牙
    if not _ser_io_lock.acquire(timeout=3):
        print("[push] 串口 IO 忙 (先前写挂起未返回), 回退蓝牙")
        return False
    try:
        for attempt in range(2):
            ser = _get_serial()
            if ser is None:
                return False                     # 端口未连接 -> 立刻回退蓝牙
            if time.time() < _ser_ready_at:
                # 就绪检查必须在 _get_serial 之后: 新打开的句柄有自己的 30s
                # 观察期 (设备半死窗), 先查后开会把写入打进未就绪窗口 ->
                # 超时 -> 误关句柄 -> 复位芯片的恶性循环
                print(f"[push] 串口未就绪 (设备唤醒自检中, {_ser_ready_at - time.time():.0f}s 后可用), 回退蓝牙")
                return False
            try:
                ser.reset_input_buffer()
                print(f"[ser] write 0x07 handle={ser._port_handle} "
                      f"t={time.strftime('%H:%M:%S')}")
                ser.write(b"\x07")               # 常开: 设备保持清醒, 串口推送长期可用
                ser.write(b"\x00")               # 复位帧接收计数
                for i in range(0, len(data), 128):   # 128B/块: ESP32 串口 RX 缓冲 256B
                    ser.write(b"\x02" + len(data[i:i + 128]).to_bytes(2, "little")
                              + data[i:i + 128])
                    time.sleep(0.01)             # 消化间隙 (115200 下 128B ≈ 11ms)
                ser.write(b"\x03" if not fast else b"\x04")
                # 等固件刷屏完成日志 ([EPD] FULL/FAST refresh ...)
                deadline = time.time() + 18
                buf = b""
                while time.time() < deadline:
                    try:
                        chunk = ser.read(256)
                    except Exception:
                        break
                    if chunk:
                        buf += chunk
                        if b"refresh " in buf:
                            print(f"[push] 串口刷屏完成 ({ser.port}, {'快' if fast else '全'})")
                            return True
                print(f"[push] 串口未确认刷屏完成(第{attempt + 1}轮), 回退蓝牙")
                return False
            except Exception as e:
                print(f"[push] 串口推送异常(第{attempt + 1}轮):", e)
                # 句柄已死 (设备重枚举/拔出, winerror 22/6 等) -> close 安全重开;
                # 活句柄 (写超时/半死) 绝不能 close——close 活句柄会复位芯片,
                # 且 list_ports 在 USB 枚举抖动时不可靠 ("端口消失"可能误报),
                # 一旦据此强关就形成 每轮推送->复位->重枚举 的重启死循环
                _release_serial(force=_is_dead_handle_err(e))
                time.sleep(1.0)                  # 覆盖唤醒交接窗 1-3s, 提高第 2 轮命中率
        return False
    finally:
        _ser_io_lock.release()


async def do_push(force_full=False):
    """刷屏: 优先 USB 串口直推 (快且不受 BLE 栈影响), 端口未连接/未就绪/
    写挂起/任何异常 都立刻回退蓝牙——串口失败绝不终止推送。
    返回 'ok' | 'fail' | 'queued' (pushing 期间的请求排队, 完成后补推)"""
    global _pending_push_full, _pending_push_any, _ser_poison_until
    if hub.pushing:
        # 排队而非丢弃: 全刷排全刷, 普通推送排普通补推 (点击不丢)
        if force_full:
            _pending_push_full = True
        else:
            _pending_push_any = True
        return "queued"
    hub.pushing = True
    t0 = time.time()
    path = "?"
    ok = False
    try:
        img = render_state()
        img.save(PREVIEW_PNG)
        data = vp.image_to_1bpp_bytes(img)
        hub.push_count += 1
        fast = not force_full and hub.push_count % FULL_REFRESH_EVERY != 0
        # 1) 串口直推 (阻塞 IO 放线程池)。任何结果 (含超时/异常) 都继续蓝牙回退,
        #    绝不提前中断——此前串口超时直接落入外层 except, 蓝牙永远不会被尝试
        try:
            ok = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None, _push_serial_sync, data, fast),
                timeout=SERIAL_PUSH_TIMEOUT)
        except asyncio.TimeoutError:
            # usbser 写 IRP 挂起 (close/复位都救不了, 需设备节点重启):
            # 冷却串口 2 分钟直接走蓝牙, 避免每轮推送都白等 25s
            _ser_poison_until = time.time() + 120
            print(f"[push] 串口推送超时 (写 IRP 挂起?), 冷却 120s, 立即回退蓝牙")
        except Exception as e:
            print("[push] 串口推送异常 (回退蓝牙):", e)
        path = "串口" if ok else "蓝牙"
        if not ok:
            # 2) 蓝牙推送: 互斥锁与连接守护串行, 避免并发扫描互相取消 (0x800704C7)。
            #    等锁也计入超时——守护任务持锁扫描挂起时, 推送最多等 BLE_PUSH_TIMEOUT
            #    而不是永久卡死 (卡死时 pushing=True 会吞掉所有后续推送/点击)
            async def _ble_push():
                async with _ble_lock:
                    return await push_cached(data, fast=fast)
            try:
                ok = await asyncio.wait_for(_ble_push(), timeout=BLE_PUSH_TIMEOUT)
            except asyncio.TimeoutError:
                # 常见: 常驻连接上 write 永久挂起 (WinRT)。丢弃连接, 下轮重建,
                # 否则坏连接被反复复用 -> 表现为"蓝牙彻底失效"
                _drop_client()
                print("[push] 蓝牙推送超时 (已丢弃常驻连接, 下轮重建)")
            except Exception as e:
                print("[push] 蓝牙推送异常:", e)
                _drop_client()
        if ok:
            hub.last_push = time.time()
            hub.push_eligible_at = None
            hub.push_fail_streak = 0
        else:
            hub.push_fail_streak += 1
        hub.last_push_result = {"ok": bool(ok), "path": path,
                                "dur": round(time.time() - t0, 1),
                                "ts": time.time()}
        print(f"[push] {'成功' if ok else '失败'} 耗时 {time.time() - t0:.1f}s ({path})")
    except Exception as e:
        print("[push] error:", e)
    finally:
        hub.pushing = False
        if _pending_push_full or _pending_push_any:
            full = _pending_push_full
            _pending_push_full = False
            _pending_push_any = False
            asyncio.create_task(do_push(force_full=full))
    return "ok" if ok else "fail"


_persist_client = None       # 常驻 BLE 连接: 推送复用免重连 (延迟 ~2s 而非 ~8s)
_persist_device = None       # 扫描到的 BLEDevice 对象: WinRT 缓存异常时用对象连接绕过地址查找
_last_bthserv_restart = 0.0  # 蓝牙服务重启节流 (WinRT 缓存损坏时的自动恢复)


def _restart_bthserv():
    """WinRT 缓存污染 (怪地址/0000None/0x800704C7 已取消) 根治三步:
    1) pnputil 解除系统配对——配对态 Windows 会自动重连 VibeDot 抢占唯一
       BLE 连接 (写被取消) 并向缓存写脏服务数据; bleak 连接无需配对,
       解除后固件正常工作; 2) 删 BTHPORT 残留键; 3) 重启蓝牙服务清缓存。
    服务器进程为管理员时有效, 失败则依赖扫描重试慢慢恢复"""
    global _last_bthserv_restart
    now = time.time()
    if now - _last_bthserv_restart < 300:      # 5 分钟最多一次
        return False
    _last_bthserv_restart = now
    try:
        r = subprocess.run(
            ["pnputil", "/remove-device",
             r"BTHLE\DEV_7CE8B17A3DC2\7&17003C4D&0&7CE8B17A3DC2"],
            timeout=30, capture_output=True)
        out = (r.stdout or b"") + (r.stderr or b"")
        ok = r.returncode == 0
        print("[ble] 系统配对已解除" if ok
              else "[ble] 解除配对未生效 (可能已无配对): "
                   + out.decode("gbk", errors="replace").strip()[-80:])
    except Exception as e:
        print("[ble] 解除配对异常:", e)
    try:
        subprocess.run(
            ["reg", "delete",
             r"HKLM\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices\7ce8b17a3dc2",
             "/f"], timeout=15, capture_output=True)
    except Exception:
        pass
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Restart-Service bthserv -Force"],
            timeout=30, capture_output=True)
        print("[ble] 蓝牙服务已重启 (WinRT 缓存清空)")
        return True
    except Exception as e:
        print("[ble] 蓝牙服务重启失败:", e)
        return False


def _valid_mac(a):
    """校验合法 MAC (xx:xx:xx:xx:xx:xx); bleak 偶尔返回 '0000None...' 怪串"""
    try:
        return bool(a) and len(str(a)) == 17 and int(str(a).replace(':', ''), 16) >= 0
    except Exception:
        return False


def _drop_client():
    global _persist_client
    _persist_client = None


async def _ensure_client():
    """获取常驻连接 (须在 _ble_lock 内调用); 断线自动重建;
    设备不在线返回 None, 调用方走扫描回退"""
    global _persist_client, _persist_device
    import bleak
    if _persist_client and _persist_client.is_connected:
        return _persist_client
    # 优先用扫描对象连接 (WinRT 按地址查找偶发 not found 时对象仍可连)
    if _persist_device is not None:
        try:
            # use_cached_services=False: 每次连接强制全新服务发现 (UNCACHED),
            # 绕开被污染的 WinRT GATT 缓存 (0000None 解析错误); 代价 ~1s
            c = bleak.BleakClient(_persist_device, timeout=20.0,
                                  use_cached_services=False)
            await c.connect()
            _persist_client = c
            return c
        except Exception as e:
            print("[ble] 对象连接失败:", e)
            _persist_device = None
    if not _valid_mac(hub.last_addr):
        hub.last_addr = None
        return None
    try:
        c = bleak.BleakClient(hub.last_addr, timeout=20.0,
                              use_cached_services=False)
        await c.connect()
        _persist_client = c
        return c
    except Exception as e:
        print("[ble] 常驻连接建立失败:", e)
        _persist_client = None
        return None


async def push_cached(data, fast=False):
    """常驻连接直推 (免重连, ~2s); 断线重试一次后回退扫描
    (设备深度睡眠循环下重试等到广播窗口)"""
    import bleak
    if hub.last_addr and not _valid_mac(hub.last_addr):
        print(f"[push] 清洗怪地址: {hub.last_addr!r}")
        hub.last_addr = None
    if hub.last_addr:
        for attempt in range(2):
            client = await _ensure_client()
            if client:
                try:
                    await _write_frame(client, data, fast=fast)
                    return True
                except Exception as e:
                    err = str(e)
                    print(f"[push] 直推失败(第{attempt + 1}轮):", e)
                    _drop_client()
                    if "invalid literal" in err or "was not found" in err:
                        # WinRT 设备缓存损坏: 重启蓝牙服务清缓存后重试。
                        # 服务重启+缓存刷新需数秒, 等 8s 再试 (3s 会撞上
                        # 服务未就绪, 白白多烧一轮 20s 连接超时)
                        if _restart_bthserv():
                            await asyncio.sleep(8)
                        else:
                            await asyncio.sleep(1)
                    else:
                        await asyncio.sleep(1)
            else:
                # WinRT 设备缓存损坏(怪地址) 或连接被拒: 扫描重建对象
                break
        hub.last_addr = None
    # 回退: 扫描 (设备可能睡着, 55s 睡 / 20s 广播, 最长约 75s 醒来)
    global _persist_device
    tgt = None
    for _ in range(3):
        try:
            devices = await bleak.BleakScanner.discover(timeout=30.0)
        except Exception as e:
            # 设备复位瞬间广播包是脏的 (0000None 解析失败): 等下轮干净广播,
            # 不让单次解析失败报废整轮推送
            print("[push] 扫描解析失败 (设备复位中?), 2s 后重扫:", e)
            await asyncio.sleep(2)
            continue
        tgt = next((d for d in devices
                    if d.name == "VibeDot" and _valid_mac(d.address)), None)
        if tgt:
            break
        await asyncio.sleep(2)
    if tgt is None:
        print("[push] VibeDot 不在线 (深度睡眠循环中?)")
        return False
    hub.last_addr = tgt.address
    _persist_device = tgt
    client = await _ensure_client()
    if client is None:
        return False
    try:
        # 必须兜异常: WinRT 缓存损坏时 _write_frame 内 services 解析会抛
        # invalid literal —— 此前未捕获直接炸出 push_cached, 整轮推送报废
        await _write_frame(client, data, fast=fast)
        return True
    except Exception as e:
        print("[push] 扫描路径推送失败:", e)
        _drop_client()
        return False


async def _write_frame(client, data, fast=False):
    import bleak
    mtu = 247
    try:
        mtu = await client.exchange_mtu(517)
        print(f"[ble] MTU={mtu}")
    except Exception:
        pass
    rx = client.services.get_characteristic(vp.CHAR_RX_UUID)
    status = client.services.get_characteristic(vp.CHAR_STATUS_UUID)
    refresh_cmd = b"\x04" if fast else b"\x03"
    for attempt in range(3):
        await client.write_gatt_char(rx, b"\x00", response=True)   # 复位计数
        # 有响应写 (可靠流控): Windows 栈 WRITE_NR 会静默丢弃, 不可用;
        # 固件 status 只在刷屏分支更新, 中途读 rx 计数无效, 直接写满整帧
        for i in range(0, len(data), mtu - 4):
            await client.write_gatt_char(rx, b"\x02" + data[i:i + mtu - 4], response=True)
        await client.write_gatt_char(rx, refresh_cmd, response=True)
        for _ in range(30):
            await asyncio.sleep(0.5)
            try:
                st = await client.read_gatt_char(status)
            except Exception:
                continue
            if st and st[0] == 1:
                # 保持常开: 每次连上都写 0x07, 设备不会 20s 后又入睡
                # (电脑开机自动重连后也无需碰设备/串口)
                try:
                    await client.write_gatt_char(rx, b"\x07", response=True)
                except Exception:
                    pass
                print(f"[push] 已刷新 ({'快' if fast else '全'}, 第 {attempt + 1} 轮)")
                return
            if st and st[0] == 0:
                break
    print("[push] 状态未确认")


_last_tick_push = 0.0
_last_hold_attempt = 0.0
_last_hold_ok = True
_hold_task = None
_ble_lock = asyncio.Lock()   # BLE 操作互斥: 推送/守护/扫描串行, 避免 Windows 栈冲突


async def _auto_hold():
    """连接守护: 复用/建立常驻连接写 0x07 常开; 设备在睡眠循环时
    扫描等到广播窗口重连; 成功后每 5 分钟 keepalive 一次(设备断连 10 分钟才入睡,
    常驻连接存在时设备根本不睡, 电脑开机期间设备永不睡); 失败则 90s 快重试。
    推送进行中不启动 (避免抢 _ble_lock 把推送堵在门外); 每次扫描带硬超时,
    WinRT 挂起时及时放弃本轮而不是无限持锁 (否则推送会被永久阻塞)。"""
    global _last_hold_attempt, _last_hold_ok, _hold_task
    import bleak
    try:
        if hub.pushing:
            return                      # 推送优先, 让出 BLE 通道
        async with _ble_lock:
            if hub.pushing:             # 等锁期间推送可能已开始
                return
            client = await _ensure_client()
            if client is None:
                # 扫描等广播窗口 (55s 睡 / 20s 广播)
                tgt = None
                for _ in range(3):
                    try:
                        devices = await asyncio.wait_for(
                            bleak.BleakScanner.discover(timeout=30.0, return_adv=True),
                            timeout=35)
                    except asyncio.TimeoutError:
                        print("[hold] 扫描挂起 (WinRT), 放弃本轮")
                        break
                    except Exception as e:
                        # 设备复位瞬间广播包脏 (0000None): 跳过本轮等干净广播,
                        # 不触发重量级的蓝牙服务重启
                        print("[hold] 扫描解析失败 (设备复位中?), 2s 后重扫:", e)
                        await asyncio.sleep(2)
                        continue
                    tgt = next((d for d, adv in devices.values()
                                if _valid_mac(d.address) and (d.name == "VibeDot"
                                    or any(str(vp.SERVICE_UUID).lower() in str(u).lower()
                                           for u in (adv.service_uuids or [])))), None)
                    if tgt:
                        hub.last_addr = str(tgt.address)
                        global _persist_device
                        _persist_device = tgt
                        print(f"[hold] 扫描发现 {hub.last_addr}")
                        break
                    await asyncio.sleep(1)
                client = await _ensure_client()
            if client:
                rx = client.services.get_characteristic(vp.CHAR_RX_UUID)
                await client.write_gatt_char(rx, b"\x07", response=True)
                _last_hold_ok = True
                hub.push_fail_streak = 0      # 设备回连: 恢复正常推送节拍
                print(f"[hold] 设备常开保持 {hub.last_addr}")
            else:
                _last_hold_ok = False
                print("[hold] 本次未发现设备 (睡眠循环中, 下次再试)")
    except Exception as e:
        _last_hold_ok = False
        print("[hold] 连接守护失败:", e)
        _drop_client()
        err = str(e)
        if "invalid literal" in err or "was not found" in err:
            # WinRT 设备缓存损坏: 重启蓝牙服务清缓存 (怪地址/not found)
            _restart_bthserv()
            await asyncio.sleep(3)
    finally:
        _last_hold_attempt = time.time()
        _hold_task = None


async def scheduler():
    """后台调度: 到期刷屏 + 周期重刷(更新已运行时间) + 空闲回收 + 自动重连守护。
    节拍规则: 只要有 agent 活跃 (hook 检测到 + 后台有信息) 每 3s 刷一次,
    直到全部任务结束转懒加载; 事件到期推送失败时 3s 后自动重试而非放弃。"""
    global _last_tick_push, _hold_task
    while True:
        await asyncio.sleep(1)
        hub.reap_idle()
        due_event = hub.push_eligible_at and time.time() >= hub.push_eligible_at
        # 离线退避: 连续失败时把 tick 间隔指数拉开 (3->6->12->24->30s 封顶),
        # 避免设备离线期间 3s 一轮空转 (每轮 80-100s 失败周期, pushing 常真
        # 导致"推送中·排队"徽标常亮、点击排队排不到); 设备回连 (hold 成功)
        # 即清零恢复正常节拍
        interval = TICK_INTERVAL if hub.push_fail_streak == 0 else \
            min(30, TICK_INTERVAL * (2 ** min(hub.push_fail_streak, 4)))
        due_tick = (TICK_INTERVAL > 0 and hub.any_active()
                    and time.time() - max(_last_tick_push, hub.last_push) >= interval)
        # 空闲心跳: 无活跃 agent 时低频刷新 (时钟走字/状态不过夜)
        due_idle = (IDLE_HEARTBEAT > 0 and not hub.any_active()
                    and time.time() - max(_last_tick_push, hub.last_push) >= IDLE_HEARTBEAT)
        if due_event or due_tick or due_idle:
            r = await do_push()
            # 只有真正执行了推送才推进节拍; busy/queued 秒回时不推进,
            # 否则当前推送一结束又要多等 3s (节拍被顺延的根源)
            if r in ("ok", "fail"):
                _last_tick_push = time.time()
            if r == "fail" and hub.push_eligible_at:
                hub.push_eligible_at = time.time() + interval  # 失败按退避间隔重试
        # 连接守护: 断连/失败 25s 快重试 (设备 v5 常开持续广播, 连接即回);
        # 已连接 60s keepalive 写 0x07 续常开; 推送进行中不启动 (守护持锁
        # 扫描会堵住推送的蓝牙回退)
        if _hold_task is None and not hub.pushing and time.time() - _last_hold_attempt > (
                25 if (hub.last_addr is None or not _last_hold_ok) else 60):
            _hold_task = asyncio.create_task(_auto_hold())


@app.on_event("startup")
async def _start():
    # 启动懒加载首刷: 服务器起来 ~8s 后 (等串口/扫描就绪) 先上屏一帧,
    # 不必等第一个 hook 事件才有画面
    hub.push_eligible_at = time.time() + 8
    asyncio.create_task(scheduler())
    asyncio.create_task(_serial_keeper())
    asyncio.create_task(_minimax_watcher())


# ---------------- MiniMax Code 会话文件监视器 ----------------
# MiniMax Code 桌面版无原生 hooks (Electron 闭源, config.yaml 仅模型配置),
# 但完整会话流实时落盘: ~/.minimax/v2/sessions/**/messages.jsonl
#   每行 {"message_id","turn_id","message":{"role","content":[...]}}
# watcher tail 这些文件 -> 归一化上报状态机 (src=minimax), hook 等价物
MINIMAX_SESS = os.path.expanduser("~/.minimax/v2/sessions")
_mm_offsets = {}        # messages.jsonl 绝对路径 -> 已读偏移
_mm_sid_cache = {}      # 会话目录 -> sessionId (manifest.json)


def _mm_session_id(sess_dir):
    if sess_dir not in _mm_sid_cache:
        try:
            m = json.load(open(os.path.join(sess_dir, "manifest.json"),
                               encoding="utf-8"))
            _mm_sid_cache[sess_dir] = m.get("sessionId") or os.path.basename(sess_dir)
        except Exception:
            _mm_sid_cache[sess_dir] = "minimax-" + os.path.basename(sess_dir)[-12:]
    return _mm_sid_cache[sess_dir]


def _mm_classify(line):
    """一行 messages.jsonl -> (state, summary) 或 None"""
    try:
        obj = json.loads(line)
    except Exception:
        return None
    msg = obj.get("message") or {}
    role = msg.get("role", "")
    if role not in ("user", "assistant"):
        return None
    text = ""
    for part in (msg.get("content") or []):
        if isinstance(part, dict) and part.get("type") == "text":
            text = (part.get("text") or "").strip()
            break
    if role == "user":
        # 系统注入的上下文 (system-reminder 等) 不算用户活动
        if text.startswith("<system-reminder>") or text.startswith("<"):
            return None
        return "thinking", text[:40]
    # assistant 消息 = 模型在产出 (编码/回答中)
    return "coding", text[:40]


async def _minimax_watcher():
    """每 2s 扫描 MiniMax 会话目录, tail 最近活跃的 messages.jsonl。
    目录不存在 (未安装 MiniMax Code) 时静默待机。"""
    import glob as _glob
    while True:
        try:
            files = _glob.glob(os.path.join(
                MINIMAX_SESS, "**", "messages.jsonl"), recursive=True)
            if files:
                # 只跟踪最近 1 小时内有写入的会话文件 (活跃窗口)
                now = time.time()
                files = [f for f in files
                         if now - os.path.getmtime(f) < 3600]
                files.sort(key=os.path.getmtime, reverse=True)
                for path in files[:8]:
                    off = _mm_offsets.get(path, 0)
                    try:
                        sz = os.path.getsize(path)
                        if sz < off:            # 文件被截断/轮转
                            off = 0
                        if sz == off:
                            continue
                        with open(path, encoding="utf-8", errors="ignore") as fh:
                            fh.seek(off)
                            new = fh.read()
                            _mm_offsets[path] = fh.tell()
                    except Exception:
                        continue
                    # MiniMax Code 是多 agent 架构 (mavis/worker/explore/verifier
                    # 各有独立会话文件), 按会话分会上报会把 agent 列表刷爆。
                    # 统一归并为单一 "MiniMax" agent: 一个应用在干活 = 一张卡片
                    for line in new.splitlines():
                        r = _mm_classify(line)
                        if r:
                            state, summary = r
                            hub.on_event({"type": state, "tool": "",
                                          "summary": summary, "input": {},
                                          "session_id": "minimax", "cwd": "",
                                          "src": "minimax"})
                # 回收过期 offset 防泄漏
                if len(_mm_offsets) > 64:
                    live = set(files)
                    for k in list(_mm_offsets):
                        if k not in live:
                            _mm_offsets.pop(k, None)
        except FileNotFoundError:
            pass                       # 未安装 MiniMax Code: 静默
        except Exception as e:
            print("[minimax] watcher 异常:", e)
        await asyncio.sleep(2)


# ---------------- API ----------------
@app.post("/api/event")
async def api_event(ev: Event):
    hub.on_event(ev.dict())
    return {"ok": True, "agents": len(hub.agents), "state": hub.overall()}


@app.get("/api/status")
async def api_status():
    return {
        "state": hub.overall(),
        "uptime": time.time() - hub.started,
        "agents": [a.to_dict() for a in hub.agents.values()],
        "counts": hub.counts, "events": list(hub.events)[:30],
        "project": hub.project, "online": hub.last_addr is not None,
        "last_push": hub.last_push, "push_count": hub.push_count,
        "pushing": hub.pushing,
        "pending": bool(_pending_push_any or _pending_push_full),
        "last_result": hub.last_push_result,
        "device": hub.last_addr,          # 控制台蓝牙管理栏实时显示
    }


@app.post("/api/push")
async def api_push(full: int = 0):
    """点击推送: 排队后等待真正执行完再返回——前端按钮转圈时间 = 真实推送进度,
    返回实际路径 (串口优先, 未连接立刻蓝牙) / 耗时 / 成败, 不再'点一下就没下文'"""
    r = await do_push(force_full=bool(full))
    if r == "queued":
        # 已排队: 等当前推送 + 排队补推全部落地 (上限 3 分钟, 覆盖最坏
        # 串口 25s + 蓝牙扫描回退 ~2.5min)
        deadline = time.time() + 180
        while time.time() < deadline and (hub.pushing or _pending_push_any
                                          or _pending_push_full):
            await asyncio.sleep(0.5)
    lr = hub.last_push_result or {}
    ok = bool(lr.get("ok"))
    return {"ok": ok, "queued": False,
            "result": "ok" if ok else "fail",
            "path": lr.get("path"), "dur": lr.get("dur"),
            "preview": "/preview.png"}


@app.get("/preview.png")
async def api_preview():
    if os.path.exists(PREVIEW_PNG):
        return FileResponse(PREVIEW_PNG, media_type="image/png")
    return JSONResponse({"error": "no preview"}, status_code=404)


# ---------------- hook / 接入 一键安装 ----------------
# Claude Code / Qoder / WorkBuddy 的 hooks 配置完全同构 (Claude Code 风格):
#   {"hooks": {"事件名": [{"matcher":..., "hooks": [{"type":"command","command":...}]}]}}
# 来源:
#   Claude Code: ~/.claude/settings.json (官方文档)
#   Qoder CN:   ~/.qoder/settings.json + ~/.qoder-cn/settings.json (阿里云帮助文档)
#   WorkBuddy:  ~/.workbuddy/settings.json (社区实测, 与 Claude Code 同构)
#   Codex:      ~/.codex/config.toml notify (回合事件)
HOOK_EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
               "Notification", "PermissionRequest", "Stop", "SessionStart", "SessionEnd"]


def _hook_command(src: str):
    py = sys.executable.replace("\\", "/")
    script = os.path.join(BASE, "hook_event.py").replace("\\", "/")
    return f'"{py}" "{script}" {src}'


def _install_hooks_json(path: str, src: str):
    """向任意 Claude Code 同构 settings.json 写入 hooks (幂等, 保留已有配置)"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    settings = {}
    if os.path.exists(path):
        try:
            settings = json.load(open(path, encoding="utf-8"))
        except Exception:
            return False, f"无法解析已有配置 {path}"
        shutil.copy2(path, path + ".bak")
    cmd = {"type": "command", "command": _hook_command(src)}
    hooks = settings.setdefault("hooks", {})
    added = 0
    for evt in HOOK_EVENTS:
        entries = hooks.setdefault(evt, [])
        matcher = "*" if evt in ("PreToolUse", "PostToolUse", "PostToolUseFailure",
                                 "PermissionRequest") else ""
        if not any(h.get("command") == cmd["command"]
                   for e in entries for h in e.get("hooks", [])):
            entries.append({"matcher": matcher, "hooks": [dict(cmd)]})
            added += 1
    json.dump(settings, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return True, f"hooks 已写入 {path} (+{added})"


def _install_claude(scope: str, project_dir: str):
    """Claude Code / Claude Desktop 全局: ~/.claude/settings.json"""
    if scope == "user":
        return _install_hooks_json(os.path.expanduser("~/.claude/settings.json"), "claude")
    return _install_hooks_json(os.path.join(project_dir or hub.project, ".claude", "settings.json"), "claude")


def _install_qoder():
    """Qoder: 用户级 ~/.qoder/settings.json 与 ~/.qoder-cn/settings.json (CN 版)"""
    msgs, ok = [], True
    for d in ("~/.qoder", "~/.qoder-cn"):
        path = os.path.expanduser(os.path.join(d, "settings.json"))
        # 目录不存在且非已知配置位置时只写 ~/.qoder
        if not os.path.isdir(os.path.expanduser(d)) and d == "~/.qoder-cn":
            continue
        r = _install_hooks_json(path, "qoder")
        ok = ok and r[0]
        msgs.append(r[1])
    return ok, "; ".join(msgs)


def _install_workbuddy():
    """WorkBuddy: ~/.workbuddy/settings.json hooks"""
    return _install_hooks_json(os.path.expanduser("~/.workbuddy/settings.json"), "workbuddy")


def _install_codex():
    """Codex CLI / Codex Desktop: ~/.codex/config.toml 追加 notify (回合事件)"""
    path = os.path.expanduser("~/.codex/config.toml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    py = sys.executable.replace("\\", "/")
    script = os.path.join(BASE, "hook_event.py").replace("\\", "/")
    line = f'notify = ["{py}", "{script}", "codex"]'
    if "hook_event.py" in body:
        return True, f"Codex notify 已存在 {path}"
    if "notify" in body:
        return False, "Codex 配置已有 notify, 跳过 (避免覆盖), 请手动接入"
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    body += f"\n{line}\n"
    open(path, "w", encoding="utf-8").write(body)
    return True, f"Codex notify 已追加到 {path}"


def _install_kimi():
    """Kimi Code CLI: ~/.kimi-code/config.toml (或 ~/.kimi/config.toml) 追加
    [[hooks]] 块。Kimi 的 hook 事件与 Claude Code 同构 (stdin JSON 含
    hook_event_name/session_id/tool_name), hook_event.py kimi 直接复用。
    [[hooks]] 仅允许 event/matcher/command/timeout 四字段"""
    path = os.path.expanduser("~/.kimi-code/config.toml")
    if not os.path.isdir(os.path.expanduser("~/.kimi-code")) \
            and os.path.isdir(os.path.expanduser("~/.kimi")):
        path = os.path.expanduser("~/.kimi/config.toml")   # 旧版/社区版路径
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if "hook_event.py" in body:
        return True, f"Kimi hooks 已存在 {path}"
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    py = sys.executable.replace("\\", "/")
    script = os.path.join(BASE, "hook_event.py").replace("\\", "/")
    # TOML 单引号字面量, 内部双引号无需转义; 不写 matcher = 匹配全部
    cmd = f"'\"{py}\" \"{script}\" kimi'"
    events = ["UserPromptSubmit", "PreToolUse", "PostToolUse",
              "PostToolUseFailure", "PermissionRequest", "Notification",
              "SessionStart", "SessionEnd", "Stop"]
    body += "\n# --- vibedot hooks (自动生成, 勿改动字段) ---\n"
    for evt in events:
        body += (f'[[hooks]]\nevent = "{evt}"\n'
                 f"command = {cmd}\ntimeout = 5\n\n")
    open(path, "w", encoding="utf-8").write(body)
    return True, f"Kimi hooks 已写入 {path} (+{len(events)} 事件)"


INSTALLERS = {
    "claude_user": lambda req: _install_claude("user", req.project_dir),
    "claude_project": lambda req: _install_claude("project", req.project_dir),
    "codex_desktop": lambda req: _install_codex(),
    "workbuddy": lambda req: _install_workbuddy(),
    "qoder": lambda req: _install_qoder(),
    "kimi": lambda req: _install_kimi(),
}


class HookInstall(BaseModel):
    tool: str = ""              # INSTALLERS 键名或 all
    project_dir: str = ""


@app.post("/api/hook/install")
async def api_hook_install(req: HookInstall):
    tools = list(INSTALLERS) if req.tool in ("", "all") else [req.tool]
    results = {}
    for t in tools:
        try:
            results[t] = INSTALLERS[t](req)
        except Exception as e:
            results[t] = (False, str(e))
    return {"ok": all(ok for ok, _ in results.values()), "results": results}


@app.get("/api/hook/status")
async def api_hook_status():
    def _json_installed(path):
        if not os.path.exists(path):
            return False
        try:
            s = json.load(open(path, encoding="utf-8"))
            return any("hook_event.py" in h.get("command", "")
                       for evt in HOOK_EVENTS for e in s.get("hooks", {}).get(evt, [])
                       for h in e.get("hooks", []))
        except Exception:
            return False

    out = {
        "claude_user": {"path": os.path.expanduser("~/.claude/settings.json")},
        "claude_project": {"path": os.path.join(hub.project, ".claude", "settings.json")},
        "qoder": {"path": os.path.expanduser("~/.qoder/settings.json") + " (+~/.qoder-cn)"},
        "workbuddy": {"path": os.path.expanduser("~/.workbuddy/settings.json")},
    }
    for k, v in out.items():
        main_path = v["path"].split(" (+")[0]
        installed = _json_installed(main_path)
        if k == "qoder":     # 任一位置接入即认为已接入
            installed = installed or _json_installed(os.path.expanduser("~/.qoder-cn/settings.json"))
        v["installed"] = installed
    codex_path = os.path.expanduser("~/.codex/config.toml")
    out["codex_desktop"] = {
        "path": codex_path,
        "installed": os.path.exists(codex_path) and
                     "hook_event.py" in open(codex_path, encoding="utf-8").read(),
    }
    kimi_path = os.path.expanduser("~/.kimi-code/config.toml")
    if not os.path.exists(kimi_path) \
            and os.path.exists(os.path.expanduser("~/.kimi/config.toml")):
        kimi_path = os.path.expanduser("~/.kimi/config.toml")
    out["kimi"] = {
        "path": kimi_path,
        "installed": os.path.exists(kimi_path) and
                     "hook_event.py" in open(kimi_path, encoding="utf-8").read(),
    }
    out["minimax"] = {
        "path": MINIMAX_SESS,
        # MiniMax Code 无 hooks: 文件监视器随服务器自动运行, 装了就生效
        "installed": os.path.isdir(MINIMAX_SESS),
    }
    return out


# ---------------- 设备: 串口检测 / 一键烧录 / 蓝牙 ----------------
# 目录约定: 服务器在 <root>/vibedot/pc 下, 固件在 <root>/vibedot/vibedot, 工具在 <root>/../tools
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKETCH_DIR = os.path.join(os.path.dirname(BASE), "vibedot")
BUILD_DIR = os.path.join(ROOT, "build")
ARDUINO_CLI = os.path.join(ROOT, "tools", "arduino-cli.exe")
FQBN = "esp32:esp32:esp32c3:CDCOnBoot=cdc"


@app.get("/api/ports")
async def api_ports():
    """枚举串口, 标出 ESP32 原生 USB (VID 0x303A)"""
    try:
        from serial.tools import list_ports
        ports = [{"port": p.device, "desc": p.description,
                  "vid": f"{p.vid:04X}" if p.vid else "", "pid": f"{p.pid:04X}" if p.pid else "",
                  "esp32": p.vid == 0x303A}
                 for p in list_ports.comports()]
    except Exception as e:
        return {"ports": [], "error": str(e)}
    return {"ports": ports, "cli": os.path.exists(ARDUINO_CLI)}


@app.post("/api/usb-restart")
async def api_usb_restart():
    """强制 USB 重新枚举 (需管理员): 串口写路径卡死时恢复——
    usbser.sys 残留挂起的写 IRP, 芯片复位不会断开 USB 链路, 驱动状态不会自行恢复"""
    _release_serial(force=True)  # 用户主动恢复: 强制释放句柄再重启设备节点
    ps = (r"Get-PnpDevice -InstanceId 'USB\VID_303A*' -ErrorAction SilentlyContinue"
          r" | Where-Object { $_.Class -eq 'USB' }"
          r" | ForEach-Object { pnputil /restart-device $_.InstanceId }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           timeout=60, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        return {"ok": r.returncode == 0, "out": out[-500:]}
    except Exception as e:
        return {"ok": False, "out": str(e)}


FLASH_STATE = {"running": False, "log": [], "ok": None, "port": "", "started": 0.0}


def _flash_worker(port: str):
    global _ser_keep
    _ser_keep = False      # 烧录窗口: 守护/推送不再触碰串口
    _release_serial(force=True)  # 烧录需独占串口, 无条件释放常驻句柄
    try:
        return _flash_worker_inner(port)
    finally:
        _ser_keep = True   # 烧录结束, 恢复常驻守护


def _flash_worker_inner(port: str):
    env = dict(os.environ)
    env["ARDUINO_DIRECTORIES_DATA"] = r"E:\Tool\Arduino15"
    env["ARDUINO_DIRECTORIES_DOWNLOADS"] = r"E:\Tool\Arduino15\staging"
    env["ARDUINO_DIRECTORIES_USER"] = r"E:\Tool\ArduinoUser"
    steps = [
        ("编译", [ARDUINO_CLI, "compile", "--fqbn", FQBN, "--build-path", BUILD_DIR, SKETCH_DIR]),
        ("烧录", [ARDUINO_CLI, "upload", "-p", port, "--fqbn", FQBN,
                  "--input-dir", BUILD_DIR]),
    ]
    for name, cmd in steps:
        FLASH_STATE["log"].append(f"$ {name}: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace")
            for line in proc.stdout:
                FLASH_STATE["log"].append(line.rstrip())
            proc.wait()
            if proc.returncode != 0:
                FLASH_STATE["log"].append(f"[FAIL] {name} 退出码 {proc.returncode}")
                FLASH_STATE["ok"] = False
                break
        except Exception as e:
            FLASH_STATE["log"].append(f"[FAIL] {name}: {e}")
            FLASH_STATE["ok"] = False
            break
    else:
        FLASH_STATE["log"].append("[OK] 烧录完成, 设备已重启")
        FLASH_STATE["ok"] = True
    FLASH_STATE["running"] = False


class FlashReq(BaseModel):
    port: str = ""


@app.post("/api/flash")
async def api_flash(req: FlashReq):
    if FLASH_STATE["running"]:
        return {"ok": False, "msg": "烧录进行中"}
    if not os.path.exists(ARDUINO_CLI):
        return {"ok": False, "msg": f"未找到 arduino-cli: {ARDUINO_CLI}"}
    port = req.port
    if not port:
        try:
            from serial.tools import list_ports
            esp = next((p.device for p in list_ports.comports() if p.vid == 0x303A), None)
            port = esp or ""
        except Exception:
            port = ""
    if not port:
        return {"ok": False, "msg": "未指定串口且未检测到 ESP32 设备"}
    FLASH_STATE.update(running=True, log=[], ok=None, port=port, started=time.time())
    threading.Thread(target=_flash_worker, args=(port,), daemon=True).start()
    return {"ok": True, "msg": f"开始烧录到 {port}"}


@app.get("/api/flash/status")
async def api_flash_status():
    return {"running": FLASH_STATE["running"], "ok": FLASH_STATE["ok"],
            "port": FLASH_STATE["port"], "log": FLASH_STATE["log"][-80:]}


async def _ble_cmd(payload=None, read_status=False, scans=3):
    """控制台蓝牙命令统一入口:
    - 全局 _ble_lock 串行 (与推送/守护互斥, 避免并发撞 0x800704C7)
    - 优先复用常驻连接 (use_cached_services=False, 不吃 WinRT 脏缓存);
      无连接则扫描等广播窗口后重建 (设备深睡循环下最长 ~75s)
    - payload=None 表示只连接读状态不写
    返回 (ok, msg, addr)"""
    global _persist_device
    import bleak
    async with _ble_lock:
        client = await _ensure_client()
        if client is None:
            tgt = None
            for _ in range(scans):
                try:
                    devices = await asyncio.wait_for(
                        bleak.BleakScanner.discover(timeout=30.0, return_adv=True),
                        timeout=35)
                except Exception as e:
                    print("[ble] 控制台扫描失败 (重试):", e)
                    await asyncio.sleep(1)
                    continue
                tgt = next((d for d, adv in devices.values()
                            if _valid_mac(d.address) and (d.name == "VibeDot"
                                or any(str(vp.SERVICE_UUID).lower() in str(u).lower()
                                       for u in (adv.service_uuids or [])))), None)
                if tgt:
                    break
                await asyncio.sleep(1)
            if tgt is None:
                return False, "未发现设备广播 (设备深睡中, 最长 75s 后自动唤醒广播)", hub.last_addr
            hub.last_addr = str(tgt.address)
            _persist_device = tgt
            client = await _ensure_client()
            if client is None:
                return False, ("已发现设备但连接失败 (WinRT 缓存脏/设备弹跳, "
                               "服务器会自动清理恢复, 稍等 1-2 分钟再试)"), hub.last_addr
        try:
            extra = ""
            if payload is not None:
                rx = client.services.get_characteristic(vp.CHAR_RX_UUID)
                await client.write_gatt_char(rx, payload, response=True)
            if read_status:
                st = await client.read_gatt_char(vp.CHAR_STATUS_UUID)
                extra = f", 状态特征: {list(st)}"
            return True, f"{hub.last_addr}{extra}", hub.last_addr
        except Exception as e:
            # 弹跳后残缺 GATT 表 (特征 not found) 等: 丢弃连接重建一次再试
            _drop_client()
            try:
                client2 = await _ensure_client()
                if client2 is None:
                    return False, f"发送失败: {e}", hub.last_addr
                if payload is not None:
                    rx = client2.services.get_characteristic(vp.CHAR_RX_UUID)
                    await client2.write_gatt_char(rx, payload, response=True)
                extra = ""
                if read_status:
                    st = await client2.read_gatt_char(vp.CHAR_STATUS_UUID)
                    extra = f", 状态特征: {list(st)}"
                return True, f"{hub.last_addr}{extra} (重连后成功)", hub.last_addr
            except Exception as e2:
                _drop_client()
                return False, f"发送失败: {e2}", hub.last_addr


@app.get("/api/ble/scan")
async def api_ble_scan():
    """扫描 VibeDot 蓝牙广播 (走全局锁, 与推送/守护串行)"""
    import bleak
    try:
        async with _ble_lock:
            devices = await asyncio.wait_for(
                bleak.BleakScanner.discover(timeout=6.0, return_adv=True), timeout=10)
    except Exception as e:
        return {"found": [], "msg": f"扫描失败: {e}"}
    found = []
    for d, adv in devices.values():
        if _valid_mac(d.address) and (d.name == "VibeDot" or any(
                str(vp.SERVICE_UUID).lower() in str(u).lower()
                for u in (adv.service_uuids or []))):
            found.append({"address": str(d.address), "name": d.name or "VibeDot",
                          "rssi": adv.rssi})
    if found:
        hub.last_addr = found[0]["address"]
    return {"found": found,
            "msg": f"发现 {len(found)} 台" if found else "未发现 (设备深睡中, 最长 75s 后广播)"}


@app.post("/api/ble/test")
async def api_ble_test():
    """连接设备并读状态特征 (常驻连接复用, 不再裸连地址踩 WinRT not found)"""
    ok, msg, addr = await _ble_cmd(read_status=True)
    return {"ok": ok, "msg": f"已连接 {msg}" if ok else msg, "address": addr}


@app.post("/api/ble/sleep")
async def api_ble_sleep():
    """面板睡眠 (0x05): 屏幕断电省电, 设备保持广播;
    面板深睡后首次推送固件会强制全刷重建 VCOM"""
    ok, msg, addr = await _ble_cmd(b"\x05")
    return {"ok": ok, "msg": "面板已睡眠 (下次推送自动唤醒全刷)" if ok else msg}


@app.post("/api/ble/off")
async def api_ble_off():
    """远程关闭设备蓝牙: 写 0x06 -> 固件立即面板睡眠 + 深度睡眠循环"""
    ok, msg, addr = await _ble_cmd(b"\x06")
    if ok:
        hub.last_addr = None
        _drop_client()
        return {"ok": True, "msg": "已发送关闭指令, 设备进入低功耗睡眠 (55s 睡 / 20s 广播)"}
    return {"ok": False, "msg": msg}


@app.post("/api/ble/on")
async def api_ble_on():
    """开启蓝牙并常开: 扫描等待设备广播窗口 (睡眠循环下最长 ~75s),
    连上后写 0x07 退出睡眠循环保持持续广播, 电脑随时可搜"""
    ok, msg, addr = await _ble_cmd(b"\x07", read_status=True)
    return {"ok": ok,
            "msg": f"蓝牙已开启并保持常开 {msg}" if ok else msg,
            "address": addr}


@app.get("/api/ble/device")
async def api_ble_device():
    """当前管理的设备: 地址 / 在线状态"""
    if not _valid_mac(hub.last_addr):
        hub.last_addr = None
        return {"addr": None, "online": False, "msg": "未连接过设备, 先扫描"}
    import bleak
    try:
        async with bleak.BleakClient(hub.last_addr, timeout=6.0) as client:
            return {"addr": hub.last_addr, "online": True, "msg": "设备在线"}
    except Exception:
        return {"addr": hub.last_addr, "online": False, "msg": "设备离线 (睡眠循环中, 点\"开启蓝牙\"等待广播)"}


# ---------------- 开机自启动 ----------------
def _startup_vbs_path():
    return os.path.join(os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"),
                        "VibeDot.vbs")


def _vbs_body(port):
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    if pyw == sys.executable:
        pyw = sys.executable      # 无 pythonw 则原样 (会带控制台窗口)
    server = os.path.join(BASE, "vibedot_server.py").replace("\\", "/")
    proj = hub.project.replace("\\", "/")
    return (f'CreateObject("Wscript.Shell").Run '
            f'"""{pyw}"" ""{server}"" --project ""{proj}"" --port {port}", 0, False\n')


@app.get("/api/autostart/status")
async def api_autostart_status():
    p = _startup_vbs_path()
    installed = os.path.exists(p)
    running_port = None
    if installed:
        try:
            body = open(p).read()
            running_port = body.split("--port ")[1].split('"')[0].strip()
        except Exception:
            pass
    return {"path": p, "installed": installed, "port": running_port}


@app.post("/api/autostart/install")
async def api_autostart_install():
    try:
        p = _startup_vbs_path()
        open(p, "w", encoding="utf-8").write(_vbs_body(_current_port))
        return {"ok": True, "path": p, "msg": "已写入开机自启 (登录后静默常驻)"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@app.post("/api/autostart/uninstall")
async def api_autostart_uninstall():
    p = _startup_vbs_path()
    if os.path.exists(p):
        os.remove(p)
        return {"ok": True, "msg": "已移除开机自启"}
    return {"ok": False, "msg": "未安装"}


@app.get("/", response_class=HTMLResponse)
async def index():
    return open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8").read()


_current_port = DEFAULT_PORT


def main():
    global hub, _current_port
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.getcwd())
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    _current_port = args.port
    os.makedirs(WEB_DIR, exist_ok=True)
    hub = Hub(os.path.abspath(args.project))
    print(f"VibeDot server v2: project={hub.project}")
    print(f"Web 控制台: http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
