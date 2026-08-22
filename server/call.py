"""
CET4Prep 语音通话信令模块（v9.125）— 一对一实时语音通话

设计要点（增量扩展，不动既有消息链路）：
1. 传输面：WebRTC P2P（免费 Google STUN，无 TURN），SDP/ICE 经既有 /ws 通道中转。
2. 信令面：客户端在 /ws 上行 call_* 消息（此前 /ws 仅收 ping，本次按白名单放开通话类），
   服务端校验身份/好友/忙碌状态后转发或回错误，杜绝指定任意接收者的伪造转发。
3. 状态机：ringing → active → end；rejected / canceled(含超时未接) / 网络断开均收敛为终态，
   注册表（内存 dict）随通话结束即清理，重启后自然清空（通话本就不持久）。
4. 通话记录：结束后在会话里落一条 type='call' 的消息（content 为 JSON {event,duration}），
   notified=1（不弹系统通知）+ read_at=now（不计未读），WS 实时推双方刷新聊天页。
"""
import json
import logging
import time

import db
import social_util as su

log = logging.getLogger("call")

# 通话注册表（单进程内存）：call_id -> {caller, callee, status, created, answered}
calls: dict[str, dict] = {}
# 用户 -> 正在进行的 call_id（含 ringing，用于忙碌判定）
user_calls: dict[int, str] = {}


# ---------------- 工具 ----------------
def _user_by_pid(pid: str):
    return db.query_one("SELECT * FROM users WHERE public_id=?", ((pid or "").strip(),))


def _public(u) -> dict:
    return {"id": u["public_id"], "nickname": u["username"], "avatar": u["avatar"] or ""}


def _cleanup(call_id: str):
    c = calls.pop(call_id, None)
    if not c:
        return
    for uid in (c["caller"], c["callee"]):
        if user_calls.get(uid) == call_id:
            user_calls.pop(uid, None)


def _insert_call_record(call: dict, event: str, duration: int = 0):
    """落一条通话记录消息（sender=发起方；notified=1 不弹系统通知，read_at=now 不计未读）。
    随后 WS 实时推送双方（与 message.py 的 new_message 事件同构，前端复用现有渲染入口）。"""
    try:
        conv_id = su.get_or_create_conversation(call["caller"], call["callee"])
        # v9.129：media=video 时记录带视频标识（前端/通知栏据此显示"视频通话"）
        payload = {"event": event, "duration": max(0, int(duration))}
        if call.get("media") == "video":
            payload["media"] = "video"
        content = json.dumps(payload, ensure_ascii=False)
        cur = db.execute(
            "INSERT INTO messages (conversation_id, sender_id, type, content, notified, read_at) "
            "VALUES (?,?,'call',?,1,datetime('now','localtime'))",
            (conv_id, call["caller"], content))
        db.execute("UPDATE conversations SET last_message_at=datetime('now','localtime') WHERE id=?",
                   (conv_id,))
        mid = cur.lastrowid
        row = db.query_one("SELECT created_at FROM messages WHERE id=?", (mid,))
        created = row["created_at"] if row else ""
        caller = db.query_one("SELECT * FROM users WHERE id=?", (call["caller"],))
        callee = db.query_one("SELECT * FROM users WHERE id=?", (call["callee"],))
        if not caller or not callee:
            return
        base = {"id": mid, "type": "call", "content": content, "file_id": None,
                "revoked": False, "created_at": created, "read": True, "notified": True}
        # 双方视角各推一份（friend 字段 = 聊天对端，与 message.py 推送格式一致）
        yield_msgs = [
            (call["callee"], _public(caller), dict(base, sender="them")),
            (call["caller"], _public(callee), dict(base, sender="me")),
        ]
        return mid, yield_msgs
    except Exception as e:
        log.warning("通话记录落库失败: %s", e)
        return None


async def _push_record(hub, rec):
    """把 _insert_call_record 的结果实时推给双方（不在线则静默，重连后走历史拉取）。"""
    if not rec:
        return
    _, targets = rec
    for uid, friend, m in targets:
        await hub.push_to_user_async(uid, {"type": "new_message", "friend": friend, "message": m})


async def _end_and_record(hub, call_id: str, event: str, duration: int = 0):
    c = calls.get(call_id)
    if not c:
        return
    rec = _insert_call_record(c, event, duration)
    _cleanup(call_id)
    await _push_record(hub, rec)


def _duration_of(c: dict) -> int:
    if not c.get("answered"):
        return 0
    return max(0, int(time.time() - c["answered"]))


# ---------------- 通话记录文案（会话列表摘要复用） ----------------
def record_text(content_json: str) -> str:
    """通话记录 content(JSON) → 可读文案：通话 03:24 / 未接听 / 已取消 / 已拒绝（视频通话带前缀）。"""
    try:
        o = json.loads(content_json or "{}")
    except Exception:
        o = {}
    ev = o.get("event", "")
    dur = int(o.get("duration") or 0)
    pre = "视频通话" if o.get("media") == "video" else "通话"
    if ev == "end":
        return "%s %02d:%02d" % (pre, dur // 60, dur % 60)
    if ev == "missed":
        return "视频通话未接听" if o.get("media") == "video" else "未接听"
    if ev == "rejected":
        return "已拒绝"
    if ev == "canceled":
        return "已取消"
    return pre


# ---------------- 信令入口（ws_hub 收到 call_* 上行时调用） ----------------
async def handle_call_signal(hub, user, obj) -> None:
    """user=已鉴权用户行；obj=客户端上行 JSON。仅处理白名单 call_* 消息。"""
    t = obj.get("type", "")
    if t == "call_invite":
        await _on_invite(hub, user, obj)
    elif t == "call_accept":
        await _on_accept(hub, user, obj)
    elif t == "call_reject":
        await _on_reject(hub, user, obj)
    elif t == "call_cancel":
        await _on_cancel(hub, user, obj)
    elif t == "call_end":
        await _on_end(hub, user, obj)
    elif t in ("call_offer", "call_answer", "call_ice"):
        await _on_relay(hub, user, obj, t)


async def _on_invite(hub, user, obj):
    me = user["id"]
    call_id = str(obj.get("call_id") or "")[:64]
    peer = _user_by_pid(obj.get("to") or "")
    fail = None
    if not call_id or not peer:
        fail = "目标用户不存在"
    elif peer["id"] == me:
        fail = "不能呼叫自己"
    elif not hub.online(peer["id"]):
        fail = "对方当前不在线"
    elif user_calls.get(me) or user_calls.get(peer["id"]):
        fail = "对方正忙" if user_calls.get(peer["id"]) else "你已在通话中"
    else:
        a, b = su.pair(me, peer["id"])
        if not db.query_one("SELECT id FROM friendships WHERE user_a_id=? AND user_b_id=?", (a, b)):
            fail = "对方不是你的好友"
        elif su.is_blacked(me, peer["id"]):
            fail = "你与对方存在拉黑关系"
    if fail:
        await hub.push_to_user_async(me, {"type": "call_fail", "call_id": call_id, "reason": fail})
        return
    calls[call_id] = {"caller": me, "callee": peer["id"], "status": "ringing",
                      "created": time.time(), "answered": None,
                      "media": "video" if str(obj.get("media") or "") == "video" else "audio"}
    user_calls[me] = call_id
    user_calls[peer["id"]] = call_id
    # 来电推给对方（多设备全推；谁先接听以服务端状态机为准）
    await hub.push_to_user_async(peer["id"], {
        "type": "call_invite", "call_id": call_id,
        "media": calls[call_id]["media"], "from": _public(user)})
    # 回铃确认给发起方（前端据此显示"等待接听"）
    await hub.push_to_user_async(me, {"type": "call_ringing", "call_id": call_id})


async def _on_accept(hub, user, obj):
    me = user["id"]
    call_id = str(obj.get("call_id") or "")
    c = calls.get(call_id)
    if not c or me != c["callee"] or c["status"] != "ringing":
        await hub.push_to_user_async(me, {"type": "call_fail", "call_id": call_id,
                                          "reason": "通话不存在或状态已变化"})
        return
    c["status"] = "active"
    c["answered"] = time.time()
    await hub.push_to_user_async(c["caller"], {"type": "call_accept", "call_id": call_id})
    # 接听事件广播给被叫全部连接（多设备：其他设备自动收起来电界面）
    await hub.push_to_user_async(me, {"type": "call_accept", "call_id": call_id})


async def _on_reject(hub, user, obj):
    me = user["id"]
    call_id = str(obj.get("call_id") or "")
    c = calls.get(call_id)
    if not c or me != c["callee"]:
        return
    await hub.push_to_user_async(c["caller"], {"type": "call_reject", "call_id": call_id})
    await _end_and_record(hub, call_id, "rejected")


async def _on_cancel(hub, user, obj):
    me = user["id"]
    call_id = str(obj.get("call_id") or "")
    reason = obj.get("reason") or "user"           # user=主动取消 timeout=无人接听
    c = calls.get(call_id)
    if not c or me != c["caller"]:
        return
    await hub.push_to_user_async(c["callee"], {"type": "call_cancel", "call_id": call_id,
                                               "reason": reason})
    await _end_and_record(hub, call_id, "missed" if reason == "timeout" else "canceled")


async def _on_end(hub, user, obj):
    me = user["id"]
    call_id = str(obj.get("call_id") or "")
    c = calls.get(call_id)
    if not c or me not in (c["caller"], c["callee"]):
        return
    duration = obj.get("duration")
    if not isinstance(duration, (int, float)) or duration < 0:
        duration = _duration_of(c)
    other = c["callee"] if me == c["caller"] else c["caller"]
    await hub.push_to_user_async(other, {"type": "call_end", "call_id": call_id,
                                         "duration": int(duration)})
    await _end_and_record(hub, call_id, "end", int(duration))


async def _on_relay(hub, user, obj, t):
    """offer / answer / ice 纯转发（仅限本通话说得通的两端；ice 容忍 ringing 期竞态）。"""
    me = user["id"]
    call_id = str(obj.get("call_id") or "")
    c = calls.get(call_id)
    if not c or me not in (c["caller"], c["callee"]):
        return
    if t in ("call_offer", "call_answer") and c["status"] != "active":
        return
    other = c["callee"] if me == c["caller"] else c["caller"]
    fwd = {"type": t, "call_id": call_id}
    if t == "call_ice":
        cand = obj.get("candidate")
        if not cand:
            return
        fwd["candidate"] = cand
    else:
        sdp = obj.get("sdp")
        if not sdp:
            return
        fwd["sdp"] = sdp
    await hub.push_to_user_async(other, fwd)


# ---------------- WS 断开（ws_hub finally 钩子调用） ----------------
async def on_ws_disconnected(hub, user_id: int) -> None:
    """任一端信令断开：ringing 期=取消；active 期=按挂断处理（时长按服务端计时兜底）。"""
    call_id = user_calls.get(user_id)
    if not call_id:
        return
    c = calls.get(call_id)
    if not c:
        _cleanup(call_id)
        return
    if c["status"] == "ringing":
        other = c["callee"] if user_id == c["caller"] else c["caller"]
        await hub.push_to_user_async(other, {"type": "call_fail", "call_id": call_id,
                                             "reason": "对方网络已断开"})
        await _end_and_record(hub, call_id, "canceled")
    else:
        other = c["callee"] if user_id == c["caller"] else c["caller"]
        await hub.push_to_user_async(other, {"type": "call_end", "call_id": call_id,
                                             "duration": _duration_of(c), "reason": "network"})
        await _end_and_record(hub, call_id, "end", _duration_of(c))
