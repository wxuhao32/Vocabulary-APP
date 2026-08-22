"""
v9.125 语音通话 E2E 测试（真实 /ws 通道 + uvicorn 同进程 + 临时 SQLite）

验证 ws_hub 集成在真实 ASGI 环境下的完整链路：
  - 鉴权连接 → call_invite 白名单放行 → 双向信令转发 → call_end 收敛落库
  - 被叫断线 → on_ws_disconnected 钩子真实触发 → 主叫收到 call_end(reason=network)
运行：server venv 的 python 执行本脚本
"""
import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
_tmpdir = tempfile.mkdtemp(prefix="cet4_call_e2e_")
config.DB_PATH = pathlib.Path(_tmpdir) / "test.db"
# 凭证文件一并重定向到临时目录：init_admin() 发现临时库无 admin 时会随机生成凭证
# 并写 config.ADMIN_CRED_FILE —— 不重定向会覆盖生产 server/.admin_credentials.txt！
config.ADMIN_CRED_FILE = pathlib.Path(_tmpdir) / ".admin_credentials.txt"

import db
db.init_db()

import security
import uvicorn
import websockets
from main import app

PORT = 8765
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def mk_user(name):
    cur = db.execute(
        "INSERT INTO users (account, username, phone, password_hash) VALUES (?,?,?,?)",
        (name, name, "1390000" + name[-3:], "x"))
    uid = cur.lastrowid
    pid = db.generate_public_id()
    db.execute("UPDATE users SET public_id=? WHERE id=?", (pid, uid))
    return db.query_one("SELECT * FROM users WHERE id=?", (uid,)), security.create_access_token(uid)


class WsClient:
    """带收件箱的 WS 客户端：后台任务收消息入队，wait_for(type) 按类型等待。"""

    def __init__(self, ws):
        self.ws = ws
        self.inbox = []
        self._task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            async for raw in self.ws:
                try:
                    self.inbox.append(json.loads(raw))
                except Exception:
                    pass
        except Exception:
            pass

    async def wait_for(self, t, timeout=5.0):
        async def _poll():
            while True:
                for m in self.inbox:
                    if m.get("type") == t:
                        return m
                await asyncio.sleep(0.02)
        try:
            return await asyncio.wait_for(_poll(), timeout)
        except asyncio.TimeoutError:
            return None

    def has(self, t):
        return any(m.get("type") == t for m in self.inbox)

    async def send(self, obj):
        await self.ws.send(json.dumps(obj))

    async def close(self):
        self._task.cancel()
        await self.ws.close()


async def main():
    A, tokA = mk_user("alice")
    B, tokB = mk_user("bob")
    lo, hi = min(A["id"], B["id"]), max(A["id"], B["id"])
    db.execute("INSERT INTO friendships (user_a_id, user_b_id) VALUES (?,?)", (lo, hi))

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    srv_task = asyncio.create_task(server.serve())
    for _ in range(50):
        await asyncio.sleep(0.1)
        try:
            _ = await asyncio.open_connection("127.0.0.1", PORT)
            break
        except OSError:
            pass

    print("\n== E2E 场景1：真实 /ws 全链路（invite→accept→relay→end） ==")
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?token={tokA}") as wsA, \
               websockets.connect(f"ws://127.0.0.1:{PORT}/ws?token={tokB}") as wsB:
        ca, cb = WsClient(wsA), WsClient(wsB)
        await ca.wait_for("connected")
        await cb.wait_for("connected")
        check("双方 WS 鉴权连接成功", ca.has("connected") and cb.has("connected"))

        await ca.send({"type": "call_invite", "to": B["public_id"], "call_id": "e2e1"})
        inv = await cb.wait_for("call_invite")
        check("被叫收到 call_invite", inv is not None and inv.get("call_id") == "e2e1")
        check("来电携带主叫信息", (inv or {}).get("from", {}).get("id") == A["public_id"])
        ring = await ca.wait_for("call_ringing")
        check("主叫收到 call_ringing", ring is not None)

        await cb.send({"type": "call_accept", "call_id": "e2e1"})
        acc = await ca.wait_for("call_accept")
        check("主叫收到 call_accept", acc is not None)

        await ca.send({"type": "call_offer", "call_id": "e2e1",
                       "sdp": {"type": "offer", "sdp": "SdpOffer"}})
        off = await cb.wait_for("call_offer")
        check("offer 经真实通道转发", (off or {}).get("sdp", {}).get("sdp") == "SdpOffer")

        await cb.send({"type": "call_answer", "call_id": "e2e1",
                       "sdp": {"type": "answer", "sdp": "SdpAnswer"}})
        ans = await ca.wait_for("call_answer")
        check("answer 经真实通道转发", (ans or {}).get("sdp", {}).get("sdp") == "SdpAnswer")

        await ca.send({"type": "call_ice", "call_id": "e2e1",
                       "candidate": {"candidate": "cand", "sdpMid": "0"}})
        ice = await cb.wait_for("call_ice")
        check("ice 经真实通道转发", (ice or {}).get("candidate", {}).get("candidate") == "cand")

        await ca.send({"type": "call_end", "call_id": "e2e1", "duration": 42})
        end = await cb.wait_for("call_end")
        check("被叫收到 call_end duration=42", (end or {}).get("duration") == 42)
        rec = await ca.wait_for("new_message")
        recb = await cb.wait_for("new_message")
        ok_rec = rec and rec.get("message", {}).get("type") == "call" \
            and recb and recb.get("message", {}).get("type") == "call"
        check("双方收到通话记录 new_message", bool(ok_rec))
        await asyncio.sleep(0.2)
        row = db.query_one("SELECT * FROM messages WHERE type='call' ORDER BY id DESC LIMIT 1")
        o = json.loads(row["content"]) if row else {}
        check("通话记录落库 end/42/notified=1",
              o.get("event") == "end" and o.get("duration") == 42 and row["notified"] == 1, str(o))
        await ca.close()
        await cb.close()

    print("\n== E2E 场景2：通话中被叫断线（真实 WS 断开触发钩子） ==")
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?token={tokA}") as wsA:
        ca = WsClient(wsA)
        await ca.wait_for("connected")
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?token={tokB}") as wsB:
            cb = WsClient(wsB)
            await cb.wait_for("connected")
            await ca.send({"type": "call_invite", "to": B["public_id"], "call_id": "e2e2"})
            got_inv = await cb.wait_for("call_invite")
            check("被叫收到来电", got_inv is not None)
            await cb.send({"type": "call_accept", "call_id": "e2e2"})
            got_acc = await ca.wait_for("call_accept")
            check("通话建立（active）", got_acc is not None)
            await cb.close()      # 被叫网络断开
        end = await ca.wait_for("call_end", timeout=5)
        check("主叫收到 call_end（断线收敛）",
              end is not None and end.get("reason") == "network", str(end))
        rec = await ca.wait_for("new_message")
        check("断线通话记录推送", rec is not None and rec.get("message", {}).get("type") == "call")
        await ca.close()

    print("\n== E2E 场景3：非通话上行仍被忽略（白名单外消息不产生推送） ==")
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?token={tokA}") as wsA:
        ca = WsClient(wsA)
        await ca.wait_for("connected")
        await ca.send({"type": "new_message", "message": {"id": 1}})   # 伪造推送类消息
        await ca.send({"type": "notification", "content": "x"})
        await asyncio.sleep(0.4)
        check("伪造 new_message 被忽略", not ca.has("new_message"))
        check("伪造 notification 被忽略", not ca.has("notification"))
        await ca.close()

    server.should_exit = True
    await asyncio.sleep(0.3)
    srv_task.cancel()

    print(f"\n========== E2E 结果：{PASS} 通过 / {FAIL} 失败 ==========")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
