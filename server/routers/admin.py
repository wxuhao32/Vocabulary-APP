"""
CET4Prep 管理员系统（v9.93）—— 与普通用户体系完全独立

- 预置管理员：仅系统初始化创建（Administer），不经过普通注册流程
- 隐藏式双重认证：第一重（账号+密码）→ 第二重（独立安全密钥），全部服务端哈希校验
- 普通登录（/api/auth/login）只查 users 表，绝不查 admins 表——同名普通账号无管理员权限
- require_admin：admin_token（独立 JWT 类型）校验，普通用户 access_token 一律拒绝
"""
import logging
import os
import secrets
import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config
import db
import security
import stores

router = APIRouter(prefix="/api/admin", tags=["admin"])

logger = logging.getLogger("app")

# ---------------- 预置管理员（初始化阶段调用） ----------------
def init_admin() -> None:
    """服务端预置唯一管理员。凭证不硬编码：环境变量优先，否则随机生成写入文件。"""
    if db.query_one("SELECT id FROM admins"):
        return
    passwd = os.environ.get("VOCAB_ADMIN_PASS") or secrets.token_urlsafe(12)
    secret = os.environ.get("VOCAB_ADMIN_SECRET") or secrets.token_urlsafe(16)
    db.execute(
        "INSERT INTO admins (username, pass_hash, secret_hash) VALUES (?,?,?)",
        (config.ADMIN_USERNAME, security.hash_password(passwd), security.hash_password(secret)))
    try:
        config.ADMIN_CRED_FILE.write_text(
            f"管理员用户名: {config.ADMIN_USERNAME}\n"
            f"第一重密码: {passwd}\n"
            f"第二重安全密钥: {secret}\n",
            encoding="utf-8")
        logger.warning("管理员已预置，凭证已写入 %s（请妥善保管，勿提交到仓库）", config.ADMIN_CRED_FILE)
    except Exception as e:
        logger.warning("管理员已预置（凭证文件写入失败: %s）", e)


# ---------------- 双重认证（含失败限流） ----------------
ADMIN_MAX_FAILS = 5
ADMIN_LOCK_SECONDS = 15 * 60


def _fail_admin(username: str) -> None:
    if not username:
        return
    key = f"admin_fail:{username}"
    n = int(stores.store.get(key) or 0) + 1
    stores.store.set(key, str(n), ADMIN_LOCK_SECONDS)
    if n >= ADMIN_MAX_FAILS:
        stores.store.delete(key)
        stores.store.set(f"admin_lock:{username}", "1", ADMIN_LOCK_SECONDS)


def _admin_lock_ttl(username: str) -> int:
    return stores.store.ttl(f"admin_lock:{username}")


class Step1Req(BaseModel):
    username: str
    password: str


class Step2Req(BaseModel):
    step1_token: str
    secret_key: str


class BanReq(BaseModel):
    reason: str = "违规行为"
    duration: str = "永久"        # v9.95：1天/3天/7天/30天/180天/365天/自定义/永久
    custom_until: str = ""        # v9.95：选择「自定义」时的具体解封日期时间（YYYY-MM-DDTHH:MM）


_DURATION_DAYS = {"1天": 1, "3天": 3, "7天": 7, "30天": 30, "180天": 180, "365天": 365}


def _compute_ban_until(req: BanReq) -> tuple[str, str]:
    """根据时长/自定义日期计算解封时间。返回 (ban_until_str, duration_desc)。"""
    duration = (req.duration or "永久").strip()
    if duration == "永久" or duration not in _DURATION_DAYS and duration != "自定义":
        return "", "永久"
    if duration == "自定义":
        raw = (req.custom_until or "").strip()
        if not raw:
            raise HTTPException(status_code=400, detail="自定义封禁需填写解封日期时间")
        try:
            dt = datetime.datetime.strptime(raw.replace("T", " ")[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            raise HTTPException(status_code=400, detail="解封时间格式不正确")
        if dt <= datetime.datetime.now():
            raise HTTPException(status_code=400, detail="解封时间必须晚于当前时间")
        return dt.strftime("%Y-%m-%d %H:%M:%S"), "自定义"
    days = _DURATION_DAYS[duration]
    until = datetime.datetime.now() + datetime.timedelta(days=days)
    return until.strftime("%Y-%m-%d %H:%M:%S"), duration


@router.post("/auth/step1")
def admin_step1(req: Step1Req):
    """第一重认证：管理员账号 + 第一重凭证。失败不返回任何第二重信息。"""
    uname = (req.username or "").strip()
    lock = _admin_lock_ttl(uname)
    if lock > 0:
        raise HTTPException(status_code=429, detail=f"尝试过于频繁，请 {lock} 秒后再试")
    row = db.query_one("SELECT * FROM admins WHERE username=?", (uname,))
    if not row or not security.verify_password(row["pass_hash"], req.password or ""):
        _fail_admin(uname)
        raise HTTPException(status_code=401, detail="管理员账号或第一重凭证错误")
    stores.store.delete(f"admin_fail:{uname}")
    stores.store.delete(f"admin_lock:{uname}")
    return {"need_secret": True, "step1_token": security.create_admin_step1_token(row["id"])}


@router.post("/auth/step2")
def admin_step2(req: Step2Req):
    """第二重认证：独立安全密钥。两重全部通过才发 admin_token。"""
    try:
        payload = security.decode_token(req.step1_token, "admin_step1")
        admin_id = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="第一重认证已过期，请重新认证")
    row = db.query_one("SELECT * FROM admins WHERE id=?", (admin_id,))
    if not row or not security.verify_password(row["secret_hash"], req.secret_key or ""):
        _fail_admin(row["username"] if row else "")
        raise HTTPException(status_code=401, detail="第二重安全密钥错误")
    return {"admin_token": security.create_admin_token(admin_id),
            "admin": {"username": row["username"]}}


# ---------------- 管理员鉴权依赖 ----------------
def require_admin(request: Request):
    """依赖注入：仅接受独立 admin_token（type=admin）。普通用户 access_token 一律拒绝。"""
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少管理员凭证")
    try:
        payload = security.decode_token(auth[7:].strip(), "admin")
        admin_id = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="管理员凭证无效或已过期")
    row = db.query_one("SELECT * FROM admins WHERE id=?", (admin_id,))
    if not row:
        raise HTTPException(status_code=401, detail="管理员不存在")
    return row


def _mask_phone(p: str) -> str:
    return p[:3] + "****" + p[-4:] if len(p) >= 7 else p


def _user_out(row) -> dict:
    return {
        "id": row["id"],
        "account": row["account"],
        "username": row["username"],
        "phone_masked": _mask_phone(row["phone"]),
        "gender": row["gender"] or "secret",
        "signature": row["signature"] or "",
        "avatar": (row["avatar"] or "")[:80],
        "created_at": row["created_at"],
        "banned": bool(row["banned"]),
        "ban_reason": row["ban_reason"] or "",
        "banned_at": row["banned_at"] or "",
        "ban_until": row["ban_until"] or "",
        "ban_duration": row["ban_duration"] or "",
        "banned_by": row["banned_by"] or "",
        "ban_permanent": not (row["ban_until"] or ""),
    }


# ---------------- 用户管理 ----------------
@router.get("/users")
def admin_users(search: str = "", banned_only: int = 0, limit: int = 50, offset: int = 0,
                admin=Depends(require_admin)):
    limit = max(1, min(limit, 100))
    cond, params = [], []
    if search.strip():
        kw = f"%{search.strip()}%"
        cond.append("(account LIKE ? OR username LIKE ? OR phone LIKE ?)")
        params += [kw, kw, kw]
    if banned_only:
        cond.append("banned=1")
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    rows = db.query(
        f"SELECT * FROM users{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset))
    total = db.query_one(f"SELECT COUNT(*) n FROM users{where}", tuple(params))["n"]
    return {"total": total, "users": [_user_out(r) for r in rows]}


@router.get("/users/{user_id}")
def admin_user_detail(user_id: int, admin=Depends(require_admin)):
    row = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    d = _user_out(row)
    d["friend_count"] = db.query_one(
        "SELECT COUNT(*) n FROM friendships WHERE user_a_id=? OR user_b_id=?", (user_id, user_id))["n"]
    d["moment_count"] = db.query_one("SELECT COUNT(*) n FROM moments WHERE user_id=?", (user_id,))["n"]
    d["message_count"] = db.query_one("SELECT COUNT(*) n FROM messages WHERE sender_id=?", (user_id,))["n"]
    return {"user": d}


@router.post("/users/{user_id}/ban")
def admin_ban_user(user_id: int, req: BanReq, admin=Depends(require_admin)):
    row = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    ban_until, duration_desc = _compute_ban_until(req)
    reason = (req.reason or "").strip()[:200] or "违规行为"
    db.execute(
        "UPDATE users SET banned=1, ban_reason=?, banned_at=datetime('now','localtime'), "
        "ban_until=?, ban_duration=?, banned_by=? WHERE id=?",
        (reason, ban_until, duration_desc, admin["username"], user_id))
    # 撤销其全部 refresh token（强制下线，即使原 token 未过期也无法续期）
    db.execute("UPDATE refresh_tokens SET revoked=1 WHERE user_id=? AND revoked=0", (user_id,))
    until_txt = "永久" if not ban_until else ban_until
    return {"ok": True, "msg": f"已封禁 {row['account']}（时长 {duration_desc}，解封时间 {until_txt}）"}


@router.post("/users/{user_id}/unban")
def admin_unban_user(user_id: int, admin=Depends(require_admin)):
    row = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    db.execute(
        "UPDATE users SET banned=0, ban_reason='', banned_at=NULL, ban_until=NULL, "
        "ban_duration='', banned_by='' WHERE id=?", (user_id,))
    return {"ok": True, "msg": f"已解封 {row['account']}"}


@router.delete("/users/{user_id}")
def admin_delete_user(user_id: int, admin=Depends(require_admin)):
    row = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"ok": True, "msg": f"已删除用户 {row['account']}（关联数据级联清理）"}


# ---------------- 应用版本管理 ----------------
APK_DIR = config.DATA_DIR / "apks"


@router.get("/app/versions")
def admin_versions(admin=Depends(require_admin)):
    rows = db.query(
        "SELECT id, version, note, force_update, size, created_at FROM app_versions ORDER BY id DESC LIMIT 50")
    return {"versions": [dict(r) for r in rows]}


@router.post("/app/versions")
async def admin_publish_version(
    apk: UploadFile = File(...), version: str = Form(...), note: str = Form(""),
    force: int = Form(0), admin=Depends(require_admin)):
    version = (version or "").strip()
    if not version:
        raise HTTPException(status_code=400, detail="请填写版本号")
    raw = await apk.read()
    if len(raw) == 0 or len(raw) > 200 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="APK 为空或超过 200MB")
    APK_DIR.mkdir(parents=True, exist_ok=True)
    storage = f"{secrets.token_hex(8)}_{version.replace('.', '_')}.apk"
    apk_path = APK_DIR / storage
    apk_path.write_bytes(raw)
    try:
        # v9.97：INSERT 失败时回滚删除已写文件，避免出现"APK 在但版本记录缺失"的孤儿数据
        cur = db.execute(
            "INSERT INTO app_versions (version, note, force_update, size, storage_path) VALUES (?,?,?,?,?)",
            (version, (note or "")[:500], 1 if force else 0, len(raw), storage))
        if force:
            db.execute("UPDATE app_versions SET force_update=0 WHERE id<>?", (cur.lastrowid,))
    except Exception:
        try:
            apk_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="版本记录保存失败，已回滚")
    return {"ok": True, "msg": f"已发布 v{version}", "version_id": cur.lastrowid}


@router.delete("/app/versions/{version_id}")
def admin_delete_version(version_id: int, admin=Depends(require_admin)):
    row = db.query_one("SELECT * FROM app_versions WHERE id=?", (version_id,))
    if not row:
        raise HTTPException(status_code=404, detail="版本不存在")
    try:
        (APK_DIR / row["storage_path"]).unlink(missing_ok=True)
    except Exception:
        pass
    db.execute("DELETE FROM app_versions WHERE id=?", (version_id,))
    return {"ok": True, "msg": "已删除版本"}


# ---------------- 系统概况 ----------------
@router.get("/overview")
def admin_overview(admin=Depends(require_admin)):
    total = db.query_one("SELECT COUNT(*) n FROM users")["n"]
    banned = db.query_one("SELECT COUNT(*) n FROM users WHERE banned=1")["n"]
    today = db.query_one(
        "SELECT COUNT(*) n FROM users WHERE created_at >= date('now','localtime')")["n"]
    friends = db.query_one("SELECT COUNT(*) n FROM friendships")["n"]
    msgs = db.query_one("SELECT COUNT(*) n FROM messages")["n"]
    moments = db.query_one("SELECT COUNT(*) n FROM moments")["n"]
    ver = db.query_one("SELECT * FROM app_versions ORDER BY id DESC LIMIT 1")
    import pathlib
    apk_size = 0
    apk_path = pathlib.Path(config.ADMIN_CRED_FILE).parent  # server/
    return {
        "users": {"total": total, "banned": banned, "today_registered": today},
        "social": {"friendships": friends, "messages": msgs, "moments": moments},
        "app": {
            "version": (ver["version"] if ver else "9.93"),
            "force_update": bool(ver["force_update"]) if ver else False,
            "published_at": (ver["created_at"] if ver else ""),
        },
        "server": {"status": "running", "apk_dir": str(APK_DIR)},
    }


# ---------------- 客户端版本检查（普通用户可访问，无需管理员） ----------------
app_router = APIRouter(prefix="/api/app", tags=["app"])


@app_router.get("/version")
def app_version():
    ver = db.query_one("SELECT * FROM app_versions ORDER BY id DESC LIMIT 1")
    return {"latest": ver["version"] if ver else None,
            "force_update": bool(ver["force_update"]) if ver else False,
            "note": ver["note"] if ver else "",
            "apk_url": f"/api/app/apk/{ver['id']}" if ver else None}


@app_router.get("/apk/{version_id}")
def app_apk_download(version_id: int):
    """客户端下载 APK（公开下载，供「立即更新」按钮使用）。"""
    row = db.query_one("SELECT * FROM app_versions WHERE id=?", (version_id,))
    if not row:
        raise HTTPException(status_code=404, detail="版本不存在")
    path = APK_DIR / row["storage_path"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="APK 文件已丢失")
    return FileResponse(path, filename=f"CET4Prep-v{row['version']}.apk",
                        media_type="application/vnd.android.package-archive")
