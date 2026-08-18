"""
CET4Prep 社交模块 — 好友路由
搜索用户 / 好友申请(7 项检查) / 同意/拒绝/取消 / 好友列表 / 删除好友
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db
import social_util as su
import ws_hub
from routers.auth import require_user

router = APIRouter(prefix="/api/v1", tags=["social-friendship"])


class FriendRequestReq(BaseModel):
    target_user_id: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_id_of(row) -> str:
    return row["public_id"]


# ---------------- 搜索用户（登录必选，只返回公开信息） ----------------
@router.get("/users/search")
def search_user(id: str, user=Depends(require_user)):
    if not id or not id.strip():
        raise HTTPException(status_code=400, detail="请输入要搜索的用户 ID")
    row = db.query_one("SELECT * FROM users WHERE public_id=?", (id.strip(),))
    if not row:
        raise HTTPException(status_code=404, detail="未找到该用户")
    return {"user": su.public_user_dict(row, user["id"])}


# ---------------- 发送好友申请 ----------------
@router.post("/friends/requests")
def send_request(req: FriendRequestReq, user=Depends(require_user)):
    target_user_id = req.target_user_id.strip()
    me_id = user["id"]
    target = db.query_one("SELECT * FROM users WHERE public_id=?", (target_user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="目标用户不存在")
    target_id = target["id"]
    # 1. 不能添加自己
    if target_id == me_id:
        raise HTTPException(status_code=400, detail="不能添加自己为好友")
    # 1.5 v9.89：黑名单检查（任一方向拉黑即禁止申请）
    if su.is_blacked(me_id, target_id):
        raise HTTPException(status_code=409, detail="无法发送申请：你与对方存在拉黑关系")
    # 2. 是否已经是好友
    a, b = su.pair(me_id, target_id)
    if db.query_one("SELECT id FROM friendships WHERE user_a_id=? AND user_b_id=?", (a, b)):
        raise HTTPException(status_code=409, detail="你们已经是好友")
    # 3. 是否存在我发给他 / 他发给我的 pending（双向视为同一关系）
    if db.query_one(
            "SELECT id FROM friend_requests WHERE status='pending' AND "
            "((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))",
            (me_id, target_id, target_id, me_id)):
        raise HTTPException(status_code=409, detail="已存在待处理的好友申请")
    cur = db.execute(
        "INSERT INTO friend_requests (sender_id, receiver_id, status) VALUES (?,?, 'pending')",
        (me_id, target_id))
    # 通知对方
    nid = su.create_notification(
        target_id, "friend_request", "新的好友申请",
        f"{user['username']} 向你发送了好友申请", related_id=cur.lastrowid,
        related_pid=user["public_id"])
    # v9.110：好友申请实时推送对方（通知栏/角标即时刷新）
    ws_hub.hub.push_to_user(target_id, {
        "type": "notification",
        "notification": {"id": nid, "type": "friend_request", "title": "新的好友申请",
                         "content": f"{user['username']} 向你发送了好友申请",
                         "related_id": cur.lastrowid, "related_pid": user["public_id"],
                         "is_read": 0},
    })
    return {"ok": True, "msg": "好友申请已发送"}


# ---------------- 收到的 / 发出的 申请列表 ----------------
# v9.89：status 支持 all（含已处理的历史），前端申请中心按状态分组展示
@router.get("/friends/requests/received")
def received_requests(status: str = "pending", user=Depends(require_user)):
    if status not in ("pending", "all"):
        status = "pending"
    cond = "" if status == "all" else "AND fr.status='pending'"
    rows = db.query(
        f"""SELECT fr.id, fr.sender_id, fr.status, fr.created_at, u.public_id, u.username, u.avatar
           FROM friend_requests fr JOIN users u ON u.id=fr.sender_id
           WHERE fr.receiver_id=? {cond}
           ORDER BY fr.created_at DESC""", (user["id"],))
    return {"requests": [
        {"id": r["id"], "sender": {"id": r["public_id"], "nickname": r["username"],
                                    "avatar": r["avatar"] or ""},
         "status": r["status"], "created_at": r["created_at"]} for r in rows]}


@router.get("/friends/requests/sent")
def sent_requests(status: str = "pending", user=Depends(require_user)):
    if status not in ("pending", "all"):
        status = "pending"
    cond = "" if status == "all" else "AND fr.status='pending'"
    rows = db.query(
        f"""SELECT fr.id, fr.receiver_id, fr.status, fr.created_at, u.public_id, u.username, u.avatar
           FROM friend_requests fr JOIN users u ON u.id=fr.receiver_id
           WHERE fr.sender_id=? {cond}
           ORDER BY fr.created_at DESC""", (user["id"],))
    return {"requests": [
        {"id": r["id"], "receiver": {"id": r["public_id"], "nickname": r["username"],
                                      "avatar": r["avatar"] or ""},
         "status": r["status"], "created_at": r["created_at"]} for r in rows]}


# ---------------- 同意 / 拒绝（必须 receiver） ----------------
def _own_pending_request(request_id: int, me_id: int):
    row = db.query_one("SELECT * FROM friend_requests WHERE id=?", (request_id,))
    if not row:
        raise HTTPException(status_code=404, detail="申请不存在")
    if row["receiver_id"] != me_id:
        raise HTTPException(status_code=403, detail="无权操作他人收到的申请")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail="该申请已处理")
    return row


@router.post("/friends/requests/{request_id}/accept")
def accept_request(request_id: int, user=Depends(require_user)):
    req = _own_pending_request(request_id, user["id"])
    sender_id = req["sender_id"]
    db.execute("UPDATE friend_requests SET status='accepted', updated_at=? WHERE id=?",
               (_now(), request_id))
    a, b = su.pair(sender_id, user["id"])
    try:
        db.execute("INSERT INTO friendships (user_a_id, user_b_id) VALUES (?,?)", (a, b))
    except Exception:
        pass  # 唯一约束兜底（理论上前面已校验）
    # 通知发送方
    nid = su.create_notification(sender_id, "friend_request_accepted", "好友申请已通过",
                                 f"{user['username']} 接受了你的好友申请", related_id=request_id,
                                 related_pid=user["public_id"])
    # v9.110：申请通过实时推送发送方（好友列表即时刷新）
    sender = db.query_one("SELECT public_id, username FROM users WHERE id=?", (sender_id,))
    if sender:
        ws_hub.hub.push_to_user(sender_id, {
            "type": "notification",
            "notification": {"id": nid, "type": "friend_request_accepted", "title": "好友申请已通过",
                             "content": f"{user['username']} 接受了你的好友申请",
                             "related_id": request_id, "related_pid": user["public_id"],
                             "is_read": 0},
        })
    return {"ok": True, "msg": "已同意，你们现在是好友了"}


@router.post("/friends/requests/{request_id}/reject")
def reject_request(request_id: int, user=Depends(require_user)):
    _own_pending_request(request_id, user["id"])
    db.execute("UPDATE friend_requests SET status='rejected', updated_at=? WHERE id=?",
               (_now(), request_id))
    return {"ok": True, "msg": "已拒绝该申请"}


@router.post("/friends/requests/{request_id}/cancel")
def cancel_request(request_id: int, user=Depends(require_user)):
    row = db.query_one("SELECT * FROM friend_requests WHERE id=?", (request_id,))
    if not row or row["sender_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail="该申请已处理")
    db.execute("UPDATE friend_requests SET status='cancelled', updated_at=? WHERE id=?",
               (_now(), request_id))
    return {"ok": True, "msg": "已撤回申请"}


# ---------------- 好友列表（最后消息 + 未读数，按最后消息时间倒序） ----------------
@router.get("/friends")
def friends_list(user=Depends(require_user)):
    me = user["id"]
    rows = db.query(
        """SELECT f.user_a_id, f.user_b_id, f.created_at, u.id AS uid, u.public_id, u.username, u.avatar
           FROM friendships f
           JOIN users u ON u.id = CASE WHEN f.user_a_id=? THEN f.user_b_id ELSE f.user_a_id END
           WHERE f.user_a_id=? OR f.user_b_id=?""",
        (me, me, me))
    out = []
    for r in rows:
        friend_id = r["uid"]
        a, b = su.pair(me, friend_id)
        conv = db.query_one("SELECT id, last_message_at FROM conversations WHERE user_a_id=? AND user_b_id=?",
                            (a, b))
        unread = 0
        last_msg = None
        if conv:
            last_msg = db.query_one(
                "SELECT content, type, revoked, sender_id FROM messages "
                "WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
                (conv["id"],))
            # v9.91：撤回消息不计入未读
            unread = db.query_one(
                "SELECT COUNT(*) n FROM messages WHERE conversation_id=? AND sender_id!=? "
                "AND read_at IS NULL AND revoked=0",
                (conv["id"], me))["n"]
        last_txt = ""
        if last_msg:
            if last_msg["revoked"]:
                last_txt = "你撤回了一条消息" if last_msg["sender_id"] == me else "对方撤回了一条消息"
            else:
                last_txt = last_msg["content"] or ""
        out.append({
            "id": r["public_id"],
            "nickname": r["username"],
            "avatar": r["avatar"] or "",
            "became_friends_at": r["created_at"],
            "last_message": last_txt,
            "last_message_type": (last_msg["type"] or "") if last_msg else "",
            "last_message_at": (conv["last_message_at"] or r["created_at"]) if conv else r["created_at"],
            "unread_count": unread,
        })
    out.sort(key=lambda x: x["last_message_at"] or "", reverse=True)
    return {"friends": out}


# ---------------- 删除好友（保留历史消息；双方停止互发新消息） ----------------
@router.delete("/friends/{friend_id}")
def remove_friend(friend_id: str, user=Depends(require_user)):
    target = db.query_one("SELECT id FROM users WHERE public_id=?", (friend_id.strip(),))
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    a, b = su.pair(user["id"], target["id"])
    cur = db.execute("DELETE FROM friendships WHERE user_a_id=? AND user_b_id=?", (a, b))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="你们不是好友")
    # 历史消息保留（conversation/messages 不动）；后续消息发送会因无好友关系被拒绝
    # v9.110：删除好友实时通知对方（对方好友列表即时移除）
    ws_hub.hub.push_to_user(target["id"], {
        "type": "friend_removed",
        "friend": {"id": user["public_id"], "nickname": user["username"]},
    })
    return {"ok": True, "msg": "已删除好友（历史聊天记录已保留）"}


# ---------------- v9.89 个人主页（公开资料 + 关系状态） ----------------
@router.get("/users/{pid}/profile")
def user_profile(pid: str, user=Depends(require_user)):
    row = db.query_one("SELECT * FROM users WHERE public_id=?", (pid.strip(),))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    d = su.public_user_dict(row, user["id"])
    d["moment_count"] = db.query_one(
        "SELECT COUNT(*) n FROM moments WHERE user_id=?", (row["id"],))["n"]
    # v9.109：学习数据卡片（仅公开允许的信息：今日词数/今日时长/总时长）
    today = datetime.now().strftime("%Y-%m-%d")
    today_row = db.query_one(
        "SELECT word_count, duration_sec FROM study_daily WHERE user_id=? AND date=?",
        (row["id"], today))
    total_row = db.query_one(
        "SELECT COALESCE(SUM(duration_sec),0) d FROM study_daily WHERE user_id=?", (row["id"],))
    d["study"] = {
        "today_words": today_row["word_count"] if today_row else 0,
        "today_duration": today_row["duration_sec"] if today_row else 0,
        "total_duration": total_row["d"] if total_row else 0,
    }
    # v9.109：主页背景 + 点赞（liked_by_me 仅本人视角，不暴露点赞者列表）
    bg_row = db.query_one("SELECT bg FROM profile_bgs WHERE user_id=?", (row["id"],))
    like_cnt = db.query_one("SELECT COUNT(*) n FROM bg_likes WHERE target_id=?", (row["id"],))["n"]
    liked = db.query_one(
        "SELECT id FROM bg_likes WHERE target_id=? AND liker_id=?", (row["id"], user["id"]))
    d["profile_bg"] = bg_row["bg"] if bg_row else ""
    d["bg_like_count"] = like_cnt
    d["bg_liked_by_me"] = liked is not None
    return {"user": d}


# ---------------- v9.109 主页背景设置 / 点赞 ----------------
class SetBgReq(BaseModel):
    bg: str = ""                # dataURL；传空 = 恢复默认背景


@router.post("/users/{pid}/bg")
def set_profile_bg(pid: str, req: SetBgReq, user=Depends(require_user)):
    """本人设置主页背景（dataURL，限制 8MB）。更换背景时清空该背景的点赞（旧背景点赞不带到新背景）。"""
    row = db.query_one("SELECT id FROM users WHERE public_id=?", (pid.strip(),))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["id"] != user["id"]:
        raise HTTPException(status_code=403, detail="只能修改自己的主页背景")
    bg = (req.bg or "").strip()
    if len(bg) > 8_000_000:
        raise HTTPException(status_code=400, detail="背景图片过大，请选择 8MB 以内")
    if not bg:
        db.execute("DELETE FROM profile_bgs WHERE user_id=?", (user["id"],))
    else:
        db.execute(
            "INSERT INTO profile_bgs (user_id, bg) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET bg=excluded.bg, updated_at=datetime('now','localtime')",
            (user["id"], bg))
        # v9.109：更换背景 → 旧背景点赞作废（新背景从 0 开始）
        db.execute("DELETE FROM bg_likes WHERE target_id=?", (user["id"],))
    return {"ok": True, "msg": "主页背景已更新"}


@router.post("/users/{pid}/bg/like")
def toggle_bg_like(pid: str, user=Depends(require_user)):
    """点赞/取消点赞对方主页背景（每人一次，重复点击取消；防重复由 UNIQUE 保证）。"""
    row = db.query_one("SELECT id FROM users WHERE public_id=?", (pid.strip(),))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    target_id, me_id = row["id"], user["id"]
    if target_id == me_id:
        raise HTTPException(status_code=400, detail="不能给自己的主页背景点赞")
    if not db.query_one("SELECT id FROM profile_bgs WHERE user_id=?", (target_id,)):
        raise HTTPException(status_code=400, detail="对方未设置主页背景")
    cur = db.execute("DELETE FROM bg_likes WHERE target_id=? AND liker_id=?", (target_id, me_id))
    if cur.rowcount > 0:
        # 取消点赞（不产生通知）
        liked = False
    else:
        db.execute("INSERT INTO bg_likes (target_id, liker_id) VALUES (?,?)", (target_id, me_id))
        liked = True
        # v9.109：点赞通知（好友/陌生人都发；related_pid=点赞者公开 ID，前端跳转其主页）
        liker = db.query_one("SELECT username FROM users WHERE id=?", (me_id,))
        nid = su.create_notification(target_id, "bg_like",
                                     "%s 点赞了你的主页背景" % (liker["username"] if liker else "有人"),
                                     content="", related_pid=user["public_id"])
        # v9.110：点赞通知实时推送（通知角标即时刷新）
        ws_hub.hub.push_to_user(target_id, {
            "type": "notification",
            "notification": {"id": nid, "type": "bg_like",
                             "title": "%s 点赞了你的主页背景" % (liker["username"] if liker else "有人"),
                             "content": "", "related_id": None,
                             "related_pid": user["public_id"], "is_read": 0},
        })
    cnt = db.query_one("SELECT COUNT(*) n FROM bg_likes WHERE target_id=?", (target_id,))["n"]
    return {"ok": True, "liked": liked, "like_count": cnt}


# ---------------- v9.89 黑名单 ----------------
@router.get("/blacklist")
def blacklist(user=Depends(require_user)):
    me = user["id"]
    rows = db.query(
        """SELECT b.blocked_id, b.created_at, u.public_id, u.username, u.avatar
           FROM blacklists b JOIN users u ON u.id=b.blocked_id
           WHERE b.blocker_id=?
           ORDER BY b.created_at DESC""", (me,))
    return {"blacklist": [{
        "id": r["public_id"], "nickname": r["username"], "avatar": r["avatar"] or "",
        "blocked_at": r["created_at"],
    } for r in rows]}


@router.post("/blacklist/{target_id}")
def block_user(target_id: str, user=Depends(require_user)):
    me = user["id"]
    target = db.query_one("SELECT * FROM users WHERE public_id=?", (target_id.strip(),))
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    tid = target["id"]
    if tid == me:
        raise HTTPException(status_code=400, detail="不能拉黑自己")
    if db.query_one("SELECT id FROM blacklists WHERE blocker_id=? AND blocked_id=?", (me, tid)):
        return {"ok": True, "msg": "该用户已在你的黑名单中"}
    db.execute("INSERT INTO blacklists (blocker_id, blocked_id) VALUES (?,?)", (me, tid))
    # 拉黑即解除好友关系（历史聊天保留）
    a, b = su.pair(me, tid)
    db.execute("DELETE FROM friendships WHERE user_a_id=? AND user_b_id=?", (a, b))
    # 双方之间的 pending 申请一并作废
    db.execute(
        "UPDATE friend_requests SET status='cancelled', updated_at=? "
        "WHERE status='pending' AND ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))",
        (_now(), me, tid, tid, me))
    # v9.110：拉黑实时通知对方（对方好友列表即时移除）
    ws_hub.hub.push_to_user(tid, {
        "type": "friend_removed",
        "friend": {"id": user["public_id"], "nickname": user["username"]},
    })
    return {"ok": True, "msg": "已拉黑，并解除好友关系"}


@router.delete("/blacklist/{target_id}")
def unblock_user(target_id: str, user=Depends(require_user)):
    me = user["id"]
    target = db.query_one("SELECT id FROM users WHERE public_id=?", (target_id.strip(),))
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    cur = db.execute("DELETE FROM blacklists WHERE blocker_id=? AND blocked_id=?",
                     (me, target["id"]))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="该用户不在你的黑名单中")
    return {"ok": True, "msg": "已取消拉黑"}
