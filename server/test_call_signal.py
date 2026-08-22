"""
v9.125 语音通话信令链路测试（单元级：mock hub + 临时 SQLite，不碰生产库）

覆盖：
  1. 正常通话：invite → ringing → accept → offer/answer/ice relay → end（含落库）
  2. 拒绝：reject → 落 rejected
  3. 取消：cancel → 落 canceled
  4. 超时未接：cancel(timeout) → 落 missed
  5. 呼叫中被叫断线：call_fail + 落 canceled
  6. 通话中断线：call_end + 落 end（服务端计时兜底）
  7. 非好友 / 8. 不在线 / 9. 忙线 / 10. 呼自己 / 11. ringing 期 offer 丢弃
运行：python test_call_signal.py
"""
import asyncio
import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---- 临时数据库（隔离生产 vocab_auth.db） ----
import config
_tmpdir = tempfile.mkdtemp(prefix="cet4_call_test_")
config.DB_PATH = pathlib.Path(_tmpdir) / "test.db"

import db
db.init_db()

import call

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


class MockHub:
    """记录全部推送；(uid, payload) 列表；online() 由测试控制。"""

    def __init__(self):
        self.sent = []
        self.online_ids = set()

    def online(self, uid):
        return uid in self.online_ids

    async def push_to_user_async(self, uid, payload):
        self.sent.append((uid, payload))
        return 1

    def pops(self, uid, t):
        """取指定用户指定类型的全部推送（不清空）。"""
        return [p for (u, p) in self.sent if u == uid and p.get("type") == t]

    def clear(self):
        self.sent.clear()


def mk_user(name):
    cur = db.execute(
        "INSERT INTO users (account, username, phone, password_hash) VALUES (?,?,?,?)",
        (name, name, "1380000" + name[-3:], "x"))
    uid = cur.lastrowid
    pid = db.generate_public_id()
    db.execute("UPDATE users SET public_id=? WHERE id=?", (pid, uid))
    return db.query_one("SELECT * FROM users WHERE id=?", (uid,))


def be_friends(a, b):
    lo, hi = min(a["id"], b["id"]), max(a["id"], b["id"])
    db.execute("INSERT INTO friendships (user_a_id, user_b_id) VALUES (?,?)", (lo, hi))


def last_call_msg(conv_peer_ids):
    """查最近的 call 记录（sender 在双方之一）。"""
    rows = db.query(
        "SELECT m.* FROM messages m JOIN conversations c ON m.conversation_id=c.id "
        "WHERE m.type='call' AND (c.user_a_id=? OR c.user_b_id=?) ORDER BY m.id DESC LIMIT 1",
        (conv_peer_ids[0], conv_peer_ids[1]))
    return rows[0] if rows else None


async def main():
    hub = MockHub()
    A = mk_user("alice")
    B = mk_user("bob")
    C = mk_user("carol")          # 非好友
    D = mk_user("dave")           # 好友但离线
    be_friends(A, B)
    be_friends(A, D)
    hub.online_ids = {A["id"], B["id"], C["id"]}   # D 离线

    print("\n== 场景1：正常通话 invite → accept → relay → end ==")
    await call.handle_call_signal(hub, A, {"type": "call_invite", "to": B["public_id"], "call_id": "cid1"})
    check("B 收到来电 call_invite", len(hub.pops(B["id"], "call_invite")) == 1,
          str(hub.sent))
    inv = hub.pops(B["id"], "call_invite")[0]
    check("来电携带主叫 public_id", inv.get("from", {}).get("id") == A["public_id"])
    check("来电携带主叫昵称", inv.get("from", {}).get("nickname") == "alice")
    check("A 收到回铃 call_ringing", len(hub.pops(A["id"], "call_ringing")) == 1)
    check("注册表 ringing", call.calls.get("cid1", {}).get("status") == "ringing")
    hub.clear()

    # ringing 期 offer 应被丢弃（relay 状态检查）
    await call.handle_call_signal(hub, A, {"type": "call_offer", "call_id": "cid1", "sdp": {"type": "offer", "sdp": "x"}})
    check("ringing 期 offer 被丢弃", len(hub.pops(B["id"], "call_offer")) == 0)

    await call.handle_call_signal(hub, B, {"type": "call_accept", "call_id": "cid1"})
    check("A 收到 call_accept", len(hub.pops(A["id"], "call_accept")) == 1)
    check("注册表 active", call.calls.get("cid1", {}).get("status") == "active")
    hub.clear()

    await call.handle_call_signal(hub, A, {"type": "call_offer", "call_id": "cid1",
                                           "sdp": {"type": "offer", "sdp": "OFFER_SDP"}})
    fwd_offer = hub.pops(B["id"], "call_offer")
    check("offer 转发给 B", fwd_offer and fwd_offer[0].get("sdp", {}).get("sdp") == "OFFER_SDP",
          str(fwd_offer))
    await call.handle_call_signal(hub, B, {"type": "call_answer", "call_id": "cid1",
                                           "sdp": {"type": "answer", "sdp": "ANSWER_SDP"}})
    fwd_ans = hub.pops(A["id"], "call_answer")
    check("answer 转发给 A", fwd_ans and fwd_ans[0].get("sdp", {}).get("sdp") == "ANSWER_SDP",
          str(fwd_ans))
    await call.handle_call_signal(hub, A, {"type": "call_ice", "call_id": "cid1",
                                           "candidate": {"candidate": "c1"}})
    check("ice 转发给 B", hub.pops(B["id"], "call_ice")[0].get("candidate") == {"candidate": "c1"})
    hub.clear()

    await call.handle_call_signal(hub, A, {"type": "call_end", "call_id": "cid1", "duration": 65})
    ends = hub.pops(B["id"], "call_end")
    check("B 收到挂断 call_end", len(ends) == 1 and ends[0].get("duration") == 65)
    recs = [p for (u, p) in hub.sent if p.get("type") == "new_message"
            and p.get("message", {}).get("type") == "call"]
    check("双方收到通话记录 new_message", len(recs) == 2, str(len(recs)))
    check("注册表已清理", "cid1" not in call.calls and A["id"] not in call.user_calls)
    msg = last_call_msg((A["id"], B["id"]))
    o = json.loads(msg["content"]) if msg else {}
    check("落库 event=end duration=65", o.get("event") == "end" and o.get("duration") == 65, str(o))
    check("落库 notified=1（不弹系统通知）", msg is not None and msg["notified"] == 1)
    hub.clear()

    print("\n== 场景2：拒绝 reject ==")
    await call.handle_call_signal(hub, A, {"type": "call_invite", "to": B["public_id"], "call_id": "cid2"})
    hub.clear()
    await call.handle_call_signal(hub, B, {"type": "call_reject", "call_id": "cid2"})
    check("A 收到 call_reject", len(hub.pops(A["id"], "call_reject")) == 1)
    msg = last_call_msg((A["id"], B["id"]))
    o = json.loads(msg["content"]) if msg else {}
    check("落库 rejected", o.get("event") == "rejected", str(o))
    check("注册表已清理", "cid2" not in call.calls)
    hub.clear()

    print("\n== 场景3：取消 cancel ==")
    await call.handle_call_signal(hub, A, {"type": "call_invite", "to": B["public_id"], "call_id": "cid3"})
    hub.clear()
    await call.handle_call_signal(hub, A, {"type": "call_cancel", "call_id": "cid3", "reason": "user"})
    check("B 收到 call_cancel", len(hub.pops(B["id"], "call_cancel")) == 1)
    msg = last_call_msg((A["id"], B["id"]))
    o = json.loads(msg["content"]) if msg else {}
    check("落库 canceled", o.get("event") == "canceled", str(o))
    hub.clear()

    print("\n== 场景4：超时未接 cancel(timeout) → missed ==")
    await call.handle_call_signal(hub, A, {"type": "call_invite", "to": B["public_id"], "call_id": "cid4"})
    hub.clear()
    await call.handle_call_signal(hub, A, {"type": "call_cancel", "call_id": "cid4", "reason": "timeout"})
    msg = last_call_msg((A["id"], B["id"]))
    o = json.loads(msg["content"]) if msg else {}
    check("落库 missed（未接听）", o.get("event") == "missed", str(o))
    hub.clear()

    print("\n== 场景5：呼叫中被叫断线 ==")
    await call.handle_call_signal(hub, A, {"type": "call_invite", "to": B["public_id"], "call_id": "cid5"})
    hub.clear()
    await call.on_ws_disconnected(hub, B["id"])
    fails = hub.pops(A["id"], "call_fail")
    check("A 收到 call_fail（网络断开）", len(fails) == 1 and "断开" in fails[0].get("reason", ""),
          str(fails))
    msg = last_call_msg((A["id"], B["id"]))
    o = json.loads(msg["content"]) if msg else {}
    check("落库 canceled", o.get("event") == "canceled", str(o))
    hub.clear()

    print("\n== 场景6：通话中断线（服务端计时兜底） ==")
    await call.handle_call_signal(hub, A, {"type": "call_invite", "to": B["public_id"], "call_id": "cid6"})
    await call.handle_call_signal(hub, B, {"type": "call_accept", "call_id": "cid6"})
    hub.clear()
    time.sleep(0.05)
    await call.on_ws_disconnected(hub, B["id"])
    ends = hub.pops(A["id"], "call_end")
    check("A 收到 call_end", len(ends) == 1 and ends[0].get("reason") == "network", str(ends))
    check("时长按服务端计时（>=0）", len(ends) == 1 and ends[0].get("duration", -1) >= 0)
    msg = last_call_msg((A["id"], B["id"]))
    o = json.loads(msg["content"]) if msg else {}
    check("落库 end", o.get("event") == "end", str(o))
    hub.clear()

    print("\n== 场景7：非好友呼叫被拒 ==")
    await call.handle_call_signal(hub, A, {"type": "call_invite", "to": C["public_id"], "call_id": "cid7"})
    fails = hub.pops(A["id"], "call_fail")
    check("call_fail 好友关系", len(fails) == 1 and "好友" in fails[0].get("reason", ""), str(fails))
    check("未注册通话", "cid7" not in call.calls)
    hub.clear()

    print("\n== 场景8：对方不在线 ==")
    await call.handle_call_signal(hub, A, {"type": "call_invite", "to": D["public_id"], "call_id": "cid8"})
    fails = hub.pops(A["id"], "call_fail")
    check("call_fail 不在线", len(fails) == 1 and "在线" in fails[0].get("reason", ""), str(fails))
    hub.clear()

    print("\n== 场景9：忙线（对方已在通话） ==")
    await call.handle_call_signal(hub, A, {"type": "call_invite", "to": B["public_id"], "call_id": "cid9"})
    hub.clear()
    await call.handle_call_signal(hub, C, {"type": "call_invite", "to": A["public_id"], "call_id": "cid10"})
    fails = hub.pops(C["id"], "call_fail")
    check("call_fail 对方正忙", len(fails) == 1 and "忙" in fails[0].get("reason", ""), str(fails))
    # 收敛残留
    await call.handle_call_signal(hub, A, {"type": "call_end", "call_id": "cid9", "duration": 1})
    hub.clear()

    print("\n== 场景10：呼叫自己 ==")
    await call.handle_call_signal(hub, A, {"type": "call_invite", "to": A["public_id"], "call_id": "cid11"})
    fails = hub.pops(A["id"], "call_fail")
    check("call_fail 不能呼叫自己", len(fails) == 1 and "自己" in fails[0].get("reason", ""), str(fails))
    hub.clear()

    print("\n== record_text 文案 ==")
    check("end", call.record_text('{"event":"end","duration":185}') == "通话 03:05")
    check("missed", call.record_text('{"event":"missed","duration":0}') == "未接听")
    check("rejected", call.record_text('{"event":"rejected"}') == "已拒绝")
    check("canceled", call.record_text('{"event":"canceled"}') == "已取消")
    check("坏 JSON 容错", call.record_text("not-json") == "通话")

    print(f"\n========== 结果：{PASS} 通过 / {FAIL} 失败 ==========")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
