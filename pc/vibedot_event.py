# -*- coding: utf-8 -*-
"""
VibeDot 通用事件适配器 — 任何 AI agent 工具都能调用 (WorkBuddy / Qoder / 手动):

    python vibedot_event.py <state> "<一句话摘要>" [会话id] [来源]

state: thinking | coding | command | searching | done | waiting | error
来源: qoder / workbuddy / claude / codex ... (显示在 agent 名字上)
示例:
    python vibedot_event.py coding "重构登录模块" wb1 workbuddy
    python vibedot_event.py done "重构完成" wb1 workbuddy
"""
import json
import os
import sys
import urllib.request

SERVER = os.environ.get("VIBEDOT_SERVER", "http://127.0.0.1:8266")


def main():
    if len(sys.argv) < 2:
        return 0
    state = sys.argv[1]
    summary = sys.argv[2] if len(sys.argv) > 2 else ""
    session = sys.argv[3] if len(sys.argv) > 3 else ""
    src = sys.argv[4] if len(sys.argv) > 4 else "manual"
    try:
        req = urllib.request.Request(
            SERVER + "/api/event",
            data=json.dumps({"type": state, "summary": summary,
                             "session_id": session, "src": src}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=1.5)
    except Exception:
        pass          # 服务器不在线时静默丢弃, 绝不影响 agent 工作
    return 0


if __name__ == "__main__":
    sys.exit(main())
