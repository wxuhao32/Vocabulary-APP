# -*- coding: utf-8 -*-
"""
Vocabulary APP — 公网 Tunnel 启动器（Cloudflare Tunnel / cloudflared）

功能：
1. 启动 cloudflared quick tunnel 将本地 FastAPI（默认 http://127.0.0.1:8000）映射到公网；
2. 自动解析输出的公网 URL（https://xxx.trycloudflare.com）；
3. 将公网地址集中写入 server/tunnel_config.json（public_base_url / public_ws_url）；
4. 前台保持运行；Ctrl+C 停止隧道。

用法：
  python tunnel/start_tunnel.py [--local-url http://127.0.0.1:8000] [--tunnel tunnel/cloudflared.exe]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # server/
CONFIG_PATH = os.path.join(BASE_DIR, "tunnel_config.json")
URL_RE = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)", re.IGNORECASE)


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"local_host": "127.0.0.1", "local_port": 8000,
            "public_base_url": "", "public_ws_url": "",
            "tunnel_provider": "cloudflare", "updated_at": ""}


def save_public_url(public_url):
    cfg = load_config()
    cfg["public_base_url"] = public_url
    cfg["public_ws_url"] = public_url.replace("https://", "wss://") + "/ws/echo"
    cfg["tunnel_provider"] = "cloudflare"
    cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def copy_clipboard(text):
    """Windows 剪贴板复制（clip 命令；URL 为纯 ASCII 无编码问题）。失败静默。"""
    try:
        p = subprocess.Popen(["clip"], stdin=subprocess.PIPE)
        p.communicate(text.encode("utf-8"))
    except Exception:
        pass


def show_banner(url, ws_url):
    """域名生成后的醒目提示：大字横幅 + 已复制到剪贴板。"""
    try:
        os.system("")   # 开启 Windows 终端 ANSI 颜色
    except Exception:
        pass
    G = "\x1b[32m"; R = "\x1b[36m"; N = "\x1b[0m"
    W = 62
    line = "*" * W
    print()
    print(G + line + N)
    print(G + "  ✅ 公网域名已生成！已自动复制到剪贴板，可直接粘贴" + N)
    print()
    print(G + "  >>>  " + R + url + G + "  <<<" + N)
    print()
    print(G + "  手机 App：我的 → 账户与安全 → 服务器地址 → 粘贴" + N)
    print(G + "  验证：浏览器打开 " + url + "/api/health 返回 ok:true 即通" + N)
    print(G + "  WebSocket 验证地址：" + ws_url + N)
    print(G + "  关闭本窗口 = 停止公网访问（域名每次启动会变）" + N)
    print(G + line + N)
    print()


def main():
    parser = argparse.ArgumentParser(description="Vocabulary APP 公网 Tunnel 启动器")
    parser.add_argument("--local-url", default=None, help="本地服务地址，默认读取 tunnel_config.json（http://127.0.0.1:8000）")
    parser.add_argument("--tunnel", default=None, help="cloudflared 可执行文件路径（默认 server/tunnel/cloudflared.exe）")
    parser.add_argument("--quiet", action="store_true", help="不打印 cloudflared 原始日志（默认开启，日志写入 tunnel/cloudflared.log）")
    args = parser.parse_args()

    cfg = load_config()
    local_url = args.local_url or ("http://%s:%s" % (cfg["local_host"], cfg["local_port"]))
    exe = args.tunnel or os.path.join(BASE_DIR, "tunnel", "cloudflared.exe")

    if not os.path.exists(exe):
        print("[错误] 未找到 cloudflared：%s" % exe)
        print("       请先运行  server\\tunnel\\下载cloudflared.bat  下载客户端。")
        return 1

    print("=" * 62)
    print("  Vocabulary APP 公网 Tunnel")
    print("  本地映射: %s  →  公网 https://xxxx.trycloudflare.com" % local_url)
    print("  首次启动约需 10~30 秒生成公网地址，请稍候...")
    print("=" * 62)

    cmd = [exe, "tunnel", "--url", local_url, "--no-autoupdate"]
    quiet = args.quiet
    log_path = os.path.join(BASE_DIR, "tunnel", "cloudflared.log")
    logf = None
    if quiet:
        try:
            logf = open(log_path, "w", encoding="utf-8", errors="replace")
        except Exception:
            logf = None
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)

    published = False
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if quiet:
                if logf:
                    logf.write(line + "\n")
            else:
                if line:
                    print(line)
            if not published:
                m = URL_RE.search(line)
                if m:
                    url = "https://" + m.group(1)
                    cfg = save_public_url(url)
                    published = True
                    show_banner(url, cfg["public_ws_url"])
                    copy_clipboard(url)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        if logf:
            try:
                logf.close()
            except Exception:
                pass

    if not published:
        print()
        print("[提示] 未检测到公网地址输出，可能原因：")
        print("  1. cloudflared 无法连接外网（检查网络/防火墙）")
        print("  2. 本地 FastAPI 未启动（请先运行「启动CET4Prep服务器.bat」）")
        print("  3. 端口被占用（tunnel_config.json 中的 local_port 是否与 FastAPI 一致）")
        print("  详细日志: server\\tunnel\\cloudflared.log")
    return 0 if published else 1


if __name__ == "__main__":
    sys.exit(main())
