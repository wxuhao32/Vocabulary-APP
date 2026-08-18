"""
CET4Prep 社交模块 — 动态（朋友圈）路由 v9.89
发布文字动态（public / friends / private 可见性）/ 动态流 / 个人主页动态 / 删除自己的动态
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db
import social_util as su
from routers.auth import require_user

router = APIRouter(prefix="/api/v1", tags=["social-moments"])


class MomentReq(BaseModel):
    content: str
    visibility: str = "public"          # public / friends / private


def _moment_dict(m, me_id: int) -> dict:
    author = db.query_one(
        "SELECT id, public_id, username, avatar, gender, signature FROM users WHERE id=?",
        (m["user_id"],))
    return {
        "id": m["id"],
        "content": m["content"],
        "visibility": m["visibility"],
        "created_at": m["created_at"],
        "author": {
            "id": author["public_id"] if author else "",
            "nickname": author["username"] if author else "",
            "avatar": author["avatar"] or "" if author else "",
        },
        "is_mine": m["user_id"] == me_id,
    }


# ---------------- 发布动态 ----------------
@router.post("/moments")
def create_moment(req: MomentReq, user=Depends(require_user)):
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="动态内容不能为空")
    if len(content) > 2000:
        raise HTTPException(status_code=400, detail="动态不能超过 2000 字")
    if req.visibility not in ("public", "friends", "private"):
        raise HTTPException(status_code=400, detail="可见范围不合法")
    cur = db.execute(
        "INSERT INTO moments (user_id, content, visibility) VALUES (?,?,?)",
        (user["id"], content, req.visibility))
    return {"ok": True, "id": cur.lastrowid, "msg": "发布成功"}


# ---------------- 动态流：公开 + 好友可见（互为好友）+ 自己的 ----------------
@router.get("/moments/feed")
def moment_feed(limit: int = 50, user=Depends(require_user)):
    me = user["id"]
    limit = max(1, min(limit, 100))
    rows = db.query(
        """SELECT m.* FROM moments m
           WHERE m.visibility='public'
              OR m.user_id=?
              OR (m.visibility='friends' AND (
                    EXISTS(SELECT 1 FROM friendships f
                           WHERE f.user_a_id=m.user_id AND f.user_b_id=?)
                 OR EXISTS(SELECT 1 FROM friendships f
                           WHERE f.user_a_id=? AND f.user_b_id=m.user_id)))
           ORDER BY m.id DESC LIMIT ?""",
        (me, me, me, limit))
    return {"moments": [_moment_dict(m, me) for m in rows]}


# ---------------- 指定用户可见动态（个人主页） ----------------
@router.get("/users/{pid}/moments")
def user_moments(pid: str, limit: int = 30, user=Depends(require_user)):
    me = user["id"]
    target = db.query_one("SELECT id FROM users WHERE public_id=?", (pid.strip(),))
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    tid = target["id"]
    is_friend = False
    if tid != me:
        a, b = su.pair(me, tid)
        is_friend = db.query_one(
            "SELECT id FROM friendships WHERE user_a_id=? AND user_b_id=?", (a, b)) is not None
    limit = max(1, min(limit, 100))
    if tid == me:
        rows = db.query("SELECT * FROM moments WHERE user_id=? ORDER BY id DESC LIMIT ?", (tid, limit))
    else:
        rows = db.query(
            """SELECT * FROM moments WHERE user_id=?
               AND (visibility='public' OR (visibility='friends' AND ?))
               ORDER BY id DESC LIMIT ?""", (tid, 1 if is_friend else 0, limit))
    return {"moments": [_moment_dict(m, me) for m in rows]}


# ---------------- 修改动态可见范围（仅作者，三种权限互转，v9.90） ----------------
class MomentPatchReq(BaseModel):
    visibility: str


@router.patch("/moments/{moment_id}")
def patch_moment_visibility(moment_id: int, req: MomentPatchReq, user=Depends(require_user)):
    if req.visibility not in ("public", "friends", "private"):
        raise HTTPException(status_code=400, detail="可见范围不合法")
    row = db.query_one("SELECT * FROM moments WHERE id=?", (moment_id,))
    if not row:
        raise HTTPException(status_code=404, detail="动态不存在")
    if row["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="只能修改自己的动态")
    db.execute("UPDATE moments SET visibility=? WHERE id=?", (req.visibility, moment_id))
    return {"ok": True, "visibility": req.visibility, "msg": "可见范围已更新"}


# ---------------- 删除自己的动态 ----------------
@router.delete("/moments/{moment_id}")
def delete_moment(moment_id: int, user=Depends(require_user)):
    row = db.query_one("SELECT * FROM moments WHERE id=?", (moment_id,))
    if not row:
        raise HTTPException(status_code=404, detail="动态不存在")
    if row["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="只能删除自己的动态")
    db.execute("DELETE FROM moments WHERE id=?", (moment_id,))
    return {"ok": True, "msg": "已删除"}
