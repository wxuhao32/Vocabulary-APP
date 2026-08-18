"""
CET4Prep 社交模块 — 通知路由
未读+最近通知 / 单条已读 / 全部已读 / 未读数（轮询角标）
"""
from fastapi import APIRouter, Depends, HTTPException

import db
from routers.auth import require_user

router = APIRouter(prefix="/api/v1", tags=["social-notification"])


@router.get("/notifications")
def notifications(unread_only: int = 0, offset: int = 0, limit: int = 20,
                  user=Depends(require_user)):
    """通知分页（最新在前）：offset/limit 懒加载；total=该用户通知总数；has_more=是否还有更早的。"""
    offset = max(0, offset)
    limit = max(1, min(limit, 50))
    total = db.query_one(
        "SELECT COUNT(*) n FROM notifications WHERE user_id=? " + ("AND is_read=0" if unread_only else ""),
        (user["id"],))["n"]
    if unread_only:
        rows = db.query(
            "SELECT * FROM notifications WHERE user_id=? AND is_read=0 "
            "ORDER BY id DESC LIMIT ? OFFSET ?", (user["id"], limit, offset))
    else:
        rows = db.query(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user["id"], limit, offset))
    rows = list(rows)
    return {"notifications": [{
        "id": n["id"], "type": n["type"], "title": n["title"], "content": n["content"],
        "related_id": n["related_id"], "related_pid": n["related_pid"] or "",
        "is_read": n["is_read"], "created_at": n["created_at"],
    } for n in rows], "total": total, "has_more": len(rows) >= limit}


@router.get("/notifications/unread-count")
def unread_count(user=Depends(require_user)):
    n = db.query_one("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND is_read=0",
                     (user["id"],))["n"]
    return {"unread": n}


@router.post("/notifications/{notification_id}/read")
def mark_read(notification_id: int, user=Depends(require_user)):
    cur = db.execute(
        "UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?",
        (notification_id, user["id"]))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="通知不存在")
    return {"ok": True}


@router.post("/notifications/read-all")
def mark_all_read(user=Depends(require_user)):
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0", (user["id"],))
    return {"ok": True}


@router.delete("/notifications/{notification_id}")
def delete_notification(notification_id: int, user=Depends(require_user)):
    """v9.96：单条删除通知——仅删除通知记录，不影响好友申请/聊天消息等业务数据。"""
    cur = db.execute(
        "DELETE FROM notifications WHERE id=? AND user_id=?",
        (notification_id, user["id"]))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="通知不存在")
    return {"ok": True, "msg": "已删除该通知"}
