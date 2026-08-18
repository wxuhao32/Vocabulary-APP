"""
CET4Prep 本地认证系统 — FastAPI 入口
开发电脑即本地服务器：uvicorn 启动于 127.0.0.1:8000
"""
import logging
import pathlib

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

import config
import db
import ws_hub
from routers import auth as auth_router
from routers import friendship, message, file as file_router, notification as notif_router, moments
from routers import admin as admin_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="CET4Prep Auth Server", version="1.0.0")

# CORS：Android WebView(file://) 与本地浏览器均可跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(auth_router.me_router)
app.include_router(friendship.router)
app.include_router(message.router)
app.include_router(file_router.router)
app.include_router(notif_router.router)
app.include_router(moments.router)
app.include_router(admin_router.router)      # v9.93：管理员系统（隐藏双因素认证）
app.include_router(admin_router.app_router)  # v9.93：客户端版本检查


@app.get("/app", response_class=HTMLResponse)
def serve_app():
    """v9.93：电脑浏览器直接访问客户端（服务器托管 Android assets 单文件 index.html）。"""
    p = pathlib.Path(__file__).resolve().parent.parent / "app" / "src" / "main" / "assets" / "index.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("index.html 未找到（需在 Android 工程 app/src/main/assets 目录）", status_code=404)


@app.on_event("startup")
def on_startup():
    db.init_db()
    admin_router.init_admin()                # v9.93：预置管理员（仅首次）
    logging.getLogger("app").info("SQLite 就绪: %s", config.DB_PATH)
    logging.getLogger("app").info("Auth Server 启动完成（DEBUG=%s）", config.DEBUG)


@app.get("/api/health")
def health():
    return {"ok": True, "service": "cet4prep-auth", "version": "1.0.0"}


@app.websocket("/ws/echo")
async def ws_echo(websocket: WebSocket):
    """v9.100：WebSocket 验证端点（不参与业务）——用于验证公网 Tunnel 的 WebSocket 转发能力。
    客户端发送任意文本，服务端原样回显 echo:<msg>；断开后自动退出，客户端可按现有重连机制恢复。"""
    await websocket.accept()
    try:
        while True:
            msg = await websocket.receive_text()
            await websocket.send_text("echo:" + msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.websocket("/ws")
async def ws_main(websocket: WebSocket):
    """v9.110：聊天消息 / 通知实时推送端点（业务）。
    鉴权：?token=<access_token>；消息持久化仍在 REST 路由，WS 仅负责实时传输。
    协议：服务端推送 JSON 事件（new_message / revoked / read / deleted / cleared / notification）；
    客户端仅发送 {"type":"ping"} 心跳。断开后由客户端自动重连 + 既有 REST 机制补同步。"""
    await ws_hub.handle_ws(websocket)
