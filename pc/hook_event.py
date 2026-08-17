# -*- coding: utf-8 -*-
"""
VibeDot hook 转发器 — 被 Claude Code / Codex 等 AI 工具的 hook 机制调用,
从 stdin 读 JSON 事件, 归一化后 POST 到本地 vibedot_server, 立即退出 (不阻塞 AI 工具).

用法 (由安装器自动写入各工具配置):
  <python> hook_event.py claude          # Claude Code hooks: stdin JSON
  <python> hook_event.py codex           # Codex notify: JSON 以命令行参数传入

Claude Code stdin JSON: {hook_event_name, tool_name, tool_input, message/prompt, cwd, session_id}
Codex notify JSON:      {type: "agent_turn_complete", turn_counter, thread_id, ...}
"""
import json
import os
import sys
import urllib.request

SERVER = os.environ.get("VIBEDOT_SERVER", "http://127.0.0.1:8266")


def normalize(raw: dict, src: str) -> dict:
    """各工具事件 -> 统一 {type, tool, summary, input, session_id, cwd, src}"""
    # Codex: {"type": "agent_turn_complete", "turn_counter": n, ...}
    if raw.get("type") == "agent_turn_complete":
        return {"type": "Stop", "tool": "", "src": src or "codex",
                "summary": f"Codex 回合结束 #{raw.get('turn_counter', '?')}",
                "input": {}, "session_id": str(raw.get("thread_id", "")), "cwd": ""}

    # Claude Code
    evt = raw.get("hook_event_name", raw.get("type", ""))
    ev = {
        "type": evt,
        "tool": raw.get("tool_name", ""),
        "input": raw.get("tool_input", {}) or {},
        "session_id": raw.get("session_id", ""),
        "cwd": raw.get("cwd", ""),
        "summary": "",
        "src": src or "claude",
    }
    if evt == "Notification":
        ev["summary"] = raw.get("message", "")
    elif evt == "UserPromptSubmit":
        ev["summary"] = (raw.get("prompt") or "")[:80]
    return ev


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else ""
    # Codex notify: JSON 以最后一个 CLI 参数传入 (而非 stdin)
    raw = None
    for arg in reversed(sys.argv[2:]):
        if arg.startswith("{"):
            try:
                raw = json.loads(arg)
            except Exception:
                raw = None
            break
    if raw is None:
        try:
            raw = json.load(sys.stdin)
        except Exception:
            return 0   # 非 JSON 输入, 静默退出, 绝不报错阻塞 AI 工具
    if not isinstance(raw, dict):
        return 0
    try:
        req = urllib.request.Request(
            SERVER + "/api/event",
            data=json.dumps(normalize(raw, src)).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=1.5)
    except Exception:
        pass   # 服务器不在线时静默丢弃
    return 0


if __name__ == "__main__":
    sys.exit(main())
