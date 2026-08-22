"""
CET4Prep 社交模块 — WebSocket 实时推送中心（v9.110）

设计要点：
1. 鉴权：连接时携带 ?token=<access_token>（WebView JS 无法自定义 WS 请求头，故走 query）。
   服务端用 JWT 解析出 user_id 并绑定连接 —— 连接身份只由 Token 决定，
   客户端消息除 ping 外一律忽略（本通道为"服务端→客户端"单向推送，杜绝伪造订阅他人消息）。
2. 连接注册表：user_id -> set[WebSocket]，支持多设备 / 同设备多连接，推送广播到全部在线连接。
3. 心跳：客户端每 25s 发 {"type":"ping"}，服务端回 {"type":"pong"}；
   70s 收不到任何客户端消息则关闭连接（客户端自动重连兜底）。
4. 同步端点调用：REST 路由（同步函数）通过 push_to_user() 把推送调度回 WS 事件循环，
   连接未建立时安全空操作 —— 消息仍由数据库持久化，WS 只负责实时传输，不替代数据库。
5. 离线补同步：消息全部落库；客户端重连成功（onopen）后走既有 REST 机制
   （通知列表 + 好友列表 + 当前会话历史重拉）补齐离线期间遗漏 —— 不删除任何现有同步机制。
"""
import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

import call as call_router   # v9.125：语音通话信令（call_* 上行白名单 + 断线收敛）
import db
import security
from routers.auth import _check_banned

log = logging.getLogger("ws_hub")

# 鉴权失败关闭码（客户端据此触发 Refresh Token 重连）
CLOSE_UNAUTHORIZED = 4401
CLOSE_BANNED = 4403
# 心跳：客户端 ping 间隔 25s；服务端 70s 无任何消息即断开
HEARTBEAT_TIMEOUT = 70


class ConnectionHub:
    """用户在线连接注册表 + 推送。单进程内存结构（与验证码 store 一致，未来可换 Redis）。"""

    def __init__(self):
        self._conns: dict[int, set[WebSocket]] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None  # WS 事件循环（推送调度目标）

    def set_loop(self, loop) -> None:
        if loop is not None:
            self._loop = loop

    async def register(self, user_id: int, ws: WebSocket) -> None:
        async with self._lock:
            self._conns.setdefault(user_id, set()).add(ws)

    async def unregister(self, user_id: int, ws: WebSocket) -> None:
        async with self._lock:
            s = self._conns.get(user_id)
            if s:
                s.discard(ws)
                if not s:
                    self._conns.pop(user_id, None)

    async def _send(self, ws: WebSocket, payload: dict) -> bool:
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception:
            return False

    async def push_to_user_async(self, user_id: int, payload: dict) -> int:
        """推送给该用户全部在线连接（多设备/多连接广播）。返回成功发送数。"""
        async with self._lock:
            conns = list(self._conns.get(user_id, ()))
        ok = 0
        dead = []
        for ws in conns:
            if await self._send(ws, payload):
                ok += 1
            else:
                dead.append(ws)
        if dead:
            async with self._lock:
                s = self._conns.get(user_id)
                if s:
                    for ws in dead:
                        s.discard(ws)
                    if not s:
                        self._conns.pop(user_id, None)
        return ok

    def push_to_user(self, user_id: int, payload: dict) -> None:
        """同步端点（REST 路由）调用入口：调度到 WS 事件循环异步发送。
        用户无在线连接或事件循环未就绪时安全空操作（数据仍已落库，重连后客户端会补拉）。"""
        if not self._conns.get(user_id) or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.push_to_user_async(user_id, payload), self._loop)
        except Exception as e:
            log.warning("push_to_user schedule fail uid=%s err=%s", user_id, e)

    def online(self, user_id: int) -> bool:
        return bool(self._conns.get(user_id))


# 全局唯一实例（单进程部署）
hub = ConnectionHub()


async def handle_ws(websocket: WebSocket) -> None:
    """/ws 业务端点：鉴权 → 注册 → 心跳保活 → 推送（服务端→客户端单向）。"""
    hub.set_loop(asyncio.get_running_loop())
    token = (websocket.query_params.get("token") or "").strip()
    user = _authenticate(token)
    if user is None:
        # 未鉴权：拒绝连接（客户端收到 4401 后走 Refresh Token 重连流程）
        try:
            await websocket.accept()
            await websocket.send_text(json.dumps(
                {"type": "error", "code": "unauthorized", "detail": "access_token 无效或已过期"},
                ensure_ascii=False))
        except Exception:
            pass
        try:
            await websocket.close(code=CLOSE_UNAUTHORIZED)
        except Exception:
            pass
        return

    await websocket.accept()
    user_id = user["id"]
    await hub.register(user_id, websocket)
    try:
        # 连接确认（携带本人公开信息；客户端据此可校验连接身份）
        await websocket.send_text(json.dumps({
            "type": "connected",
            "user": {"id": user["public_id"], "nickname": user["username"]},
        }, ensure_ascii=False))
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_TIMEOUT)
            except asyncio.TimeoutError:
                # 客户端长时间无心跳（如后台被节流）→ 主动断开，客户端恢复后自动重连
                break
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                obj = None
            if isinstance(obj, dict) and obj.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            # v9.125：通话信令白名单放行（call_* 消息由 call 模块校验身份/关系后转发，
            # 其余客户端消息仍一律忽略：本通道不允许客户端订阅/指定任意接收者）
            elif isinstance(obj, dict) and str(obj.get("type") or "").startswith("call_"):
                try:
                    await call_router.handle_call_signal(hub, user, obj)
                except Exception as e:
                    log.warning("call signal error uid=%s type=%s err=%s",
                                user_id, obj.get("type"), e)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("ws closed uid=%s err=%s", user_id, e)
    finally:
        await hub.unregister(user_id, websocket)
        # v9.125：信令断开 → 通话状态收敛（响铃中=取消；通话中=按挂断处理并通知对端）
        try:
            await call_router.on_ws_disconnected(hub, user_id)
        except Exception as e:
            log.warning("call disconnect cleanup error uid=%s err=%s", user_id, e)


def _authenticate(token: str):
    """解析 access_token → 用户行；无效 / 过期 / 封禁 → None。"""
    if not token:
        return None
    try:
        payload = security.decode_token(token, "access")
        user_id = int(payload["sub"])
    except Exception:
        return None
    row = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        return None
    if _check_banned(row):
        return None  # 封禁（含到期自动解封逻辑）→ 拒绝 WS 连接
    return row
