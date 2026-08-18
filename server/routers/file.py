"""
CET4Prep 社交模块 — 文件路由
上传（multipart，限 20MB）/ 下载（Bearer 鉴权，fetch-blob 方式，图片可预览）
"""
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import config
import db
import social_util as su
import ws_hub
from routers.auth import require_user
from routers.message import _friend_or_403, _msg_dict

router = APIRouter(prefix="/api/v1", tags=["social-file"])

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB

# v9.119：可扩展消息类型白名单——新增消息类型（如 video）只需在此追加。
# 语音消息复用文件上传/下载/推送链路，仅类型与序列化字段不同（开闭原则）。
MSG_TYPE_ALLOWED = {"file", "voice"}

FILE_DIR = config.DATA_DIR / "files"


def _save_upload(data: bytes, filename: str) -> tuple[str, Path]:
    """返回 (相对文件名, 绝对路径)。文件名随机化防路径穿越。"""
    FILE_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(filename).suffix[:12]
    name = uuid.uuid4().hex + ext
    path = FILE_DIR / name
    path.write_bytes(data)
    return name, path


@router.post("/conversations/{friend_id}/files")
async def upload_file(friend_id: str, file: UploadFile = File(...), msg_type: str = Form("file"),
                      duration: int = Form(0), user=Depends(require_user)):
    """上传文件消息（默认 file）。v9.119：支持 msg_type=voice（语音消息）——
    content 存时长秒数（可扩展消息类型，其余链路与 file 完全一致）。"""
    msg_type = (msg_type or "file").strip().lower()
    if msg_type not in MSG_TYPE_ALLOWED:
        raise HTTPException(status_code=400, detail="不支持的消息类型")
    friend, conv_id = _friend_or_403(user["id"], friend_id)
    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(raw) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件超过 20MB 限制")
    storage_name, path = _save_upload(raw, file.filename or "unnamed")
    cur = db.execute(
        "INSERT INTO files (owner_id, conversation_id, filename, mime, size, storage_path) "
        "VALUES (?,?,?,?,?,?)",
        (user["id"], conv_id, (file.filename or "unnamed")[:120],
         file.content_type or "application/octet-stream", len(raw), storage_name))
    # 消息内容：file → "[文件] 名"；voice → "[语音] N秒"（v9.123：可读文本——
    #   旧版前端（无语音条分支）也能正确显示"语音 3秒"而非裸数字；新版解析时长显示语音条）
    if msg_type == "voice":
        content = "[语音] %d秒" % max(1, min(600, duration))
    else:
        content = f"[文件] {(file.filename or 'unnamed')[:80]}"
    mcur = db.execute(
        "INSERT INTO messages (conversation_id, sender_id, type, content, file_id) "
        "VALUES (?,?,?,?,?)",
        (conv_id, user["id"], msg_type, content, cur.lastrowid))
    db.execute("UPDATE conversations SET last_message_at=datetime('now','localtime') WHERE id=?",
               (conv_id,))
    su.create_notification(friend["id"], "file_message", "收到文件",
                           f"{user['username']} 发送了文件「{(file.filename or 'unnamed')[:40]}」",
                           related_id=mcur.lastrowid, related_pid=user["public_id"])
    # v9.110：文件消息落库后实时推送接收方
    msg_row = db.query_one("SELECT * FROM messages WHERE id=?", (mcur.lastrowid,))
    if msg_row is not None:
        m = _msg_dict(msg_row, user["id"])
        m["sender"] = "them"
        ws_hub.hub.push_to_user(friend["id"], {
            "type": "new_message",
            # v9.110 修复：friend 必须为【发送者】（与文字消息一致），否则前端 isChatOpen 误判不渲染
            "friend": {"id": user["public_id"], "nickname": user["username"],
                       "avatar": user["avatar"] or ""},
            "message": m,
        })
    return {"ok": True, "file_id": cur.lastrowid, "message_id": mcur.lastrowid,
            "filename": file.filename, "size": len(raw)}


@router.get("/files/{file_id}/download")
def download_file(file_id: int, user=Depends(require_user)):
    row = db.query_one("SELECT * FROM files WHERE id=?", (file_id,))
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在")
    # 鉴权：仅会话双方可下载
    conv = db.query_one("SELECT * FROM conversations WHERE id=?", (row["conversation_id"],))
    if not conv or user["id"] not in (conv["user_a_id"], conv["user_b_id"]):
        raise HTTPException(status_code=403, detail="无权访问该文件")
    # v9.91：文件对应消息已撤回 → 禁止继续访问
    msg = db.query_one("SELECT id, revoked FROM messages WHERE file_id=? ORDER BY id DESC LIMIT 1",
                       (file_id,))
    if msg and msg["revoked"]:
        raise HTTPException(status_code=403, detail="该文件已被发送者撤回")
    path = FILE_DIR / row["storage_path"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已丢失")
    return FileResponse(path, filename=row["filename"], media_type=row["mime"] or "application/octet-stream")
