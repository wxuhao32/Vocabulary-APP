"""
CET4Prep 社交模块 — 私聊消息路由（1对1）
历史分页（游标 before=message_id）/ 发文字消息 / 标记已读 / 轮询新消息 / 撤回（v9.91）
"""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import config
import db
import social_util as su
import ws_hub
from routers.auth import require_user

router = APIRouter(prefix="/api/v1", tags=["social-message"])


class SendMsgReq(BaseModel):
    content: str


def _friend_or_403(me_id: int, friend_public_id: str):
    """发送消息/文件用：校验好友关系 + 黑名单（删好友/拉黑后不能互发）。返回 (friend_row, conversation_id)。"""
    friend = db.query_one("SELECT * FROM users WHERE public_id=?", (friend_public_id.strip(),))
    if not friend:
        raise HTTPException(status_code=404, detail="用户不存在")
    # v9.89：黑名单检查（任一方向拉黑即禁止发送）
    if su.is_blacked(me_id, friend["id"]):
        raise HTTPException(status_code=403, detail="你与对方存在拉黑关系，无法发送消息")
    a, b = su.pair(me_id, friend["id"])
    if not db.query_one("SELECT id FROM friendships WHERE user_a_id=? AND user_b_id=?", (a, b)):
        raise HTTPException(status_code=403, detail="对方不是你的好友，无法发送消息")
    conv_id = su.get_or_create_conversation(me_id, friend["id"])
    return friend, conv_id


def _conv_only(me_id: int, friend_public_id: str):
    """查看历史消息用：只要求存在会话（删除好友后历史记录仍可查看，不创建新会话）。"""
    friend = db.query_one("SELECT * FROM users WHERE public_id=?", (friend_public_id.strip(),))
    if not friend:
        raise HTTPException(status_code=404, detail="用户不存在")
    a, b = su.pair(me_id, friend["id"])
    conv = db.query_one("SELECT id FROM conversations WHERE user_a_id=? AND user_b_id=?", (a, b))
    return friend, (conv["id"] if conv else None)


def _msg_dict(m, me_id: int) -> dict:
    """v9.91：统一消息序列化。撤回消息不返回原文（内容已清空，前端显示撤回提示）。
    v9.112：增加 notified（接收方系统通知栏推送状态，仅诊断用；已读仍以 read 为准）。
    v9.119：可扩展消息类型——voice 附加 duration（秒），其余类型返回基础字段。"""
    d = {
        "id": m["id"],
        "sender": "me" if m["sender_id"] == me_id else "them",
        "type": m["type"],
        "content": "" if m["revoked"] else m["content"],
        "file_id": m["file_id"],
        "revoked": bool(m["revoked"]),
        "created_at": m["created_at"],
        "read": m["read_at"] is not None,
        "notified": bool(m["notified"]) if "notified" in m.keys() else False,
    }
    if m["type"] == "voice":
        # v9.123：兼容两种 content 格式——旧版纯数字("3") 与 新版可读文本("[语音] 3秒")
        c = str(m["content"] or "").strip()
        dur = 0
        try:
            dur = int(c)
        except (TypeError, ValueError):
            mm = re.search(r"(\d+)", c)
            if mm:
                dur = int(mm.group(1))
        d["duration"] = max(1, min(600, dur or 1))
    return d


# ---------------- 历史消息（分页：before 为消息 id 游标，默认最新一页；删好友后仍可查看） ----------------
@router.get("/conversations/{friend_id}/messages")
def list_messages(friend_id: str, before: int | None = None, limit: int = 50,
                  user=Depends(require_user)):
    friend, conv_id = _conv_only(user["id"], friend_id)
    limit = max(1, min(limit, 100))
    if conv_id is None:
        return {"friend": {"id": friend["public_id"], "nickname": friend["username"],
                           "avatar": friend["avatar"] or ""}, "messages": []}
    # 标记已读：对方发给我的消息（v9.91：先标记再查询，本次响应即返回已读状态）
    # v9.110：记录本次被标记已读的消息 id，随后 WS 实时推送"已读回执"给发送方（轮询停用后仍能刷新）
    to_read = [r["id"] for r in db.query(
        "SELECT id FROM messages WHERE conversation_id=? AND sender_id!=? "
        "AND read_at IS NULL AND revoked=0", (conv_id, user["id"]))]
    if to_read:
        db.execute(
            "UPDATE messages SET read_at=datetime('now','localtime') "
            "WHERE conversation_id=? AND sender_id!=? AND read_at IS NULL AND revoked=0",
            (conv_id, user["id"]))
        ws_hub.hub.push_to_user(friend["id"], {
            "type": "read",
            "friend": {"id": user["public_id"], "nickname": user["username"]},
            "ids": to_read,
        })
    if before:
        rows = db.query(
            "SELECT * FROM messages WHERE conversation_id=? AND id<? ORDER BY id DESC LIMIT ?",
            (conv_id, before, limit))
    else:
        rows = db.query(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
            (conv_id, limit))
    rows = list(rows)
    msgs = [_msg_dict(m, user["id"]) for m in reversed(rows)]
    return {"friend": {"id": friend["public_id"], "nickname": friend["username"],
                       "avatar": friend["avatar"] or ""}, "messages": msgs}


# ---------------- 发文字消息 ----------------
@router.post("/conversations/{friend_id}/messages")
def send_message(friend_id: str, req: SendMsgReq, user=Depends(require_user)):
    import time as _t
    _t0 = _t.time()
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息内容不能为空")
    if len(content) > 2000:
        raise HTTPException(status_code=400, detail="消息过长（上限 2000 字）")
    friend, conv_id = _friend_or_403(user["id"], friend_id)
    cur = db.execute(
        "INSERT INTO messages (conversation_id, sender_id, type, content) VALUES (?,?, 'text', ?)",
        (conv_id, user["id"], content))
    _t1 = _t.time()
    db.execute("UPDATE conversations SET last_message_at=datetime('now','localtime') WHERE id=?",
               (conv_id,))
    su.create_notification(friend["id"], "new_message", "新私聊消息",
                           f"{user['username']}：{content[:50]}", related_id=cur.lastrowid,
                           related_pid=user["public_id"])
    # v9.110：数据库落库后，WS 实时推送给接收方（接收方不在线时静默跳过，消息已持久化）
    msg_row = db.query_one("SELECT * FROM messages WHERE id=?", (cur.lastrowid,))
    _t2 = _t.time()
    if msg_row is not None:
        m = _msg_dict(msg_row, user["id"])
        m["sender"] = "them"  # 以接收方视角序列化
        ws_hub.hub.push_to_user(friend["id"], {
            "type": "new_message",
            # v9.110 修复：friend 必须是【发送者】——前端 isChatOpen(fr.id) 用它判断
            # "当前打开的会话对端是否就是发件人"；此前误传接收者导致收消息不实时上屏
            "friend": {"id": user["public_id"], "nickname": user["username"],
                       "avatar": user["avatar"] or ""},
            "message": m,
        })
    _t3 = _t.time()
    # v9.115 诊断：发送链路完整日志（收到→落库→推送，含接收方在线状态与推送结果）
    # 注意：格式串占位符与参数必须一一对应（此前 9 占位 8 参数导致 print 抛异常 → 接口 500）
    _online_before = ws_hub.hub.online(friend["id"])
    print("[CHAT-PERF] SEND uid=%s fid=%s msg_id=%s recv=%.1fms db=%.1fms push=%.1fms total=%.1fms recv_online=%d"
          % (user["id"], friend_id, cur.lastrowid,
             (_t1 - _t0) * 1000, (_t2 - _t1) * 1000, (_t3 - _t2) * 1000, (_t3 - _t0) * 1000,
             1 if _online_before else 0))
    return {"ok": True, "id": cur.lastrowid}


# ---------------- 轮询新消息（返回增量 + 撤回事件 + 已读状态，供前端定时拉取） ----------------
@router.get("/conversations/{friend_id}/messages/poll")
def poll_messages(friend_id: str, after: int = 0, revoked_after: int = 0, user=Depends(require_user)):
    friend, conv_id = _conv_only(user["id"], friend_id)
    if conv_id is None:
        return {"messages": [], "revoked": [], "my_reads": {}}
    # v9.91：先标记对方发来的新消息已读，再查询返回（本次响应即已读）
    # v9.110：已读回执经 WS 实时推送发送方（轮询停用后仍能刷新）
    to_read = [r["id"] for r in db.query(
        "SELECT id FROM messages WHERE conversation_id=? AND sender_id!=? "
        "AND read_at IS NULL AND revoked=0", (conv_id, user["id"]))]
    if to_read:
        db.execute(
            "UPDATE messages SET read_at=datetime('now','localtime') "
            "WHERE conversation_id=? AND sender_id!=? AND read_at IS NULL AND revoked=0",
            (conv_id, user["id"]))
        ws_hub.hub.push_to_user(friend["id"], {
            "type": "read",
            "friend": {"id": user["public_id"], "nickname": user["username"]},
            "ids": to_read,
        })
    rows = db.query(
        "SELECT * FROM messages WHERE conversation_id=? AND id>? ORDER BY id ASC",
        (conv_id, after))
    # v9.91：撤回事件（对方撤回的消息 id 可能已过消息游标，单独返回；前端据此把气泡替换为撤回提示）
    revoked_ids = [r["id"] for r in db.query(
        "SELECT id FROM messages WHERE conversation_id=? AND revoked=1 AND id>? ORDER BY id ASC",
        (conv_id, revoked_after))]
    # v9.91：已读状态增量（最近 20 条我发的消息，对方是否已读）
    my_reads = {}
    for m in db.query(
            "SELECT id, read_at FROM messages WHERE conversation_id=? AND sender_id=? "
            "ORDER BY id DESC LIMIT 20", (conv_id, user["id"])):
        my_reads[m["id"]] = m["read_at"] is not None
    return {"messages": [_msg_dict(m, user["id"]) for m in rows],
            "revoked": revoked_ids, "my_reads": my_reads}


# ---------------- v9.91 撤回消息（仅发送者 + 2 分钟内） ----------------
@router.post("/messages/{msg_id}/revoke")
def revoke_message(msg_id: int, user=Depends(require_user)):
    row = db.query_one("SELECT * FROM messages WHERE id=?", (msg_id,))
    if not row:
        raise HTTPException(status_code=404, detail="消息不存在")
    if row["sender_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="只能撤回自己发送的消息")
    if row["revoked"]:
        raise HTTPException(status_code=409, detail="该消息已撤回")
    try:
        sent_at = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise HTTPException(status_code=500, detail="消息时间异常")
    if (datetime.now() - sent_at).total_seconds() > config.REVOKE_WINDOW_SECONDS:
        raise HTTPException(status_code=403, detail=f"发送超过 {config.REVOKE_WINDOW_SECONDS // 60} 分钟，不能撤回")
    # 撤回：内容清空（原始内容不可恢复），保留 file_id 供文件访问拒绝校验
    db.execute("UPDATE messages SET revoked=1, content='' WHERE id=?", (msg_id,))
    # 会话最后消息时间若指向该消息，回退到上一条（保持列表摘要正确）
    conv = db.query_one("SELECT * FROM conversations WHERE id=?", (row["conversation_id"],))
    if conv:
        prev = db.query_one(
            "SELECT id, created_at FROM messages WHERE conversation_id=? AND id<? "
            "ORDER BY id DESC LIMIT 1",
            (row["conversation_id"], msg_id))
        db.execute("UPDATE conversations SET last_message_at=? WHERE id=?",
                   (prev["created_at"] if prev else conv["created_at"], row["conversation_id"]))
        # v9.110：撤回事件实时推送对方（对方聊天页/列表即时更新，无需等轮询）
        other_id = conv["user_b_id"] if conv["user_a_id"] == user["id"] else conv["user_a_id"]
        other = db.query_one("SELECT public_id, username FROM users WHERE id=?", (other_id,))
        if other:
            ws_hub.hub.push_to_user(other_id, {
                "type": "revoked",
                "friend": {"id": other["public_id"], "nickname": other["username"]},
                "ids": [msg_id],
            })
    return {"ok": True, "msg": "已撤回"}


# ---------------- v9.96 删除消息（单条/批量）+ 清空聊天 ----------------
class DeleteMsgsReq(BaseModel):
    ids: list[int] = []


def _refresh_conv_summary(conv_id: int):
    """删除/清空后回退会话最后消息时间与未读状态（列表摘要保持正确）。"""
    prev = db.query_one(
        "SELECT id, created_at FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
        (conv_id,))
    if prev:
        db.execute("UPDATE conversations SET last_message_at=? WHERE id=?",
                   (prev["created_at"], conv_id))
    else:
        conv = db.query_one("SELECT * FROM conversations WHERE id=?", (conv_id,))
        if conv:
            db.execute("UPDATE conversations SET last_message_at=? WHERE id=?",
                       (conv["created_at"], conv_id))
        db.execute("UPDATE messages SET read_at=datetime('now','localtime') "
                   "WHERE conversation_id=? AND read_at IS NULL", (conv_id,))


@router.post("/conversations/{friend_id}/messages/delete")
def delete_messages(friend_id: str, req: DeleteMsgsReq, user=Depends(require_user)):
    """删除选中的消息（单条/多条）。只删消息记录，不影响好友关系与文件（历史文件仍可下载）。"""
    friend, conv_id = _conv_only(user["id"], friend_id)
    if conv_id is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    ids = [i for i in req.ids if i]
    if not ids:
        raise HTTPException(status_code=400, detail="请选择要删除的消息")
    ph = ",".join("?" * len(ids))
    rows = db.query(f"SELECT id FROM messages WHERE conversation_id=? AND id IN ({ph})",
                    (conv_id, *ids))
    valid = [r["id"] for r in rows]
    if not valid:
        raise HTTPException(status_code=404, detail="消息不存在或不属于该会话")
    db.execute(f"DELETE FROM messages WHERE id IN ({ph})", tuple(valid))
    _refresh_conv_summary(conv_id)
    # v9.110：删除事件实时推送对方（对方聊天页同步移除，避免轮询停用后残留）
    ws_hub.hub.push_to_user(friend["id"], {
        "type": "deleted",
        "friend": {"id": user["public_id"], "nickname": user["username"]},
        "ids": valid,
    })
    return {"ok": True, "msg": f"已删除 {len(valid)} 条消息"}


@router.post("/conversations/{friend_id}/clear")
def clear_messages(friend_id: str, user=Depends(require_user)):
    """清空整个会话的聊天记录（二次确认由前端负责）。不影响好友关系。"""
    friend, conv_id = _conv_only(user["id"], friend_id)
    if conv_id is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    n = db.query_one("SELECT COUNT(*) c FROM messages WHERE conversation_id=?", (conv_id,))["c"]
    db.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
    _refresh_conv_summary(conv_id)
    # v9.110：清空事件实时推送对方（对方聊天页同步清空）
    ws_hub.hub.push_to_user(friend["id"], {
        "type": "cleared",
        "friend": {"id": user["public_id"], "nickname": user["username"]},
    })
    return {"ok": True, "msg": f"已清空聊天记录（{n} 条）"}


# ---------------- v9.112 系统通知栏推送状态（Android NotifyService 原生消费，SQLite 持久化幂等） ----------------
# 职责：NotifyService 启动/WS 重连后拉取"发给我的、尚未推送系统通知"的消息补发通知，
#       发完通过 ack 标记 notified=1 → 无论 WebSocket 重连、App 重启、历史同步都不会重复推送。
# 注意：notified（已推送通知）与 read_at（已读）是两个独立状态；
#       v9.117：pending 额外排除 read_at IS NOT NULL —— 用户已读的消息不再补发系统通知
#       （已读 = 用户已看到，避免"通知中心未全部已读就反复通知历史消息"）。
@router.get("/notify/pending")
def notify_pending(after: int = 0, limit: int = 50, user=Depends(require_user)):
    """返回发送给我、尚未产生系统通知栏推送且未读的消息（notified=0 AND read_at IS NULL，
    已撤回除外），按 id 升序。供 NotifyService 启动/重连后补发离线期间未通知的消息。"""
    limit = max(1, min(limit, 100))
    # v9.118：会话成员过滤（防跨用户泄漏）——只返回「我参与的会话」里别人发我的消息，
    #          否则全库其他会话的未读消息会混入我的 pending，导致错误系统通知 + 隐私泄漏。
    rows = db.query(
        "SELECT * FROM messages WHERE sender_id!=? AND notified=0 AND revoked=0 "
        "AND read_at IS NULL AND id>? "
        "AND conversation_id IN (SELECT id FROM conversations WHERE user_a_id=? OR user_b_id=?) "
        "ORDER BY id ASC LIMIT ?",
        (user["id"], after, user["id"], user["id"], limit))
    out = []
    for m in rows:
        conv = db.query_one("SELECT user_a_id, user_b_id FROM conversations WHERE id=?",
                            (m["conversation_id"],))
        friend_id = None
        if conv:
            friend_id = conv["user_b_id"] if conv["user_a_id"] == user["id"] else conv["user_a_id"]
        friend = db.query_one("SELECT public_id, username, avatar FROM users WHERE id=?",
                              (friend_id,)) if friend_id else None
        out.append({
            "id": m["id"], "type": m["type"], "content": m["content"],
            "file_id": m["file_id"], "created_at": m["created_at"],
            "friend": {"id": friend["public_id"] if friend else "",
                       "nickname": friend["username"] if friend else "好友",
                       "avatar": (friend["avatar"] or "") if friend else ""},
        })
    # v9.114 诊断：pending 返回记录（确认哪些消息被补发、是否含已 notified）
    print("[NOTIFY] pending uid=%s after=%s limit=%s returned=%s ids=%s"
          % (user["id"], after, limit, len(out), [m["id"] for m in rows]))
    return {"messages": out}


class NotifyAckReq(BaseModel):
    ids: list[int] = []


@router.post("/notify/ack")
def notify_ack(req: NotifyAckReq, user=Depends(require_user)):
    """NotifyService 发出系统通知后确认：把消息标记 notified=1（SQLite 持久化，幂等防重复推送）。
    只能确认「别人发给我」且未撤回的消息；重复 ack 无害。"""
    ids = [i for i in req.ids if i]
    if not ids:
        return {"ok": True, "acked": 0}
    ph = ",".join("?" * len(ids))
    # v9.118：ack 同样限定「我参与的会话」，防止误标记他人会话消息（防泄漏 + 防误 ack）
    cur = db.execute(
        f"UPDATE messages SET notified=1 WHERE sender_id!=? AND revoked=0 AND notified=0 "
        f"AND id IN ({ph}) "
        f"AND conversation_id IN (SELECT id FROM conversations WHERE user_a_id=? OR user_b_id=?)",
        (user["id"], *ids, user["id"], user["id"]))
    # v9.114 诊断：ack 结果记录（acked<请求数 = 部分已 notified/非接收方 → 排查重复通知）
    print("[NOTIFY] ack uid=%s req_ids=%s acked=%s"
          % (user["id"], ids, cur.rowcount))
    return {"ok": True, "acked": cur.rowcount}


# ---------------- v9.114 诊断：客户端/原生日志上报 ----------------
class DebugLogReq(BaseModel):
    tag: str = ""
    msg: str = ""


@router.post("/debug/log")
def debug_log(req: DebugLogReq, user=Depends(require_user)):
    """日志诊断模式：接收 App 前端 / 原生服务上报的调试日志，服务端仅 print 不落库。
    便于在服务器日志中还原真机消息生命周期/通知/键盘事件。"""
    import datetime
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print("[CLIENT-LOG] %s uid=%s %s: %s"
          % (ts, user["id"], (req.tag or "")[:32], (req.msg or "")[:500]))
    return {"ok": True}
