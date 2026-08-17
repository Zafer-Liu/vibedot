# -*- coding: utf-8 -*-
"""
VibeDot PC 端 - 渲染 vibecoding 进展仪表盘并 BLE 推送到 Quote/0 水墨屏
用法:
  python vibedot_push.py                     # 默认扫描 d:\\temp 的 TODO + git 统计
  python vibedot_push.py --project D:\\code\\myapp
  python vibedot_push.py --loop 300          # 每 300 秒自动推送一次
"""
import argparse
import asyncio
import datetime
import io
import json
import os
import re
import subprocess
import sys
import tempfile

try:
    from PIL import Image, ImageDraw, ImageFont
    import bleak
except ImportError:
    print("请先安装依赖: pip install pillow bleak")
    sys.exit(1)

SERVICE_UUID = "8e400001-1f31-4a3a-9a2f-3d1c0a5b7e01"
CHAR_RX_UUID = "8e400002-1f31-4a3a-9a2f-3d1c0a5b7e01"
CHAR_STATUS_UUID = "8e400003-1f31-4a3a-9a2f-3d1c0a5b7e01"

# 屏幕分辨率 (逆向确认: UC8251D 296x152 横屏, TRES 152 x 296, 帧 5624 字节)
# 控制器扫描: 每行 19 字节(152px) x 296 行, 即横向屏需转置
EPD_W, EPD_H = 296, 152
FRAME_BYTES = (EPD_W * EPD_H) // 8   # 5624


# ---------------- 数据采集 ----------------
def collect_todo(project: str):
    """统计项目目录 TODO/进度: TODO.md 勾选框 + py/js 等源码 TODO 注释"""
    stats = {"total": 0, "done": 0, "items": []}
    todo_md = os.path.join(project, "TODO.md")
    if os.path.exists(todo_md):
        for line in open(todo_md, encoding="utf-8", errors="ignore"):
            m = re.match(r"\s*[-*]\s+\[([ xX])\]\s+(.*)", line)
            if m:
                stats["total"] += 1
                if m.group(1).lower() == "x":
                    stats["done"] += 1
                elif len(stats["items"]) < 7:
                    stats["items"].append(m.group(2)[:40])
    return stats


def collect_git(project: str):
    """今日 commit 数 / 改动行数"""
    try:
        def git(*args):
            r = subprocess.run(["git", "-C", project] + list(args),
                               capture_output=True, text=True, timeout=10)
            return r.stdout.strip()
        today = datetime.date.today().isoformat()
        log = git("log", "--since", today + " 00:00", "--oneline")
        commits = len([l for l in log.splitlines() if l])
        diff = git("diff", "--shortstat", "HEAD")
        return {"commits": commits, "diff": diff}
    except Exception:
        return {"commits": 0, "diff": ""}


def collect_git_daily(project: str, days: int = 7):
    """最近 N 天每日 commit 数 (用于水墨屏柱状图)"""
    counts = [0] * days
    labels = []
    today = datetime.date.today()
    for i in range(days - 1, -1, -1):
        labels.append((today - datetime.timedelta(days=i)).strftime("%m-%d"))
    try:
        since = (today - datetime.timedelta(days=days - 1)).isoformat() + " 00:00"
        r = subprocess.run(["git", "-C", project, "log", "--since", since,
                            "--pretty=format:%ad", "--date=short"],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    d = datetime.datetime.strptime(line, "%Y-%m-%d").date()
                    off = (today - d).days
                    if 0 <= off < days:
                        counts[days - 1 - off] += 1
                except ValueError:
                    pass
    except Exception:
        pass
    return {"counts": counts, "labels": labels}


def collect_claude(project: str):
    """统计 Claude Code 会话用量 (若存在 ~/.claude 统计)"""
    usage = None
    home = os.path.expanduser("~")
    # 常见位置: ~/.claude/stats 或项目 .claude 目录
    for p in [os.path.join(home, ".claude", "stats.json"),
              os.path.join(project, ".claude", "stats.json")]:
        if os.path.exists(p):
            try:
                usage = json.load(open(p, encoding="utf-8"))
                break
            except Exception:
                pass
    return usage


# ---------------- 渲染 ----------------
def find_font(size_bold=None, size=None):
    """寻找系统中文字体"""
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",     # 微软雅黑
        r"C:\Windows\Fonts\msyhbd.ttc",   # 雅黑粗体
        r"C:\Windows\Fonts\simhei.ttf",   # 黑体
        r"C:\Windows\Fonts\deng.ttf",
    ]
    if size_bold:
        for c in candidates:
            if os.path.exists(c):
                try:
                    return ImageFont.truetype(c, size_bold)
                except Exception:
                    pass
    if size:
        for c in candidates:
            if os.path.exists(c):
                try:
                    return ImageFont.truetype(c, size)
                except Exception:
                    pass
    return ImageFont.load_default()


def render(stats, gitstat, w=EPD_W, h=EPD_H):
    img = Image.new("L", (w, h), 255)   # 横向绘制 296x152
    d = ImageDraw.Draw(img)
    f_title = find_font(size_bold=20)
    f_text = find_font(size=14)
    f_small = find_font(size=11)

    # 标题 + 日期
    now = datetime.datetime.now()
    d.text((6, 4), "VIBE CODING", font=f_title, fill=0)
    d.text((w - 88, 10), now.strftime("%m-%d %H:%M"), font=f_small, fill=0)

    # 分隔线
    d.line([(0, 30), (w, 30)], fill=0, width=2)

    # 进度条
    y = 38
    if stats["total"] > 0:
        pct = stats["done"] / stats["total"]
        label = f"任务进度 {stats['done']}/{stats['total']}"
    else:
        pct = 0
        label = "任务进度 0/0 (无 TODO.md)"
    d.text((6, y), label, font=f_text, fill=0)
    by = y + 20
    bar_w = w - 12
    d.rectangle([6, by, 6 + bar_w, by + 10], outline=0, width=1)
    d.rectangle([6, by, 6 + int(bar_w * pct), by + 10], fill=0)

    # TODO 列表 (左侧)
    y = by + 18
    for item in stats["items"]:
        if y > h - 26:
            break
        d.text((8, y), "· " + item, font=f_small, fill=0)
        y += 15

    # git 统计 (右下)
    d.line([(0, h - 24), (w, h - 24)], fill=0, width=1)
    d.text((6, h - 20), f"今日提交 {gitstat['commits']}", font=f_small, fill=0)

    return img.convert("L")


def image_to_1bpp_bytes(img, orient=90, invert=False):
    """横向 296x152 图 -> 控制器字节流 (19B/行 x 296 行)
    orient: 90=转置(默认, 先试), 可选 270/0/180, 显示方向不对时切换
    invert: 黑白极性反转
    """
    transforms = {
        0:   lambda im: im,
        90:  lambda im: im.transpose(Image.ROTATE_90),
        180: lambda im: im.transpose(Image.ROTATE_180),
        270: lambda im: im.transpose(Image.ROTATE_270),
    }
    im = transforms[orient](img)
    assert im.size == (EPD_H, EPD_W), f"变换后尺寸错误: {im.size}"
    b = im.convert("1").tobytes()
    if invert:
        b = bytes(0xFF ^ x for x in b)
    return b


# ---------------- BLE 推送 ----------------
def chunked(data, size):
    for i in range(0, len(data), size):
        yield data[i:i + size]


async def push(img_bytes, fast=False):
    """推送并刷新. fast=True 用 0x04 快刷 (partial LUT, ~0.5s 无闪烁)"""
    refresh_cmd = b"\x04" if fast else b"\x03"
    print("扫描 BLE 设备 VibeDot ...")
    devices = await bleak.BleakScanner.discover(timeout=8.0)
    target = None
    for dev in devices:
        if dev.name == "VibeDot":
            target = dev
            break
    if target is None:
        print("未找到 VibeDot 设备 (请确认固件已运行)")
        return False
    print(f"连接 {target.address} ...")
    async with bleak.BleakClient(target.address) as client:
        mtu = 247
        try:
            await client.exchange_mtu(mtu)
        except Exception:
            pass
        chunk_size = mtu - 3 - 1      # 1 字节命令头
        rx = client.services.get_characteristic(CHAR_RX_UUID)
        status = client.services.get_characteristic(CHAR_STATUS_UUID)

        # 有响应写 (可靠不丢包), 最多重试 3 轮
        for attempt in range(1, 4):
            await client.write_gatt_char(rx, b"\x00", response=True)   # 复位接收计数
            n = 0
            for c in chunked(img_bytes, chunk_size):
                await client.write_gatt_char(rx, b"\x02" + c, response=True)
                n += 1
            # 0x03 全刷 / 0x04 快刷; 面板刷新阻塞固件 0.5~2s, 轮询状态直到 state=1 (完成)
            # 或 state=0 (帧不完整, 重发)
            await client.write_gatt_char(rx, refresh_cmd, response=True)
            result = None
            for _ in range(30):                      # 最多等 15s
                await asyncio.sleep(0.5)
                try:
                    st = await client.read_gatt_char(status)
                except Exception:
                    continue                          # 刷新中 GATT 可能无响应
                if st and st[0] == 1:
                    result = True
                    break
                if st and st[0] == 0 and ((st[1] << 8) | st[2]) < len(img_bytes):
                    # 固件已上报不完整, 但可能还在接收尾声, 再等一轮确认
                    await asyncio.sleep(0.5)
                    try:
                        st2 = await client.read_gatt_char(status)
                    except Exception:
                        continue
                    if st2 and st2[0] == 0:
                        rx_n = (st2[1] << 8) | st2[2]
                        print(f"第 {attempt} 轮帧不完整 (固件收到 {rx_n}/{len(img_bytes)}), 重发...")
                        result = False
                        break
            if result is True:
                print(f"推送成功: {len(img_bytes)} 字节 / {n} 包 (第 {attempt} 轮), 已刷新")
                return True
            if result is None:
                print("状态读取超时, 但刷新可能已执行")
                return True
        print("推送失败: 多次重传仍未收齐")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.getcwd(), help="要统计的项目目录")
    ap.add_argument("--loop", type=int, default=0, help="循环间隔秒数, 0=只推一次")
    ap.add_argument("--save-preview", action="store_true", help="保存预览 PNG")
    ap.add_argument("--orient", type=int, default=90, choices=[90, 270],
                    help="屏幕方向补偿 (默认 90, 上下颠倒时用 270)")
    ap.add_argument("--invert", action="store_true", help="黑白极性反转")
    ap.add_argument("--fast", action="store_true", help="用快速刷新 (0x04, 无黑白闪烁)")
    args = ap.parse_args()

    def build_and_push():
        stats = collect_todo(args.project)
        gstat = collect_git(args.project)
        img = render(stats, gstat)
        data = image_to_1bpp_bytes(img, orient=args.orient, invert=args.invert)
        assert len(data) == FRAME_BYTES
        if args.save_preview:
            img.save(os.path.join(tempfile.gettempdir(), "vibedot_preview.png"))
            print("预览已保存: %s" % os.path.join(tempfile.gettempdir(), "vibedot_preview.png"))
        return asyncio.run(push(data, fast=args.fast))

    if args.loop > 0:
        import time
        while True:
            try:
                build_and_push()
            except Exception as e:
                print("推送失败:", e)
            time.sleep(args.loop)
    else:
        ok = build_and_push()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
