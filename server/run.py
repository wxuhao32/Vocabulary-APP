"""
CET4Prep 本地认证系统 — 启动入口
用法：
  python run.py                 # 默认 127.0.0.1:8000
  python run.py --host 0.0.0.0  # 局域网可访问（Android 真机连电脑 IP）
"""
import argparse
import uvicorn


def main():
    parser = argparse.ArgumentParser(description="CET4Prep 本地认证服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print(f"Auth Server → http://{args.host}:{args.port}  (Ctrl+C 停止)")
    uvicorn.run("main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
