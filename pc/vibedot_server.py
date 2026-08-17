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
TICK_INTERVAL = 3            # 活跃期间周期重刷: 有 agent 运行每 3s 一次; 无活跃时懒加载(事件驱动)
FULL_REFRESH_EVERY = 8       # 每 N 次快刷插一次全刷清残影


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
                "qoder": "Qoder"}.get(src.lower(), src or "Agent")

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
        """回收: waiting/error 卡住 2 分钟自动解除; done/结束 60s 无新事件移除
        (避免"已结束还一直显示运行"); ACTIVE 状态 15 分钟无事件视为僵尸移除
        (agent 异常退出后 hook 不再发事件, 不能永远显示运行)"""
        now = time.time()
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
    # 正常: AGENTS ×N 运行中 / AGENTS 0
    banner = None
    for a in hub.agents.values():
        if a.state == "waiting":
            banner = f"{a.name} 需要审批"
            break
        if a.state == "error":
            banner = f"{a.name} 运行失败"
            break
    n_active = sum(1 for a in hub.agents.values() if a.state in ACTIVE_STATES)
    if banner:
        d.rectangle([0, 0, vp.EPD_W, 34], fill=0, outline=0)
        d.rectangle([2, 2, vp.EPD_W - 3, 32], outline=255, width=2)
        d.text((8, 6), banner, font=f_big, fill=255)
    else:
        d.rectangle([0, 0, vp.EPD_W, 34], fill=0)
        d.text((6, 6), f"AGENTS ×{n_active} 运行中" if n_active else "AGENTS 0",
               font=f_big, fill=255)

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


_pending_push_full = False   # 推送排队: pushing 期间的请求完成后补推


async def do_push(force_full=False):
    global _pending_push_full
    if hub.pushing:
        if force_full:
            _pending_push_full = True   # 等当前推送完成后补一次全刷
        return
    hub.pushing = True
    t0 = time.time()
    try:
        img = render_state()
        img.save(PREVIEW_PNG)
        data = vp.image_to_1bpp_bytes(img)
        hub.push_count += 1
        fast = not force_full and hub.push_count % FULL_REFRESH_EVERY != 0
        # 超时保险: Windows BLE 栈偶尔永久挂起, 不能卡死调度器;
        # 互斥锁: 与连接守护串行, 避免并发扫描互相取消 (0x800704C7)
        async with _ble_lock:
            ok = await asyncio.wait_for(push_cached(data, fast=fast), timeout=120)
        if ok:
            hub.last_push = time.time()
            hub.push_eligible_at = None
            print(f"[push] 耗时 {time.time() - t0:.1f}s")
    except asyncio.TimeoutError:
        print("[push] 超时 (BLE 挂起, 已强制中断, 下轮重试)")
    except Exception as e:
        print("[push] error:", e)
    finally:
        hub.pushing = False
        if _pending_push_full:
            _pending_push_full = False
            asyncio.create_task(do_push(force_full=True))


_persist_client = None       # 常驻 BLE 连接: 推送复用免重连 (延迟 ~2s 而非 ~8s)
_persist_device = None       # 扫描到的 BLEDevice 对象: WinRT 缓存异常时用对象连接绕过地址查找
_last_bthserv_restart = 0.0  # 蓝牙服务重启节流 (WinRT 缓存损坏时的自动恢复)


def _restart_bthserv():
    """WinRT 缓存损坏 (怪地址/not found) 时重启蓝牙服务清缓存;
    服务器进程为管理员时有效, 失败则依赖扫描重试慢慢恢复"""
    global _last_bthserv_restart
    now = time.time()
    if now - _last_bthserv_restart < 300:      # 5 分钟最多一次
        return False
    _last_bthserv_restart = now
    try:
        subprocess.run(
            ["powershell", "-Command", "Restart-Service bthserv -Force"],
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
            c = bleak.BleakClient(_persist_device, timeout=20.0)
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
        c = bleak.BleakClient(hub.last_addr, timeout=20.0)
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
                        # WinRT 设备缓存损坏: 重启蓝牙服务清缓存后重试
                        _restart_bthserv()
                        await asyncio.sleep(3)
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
        devices = await bleak.BleakScanner.discover(timeout=30.0)
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
    await _write_frame(client, data, fast=fast)
    return True


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
_hold_task = None
_ble_lock = asyncio.Lock()   # BLE 操作互斥: 推送/守护/扫描串行, 避免 Windows 栈冲突


async def _auto_hold():
    """连接守护: 复用/建立常驻连接写 0x07 常开; 设备在睡眠循环时
    扫描等到广播窗口重连; 每 5 分钟 keepalive 一次(设备断连 10 分钟才入睡,
    常驻连接存在时设备根本不睡, 电脑开机期间设备永不睡)"""
    global _last_hold_attempt, _hold_task
    import bleak
    try:
        async with _ble_lock:
            client = await _ensure_client()
            if client is None:
                # 扫描等广播窗口 (55s 睡 / 20s 广播)
                tgt = None
                for _ in range(3):
                    devices = await bleak.BleakScanner.discover(timeout=30.0, return_adv=True)
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
                print(f"[hold] 设备常开保持 {hub.last_addr}")
            else:
                print("[hold] 本次未发现设备 (睡眠循环中, 下次再试)")
    except Exception as e:
        print("[hold] 连接守护失败:", e)
        _drop_client()
    finally:
        _last_hold_attempt = time.time()
        _hold_task = None


async def scheduler():
    """后台调度: 到期刷屏 + 周期重刷(更新已运行时间) + 空闲回收 + 自动重连守护"""
    global _last_tick_push, _hold_task
    while True:
        await asyncio.sleep(1)
        hub.reap_idle()
        if hub.push_eligible_at and time.time() >= hub.push_eligible_at:
            await do_push()
            _last_tick_push = time.time()
        elif (TICK_INTERVAL > 0 and hub.any_active()
              and time.time() - max(_last_tick_push, hub.last_push) >= TICK_INTERVAL):
            await do_push()      # agent 运行中: 每 3s 快刷更新时长; 全部结束后懒加载
            _last_tick_push = time.time()
        # 连接守护: 无地址每 90s 尝试重连; 有地址每 300s keepalive 保持常开
        if _hold_task is None and time.time() - _last_hold_attempt > (
                90 if hub.last_addr is None else 300):
            _hold_task = asyncio.create_task(_auto_hold())


@app.on_event("startup")
async def _start():
    asyncio.create_task(scheduler())


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
    }


@app.post("/api/push")
async def api_push(full: int = 0):
    await do_push(force_full=bool(full))
    return {"ok": True, "preview": "/preview.png"}


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


INSTALLERS = {
    "claude_user": lambda req: _install_claude("user", req.project_dir),
    "claude_project": lambda req: _install_claude("project", req.project_dir),
    "codex_desktop": lambda req: _install_codex(),
    "workbuddy": lambda req: _install_workbuddy(),
    "qoder": lambda req: _install_qoder(),
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


FLASH_STATE = {"running": False, "log": [], "ok": None, "port": "", "started": 0.0}


def _flash_worker(port: str):
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


@app.get("/api/ble/scan")
async def api_ble_scan():
    """扫描 VibeDot 蓝牙广播"""
    import bleak
    try:
        devices = await bleak.BleakScanner.discover(timeout=6.0, return_adv=True)
        found = []
        for d, adv in devices.values():
            if _valid_mac(d.address) and (d.name == "VibeDot" or any(
                    str(vp.SERVICE_UUID).lower() in str(u).lower()
                    for u in (adv.service_uuids or []))):
                found.append({"address": str(d.address), "name": d.name or "VibeDot",
                              "rssi": adv.rssi})
        if found:
            hub.last_addr = found[0]["address"]
        return {"found": found, "msg": f"发现 {len(found)} 台" if found else "未发现 (设备可能在深度睡眠, 最长 75s 后广播)"}
    except Exception as e:
        return {"found": [], "msg": f"扫描失败: {e}"}


@app.post("/api/ble/test")
async def api_ble_test():
    """连接设备并读状态特征"""
    import bleak
    addr = hub.last_addr
    if not addr:
        r = await api_ble_scan()
        if not r["found"]:
            return {"ok": False, "msg": r["msg"]}
        addr = hub.last_addr
    try:
        async with bleak.BleakClient(addr) as client:
            st = await client.read_gatt_char(vp.CHAR_STATUS_UUID)
            return {"ok": True, "msg": f"已连接 {addr}, 状态特征: {list(st)}",
                    "address": addr}
    except Exception as e:
        hub.last_addr = None
        return {"ok": False, "msg": f"连接失败: {e}"}


@app.post("/api/ble/sleep")
async def api_ble_sleep():
    """面板睡眠 (0x05): 屏幕断电省电, 设备保持广播;
    面板深睡后首次推送固件会强制全刷重建 VCOM"""
    import bleak
    addr = hub.last_addr
    if not addr:
        r = await api_ble_scan()
        if not r["found"]:
            return {"ok": False, "msg": r["msg"]}
        addr = hub.last_addr
    try:
        async with bleak.BleakClient(addr, timeout=15.0) as client:
            rx = client.services.get_characteristic(vp.CHAR_RX_UUID)
            await client.write_gatt_char(rx, b"\x05", response=True)
        return {"ok": True, "msg": "面板已睡眠 (下次推送自动唤醒全刷)"}
    except Exception as e:
        return {"ok": False, "msg": f"发送失败: {e}"}


@app.post("/api/ble/off")
async def api_ble_off():
    """远程关闭设备蓝牙: 写 0x06 -> 固件立即面板睡眠 + 深度睡眠循环"""
    import bleak
    addr = hub.last_addr
    if not addr:
        r = await api_ble_scan()
        if not r["found"]:
            return {"ok": False, "msg": r["msg"]}
        addr = hub.last_addr
    try:
        async with bleak.BleakClient(addr) as client:
            rx = client.services.get_characteristic(vp.CHAR_RX_UUID)
            await client.write_gatt_char(rx, b"\x06", response=True)
        hub.last_addr = None
        return {"ok": True, "msg": "已发送关闭指令, 设备进入低功耗睡眠 (55s 睡 / 20s 广播)"}
    except Exception as e:
        return {"ok": False, "msg": f"发送失败: {e}"}


@app.post("/api/ble/on")
async def api_ble_on():
    """开启蓝牙并常开: 扫描等待设备广播窗口 (睡眠循环下最长 ~75s),
    连上后写 0x07 退出睡眠循环保持持续广播, 电脑随时可搜"""
    import bleak
    for i in range(4):
        devices = await bleak.BleakScanner.discover(timeout=30.0, return_adv=True)
        # return_adv=True: {addr_str: (BLEDevice, AdvertisementData)}
        tgt = next((d for d, adv in devices.values()
                    if _valid_mac(d.address) and (d.name == "VibeDot"
                        or any(str(vp.SERVICE_UUID).lower() in str(u).lower()
                               for u in (adv.service_uuids or [])))), None)
        if tgt:
            hub.last_addr = str(tgt.address)
            try:
                async with bleak.BleakClient(tgt.address, timeout=15.0) as client:
                    rx = client.services.get_characteristic(vp.CHAR_RX_UUID)
                    await client.write_gatt_char(rx, b"\x07", response=True)  # 常开模式
                    await client.read_gatt_char(vp.CHAR_STATUS_UUID)
                return {"ok": True, "msg": f"蓝牙已开启并保持常开 {tgt.address}",
                        "address": str(tgt.address)}
            except Exception as e:
                return {"ok": False, "msg": f"已发现设备但连接失败: {e}"}
        await asyncio.sleep(1)
    return {"ok": False, "msg": "扫描 2 分钟未发现设备 (确认已上电, 可重试)"}


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
