"""
CET4Prep 本地认证系统 — 认证 API 路由
全流程：注册 → 图形验证码 → 短信验证码 → 登录 → Access+Refresh → 刷新 → 登出 → 忘记密码
"""
import datetime

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import config
import db
import security
import sms
import captcha
import slider_captcha
import stores

router = APIRouter(prefix="/api/auth", tags=["auth"])
me_router = APIRouter(prefix="/api/me", tags=["me"])


def _local_only(request: Request):
    """v9.100：公网 Tunnel 安全保护——仅本机/局域网直连（无 X-Forwarded-For，或为本机地址）
    可访问；经公网 Tunnel 转发（X-Forwarded-For 为真实客户端 IP）一律返回 404 伪装，
    避免将开发调试接口暴露到公网。"""
    if not _is_local(request):
        raise HTTPException(status_code=404, detail="Not Found")
    return True


def _is_local(request: Request) -> bool:
    """v9.106：判断请求是否来自本机/局域网直连（供 verify 调试字段、debug 接口共用）。"""
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip().lower()
    if xff and xff not in ("127.0.0.1", "::1", "localhost"):
        return False
    return True


def _client_ip_str(request: Request) -> str:
    """v9.108：日志用客户端 IP（Tunnel 转发取 X-Forwarded-For 首项）。"""
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff
    return getattr(request.client, "host", "") or ""


# ---------------- 请求/响应模型 ----------------
class CaptchaResp(BaseModel):
    captcha_id: str
    image: str                       # data:image/png;base64,...


class SmsSendReq(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    purpose: str = "register"        # register / reset / rebind


class RegisterReq(BaseModel):
    account: str = Field(min_length=2, max_length=32)
    phone: str = Field(min_length=11, max_length=11)
    password: str = Field(min_length=6, max_length=64)
    sms_code: str = Field(min_length=6, max_length=6)
    captcha_id: str = ""
    captcha_text: str = ""
    # v9.101：滑块验证码（优先）；提供 slider_token 时跳过字符码校验
    slider_token: str = ""
    device: str = ""            # v9.109：设备名


class LoginReq(BaseModel):
    account: str
    password: str
    captcha_id: str = ""
    captcha_text: str = ""
    # v9.101：滑块验证码（优先）
    slider_token: str = ""
    device: str = ""            # v9.109：设备名（前端 UA 摘要，用于登录记录/设备列表）


class LoginSmsReq(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    sms_code: str = Field(min_length=6, max_length=6)
    device: str = ""            # v9.109：设备名


class RefreshReq(BaseModel):
    refresh_token: str
    device_id: str = "webview"


class LogoutReq(BaseModel):
    refresh_token: str


class ResetPasswordReq(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    sms_code: str = Field(min_length=6, max_length=6)
    new_password: str = Field(min_length=6, max_length=64)
    # v9.101：滑块验证码（优先）；提供 slider_token 时跳过字符码校验
    slider_token: str = ""


class UserPatchReq(BaseModel):
    username: str | None = None
    avatar: str | None = None
    gender: str | None = None          # v9.89：male / female / secret
    signature: str | None = None       # v9.89：个性签名（≤120 字）


class RebindPhoneReq(BaseModel):
    new_phone: str = Field(min_length=11, max_length=11)
    sms_code: str = Field(min_length=6, max_length=6)


class ChangePwdReq(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=64)


class TokenResp(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict


# ---------------- 工具 ----------------
def _user_dict(row) -> dict:
    return {
        "id": row["id"],
        "public_id": row["public_id"] or "",     # v9.87：公开纯数字唯一 ID（搜索用）
        "account": row["account"],
        "username": row["username"],
        "phone": security_phone_mask(row["phone"]),
        "phone_raw": row["phone"],
        "avatar": row["avatar"] or "",
        "gender": row["gender"] or "secret",     # v9.89：male / female / secret
        "signature": row["signature"] or "",     # v9.89：个性签名
        "created_at": row["created_at"],
    }


def security_phone_mask(phone: str) -> str:
    return phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone


def _issue_tokens(user_id: int, device_id: str) -> TokenResp:
    access = security.create_access_token(user_id)
    refresh, jti, exp_iso = security.create_refresh_token(user_id)
    db.execute(
        "INSERT INTO refresh_tokens (user_id, token_hash, device_id, expires_at) VALUES (?,?,?,?)",
        (user_id, security.sha256_hex(refresh), device_id, exp_iso),
    )
    row = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    return TokenResp(
        access_token=access,
        refresh_token=refresh,
        expires_in=config.ACCESS_TOKEN_MINUTES * 60,
        user=_user_dict(row),
    )


def _rotate_refresh(old_refresh: str, device_id: str) -> TokenResp:
    """Refresh Token 旋转：旧 token 撤销，签发全新 access+refresh。"""
    try:
        payload = security.decode_token(old_refresh, "refresh")
    except pyjwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail="refresh_token 无效或已过期") from e
    token_hash = security.sha256_hex(old_refresh)
    row = db.query_one(
        "SELECT * FROM refresh_tokens WHERE token_hash=? AND revoked=0", (token_hash,))
    if not row:
        raise HTTPException(status_code=401, detail="refresh_token 已被撤销")
    if row["expires_at"] < _iso_now():
        db.execute("UPDATE refresh_tokens SET revoked=1 WHERE id=?", (row["id"],))
        raise HTTPException(status_code=401, detail="refresh_token 已过期，请重新登录")
    user_id = int(payload["sub"])
    db.execute("UPDATE refresh_tokens SET revoked=1 WHERE id=?", (row["id"],))
    # v9.93/v9.95：封禁用户禁止刷新登录态（强制下线）；到期自动解封
    urow = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if urow and _check_banned(urow):
        raise HTTPException(status_code=403, detail="账号已被封禁")
    return _issue_tokens(user_id, device_id)


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _local_now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _check_banned(row) -> dict | None:
    """封禁检查 + 到期自动解封（以服务器时间为准）。
    返回封禁信息 dict；未封禁或已自动解封返回 None。"""
    if not row["banned"]:
        return None
    until = row["ban_until"] or ""
    if until and until <= _local_now_str():
        # v9.95：到达解封时间 → 自动解除（永久封禁 ban_until 为空，永不自动解封）
        db.execute(
            "UPDATE users SET banned=0, ban_reason='', banned_at=NULL, ban_until=NULL, "
            "ban_duration='', banned_by='' WHERE id=?", (row["id"],))
        return None
    return {
        "banned": True,
        "reason": row["ban_reason"] or "",
        "until": until,
        "permanent": not until,
        "duration": row["ban_duration"] or "",
        "banned_by": row["banned_by"] or "",
        "banned_at": row["banned_at"] or "",
    }


def _ban_detail(info: dict) -> str:
    """封禁 403 的完整文案（登录页无 token 也能直接展示）。"""
    reason = info["reason"] or "违规行为"
    until = "永久" if info["permanent"] else (info["until"] or "—")
    return f"您的账号已因「{reason}」被封禁，解封时间：{until}"


# ---------- 登录失败限流（暴力破解防护：连错 5 次锁 15 分钟） ----------
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 15 * 60


def _login_lock_ttl(account: str) -> int:
    return stores.store.ttl(f"login_lock:{account}")


def _fail_login(account: str) -> None:
    """累计登录失败；达到阈值锁定账号（滑动窗口 15 分钟）。"""
    key = f"login_fail:{account}"
    n = int(stores.store.get(key) or 0) + 1
    stores.store.set(key, str(n), LOGIN_LOCK_SECONDS)
    if n >= LOGIN_MAX_FAILS:
        stores.store.delete(key)
        stores.store.set(f"login_lock:{account}", "1", LOGIN_LOCK_SECONDS)


def _clear_login_fail(account: str) -> None:
    stores.store.delete(f"login_fail:{account}")
    stores.store.delete(f"login_lock:{account}")


def require_user(request: Request):
    """依赖注入：解析 Bearer Access Token（必须标注 Request 类型，FastAPI 才能注入）。"""
    authorization = _bearer(request)          # 缺 Authorization 头 → 401（含明确 detail）
    try:
        payload = security.decode_token(authorization, "access")
        user_id = int(payload["sub"])
    except (pyjwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="access_token 无效或已过期") from None
    row = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(status_code=401, detail="用户不存在")
    # v9.93/v9.95：封禁用户拒绝一切需要鉴权的操作；到期自动解封
    info = _check_banned(row)
    if info:
        raise HTTPException(status_code=403, detail="账号已被封禁")
    return row


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization 头")
    return auth[7:].strip()


# ---------------- 公开接口 ----------------
@router.get("/captcha", response_model=CaptchaResp)
def get_captcha():
    cid, image = captcha.generate_captcha()
    return CaptchaResp(captcha_id=cid, image=image)


@router.post("/sms/send")
def sms_send(req: SmsSendReq):
    if req.purpose not in ("register", "reset", "rebind", "login"):
        raise HTTPException(status_code=400, detail="purpose 不合法")
    ok, msg = sms.send_sms_code(req.phone, req.purpose)
    if not ok:
        raise HTTPException(status_code=429, detail=msg)
    resp = {"ok": True, "msg": msg}
    # v9.104：Mock 短信无真实通道——DEBUG 模式下把验证码明文回显给客户端（前端 toast 展示，便于个人测试/异地联调；
    # 生产环境 VOCAB_DEBUG=0 时不回显）
    if config.DEBUG:
        code = sms.last_sms_code(req.phone, req.purpose)
        if code:
            resp["debug_code"] = code
    return resp


def _verify_required_captcha(request: Request, slider_token: str, captcha_id: str, captcha_text: str,
                             fallback_pass: bool = False) -> None:
    """v9.101：安全验证统一入口。滑块凭证优先（消费后一次性失效）；
    无滑块凭证时回退字符图形验证码（兼容旧客户端/测试）；
    fallback_pass=True（忘记密码）且两者都未提供时放行（由短信验证码兜底，保持旧流程兼容）。"""
    if slider_token:
        if not slider_captcha.consume_slider_token(request, slider_token):
            raise HTTPException(status_code=400, detail="滑块验证已失效，请重新验证")
        return
    if captcha_id or captcha_text:
        if not captcha.verify_captcha(captcha_id, captcha_text):
            raise HTTPException(status_code=400, detail="图形验证码错误或已失效")
        return
    if not fallback_pass:
        raise HTTPException(status_code=400, detail="请先完成滑块验证")


class CaptchaVerifyReq(BaseModel):
    captcha_id: str
    captcha_text: str


@router.post("/captcha/verify")
def captcha_verify(req: CaptchaVerifyReq):
    """v9.103：图形验证码预校验（注册分层验证第一步：图形码 → 滑块 → 短信；一次性消费）。"""
    if not captcha.verify_captcha(req.captcha_id, req.captcha_text):
        raise HTTPException(status_code=400, detail="图形验证码错误或已失效")
    return {"ok": True}


@router.post("/register", response_model=TokenResp)
def register(req: RegisterReq, request: Request):
    # 0. 账号唯一性（早期短路，避免无谓消耗验证码）
    if db.query_one("SELECT id FROM users WHERE account=?", (req.account,)):
        raise HTTPException(status_code=409, detail="该用户名已被注册")
    if db.query_one("SELECT id FROM users WHERE phone=?", (req.phone,)):
        raise HTTPException(status_code=409, detail="该手机号已注册，请直接登录")
    # 0b. 密码强度（≥8 位 + 字母 + 数字）
    strength_err = security.validate_password_strength(req.password)
    if strength_err:
        raise HTTPException(status_code=400, detail=strength_err)
    # 1. 安全验证（v9.103 分层：注册 = 图形码预校验(前端已做) + 滑块凭证【强制】+ 短信码）
    if not slider_captcha.consume_slider_token(request, req.slider_token):
        raise HTTPException(status_code=400, detail="请先完成滑块验证")
    # 2. 短信验证码
    if not sms.verify_sms_code(req.phone, "register", req.sms_code):
        raise HTTPException(status_code=400, detail="短信验证码错误或已失效")
    # 3. 写入用户（v9.87：注册即生成公开纯数字唯一 public_id）
    cur = db.execute(
        "INSERT INTO users (account, username, phone, password_hash, public_id) VALUES (?,?,?,?,?)",
        (req.account, req.account, req.phone, security.hash_password(req.password),
         db.generate_public_id()),
    )
    # v9.109：注册即自动登录 → 记一条成功登录记录（设备名）
    _device = (req.device or "webview").strip()[:60] or "webview"
    db.execute("INSERT INTO login_history (user_id, device, result) VALUES (?,?,?)",
               (cur.lastrowid, _device, "success"))
    return _issue_tokens(cur.lastrowid, _device)


@router.post("/login", response_model=TokenResp)
def login(req: LoginReq, request: Request):
    # 0. 暴力破解防护：连错 5 次锁 15 分钟
    lock_ttl = _login_lock_ttl(req.account)
    if lock_ttl > 0:
        raise HTTPException(status_code=429,
                            detail=f"失败次数过多，请 {max(1, (lock_ttl + 59) // 60)} 分钟后重试")
    # 1. 安全验证（v9.101：滑块验证码优先，字符码兜底）
    _verify_required_captcha(request, req.slider_token, req.captcha_id, req.captcha_text)
    device = (req.device or "webview").strip()[:60] or "webview"
    # 2. 账号密码校验（统一提示，防账号枚举）
    row = db.query_one(
        "SELECT * FROM users WHERE account=? OR phone=?",
        (req.account, req.account),
    )
    if not row or not security.verify_password(row["password_hash"], req.password):
        _fail_login(req.account)
        # v9.109：失败也记登录历史（仅粗粒度"密码错误"，不暴露细节）
        if row:
            db.execute("INSERT INTO login_history (user_id, device, result, detail) VALUES (?,?,?,?)",
                       (row["id"], device, "fail", "密码错误"))
        raise HTTPException(status_code=401, detail="账号或密码错误")
    # v9.93/v9.95：封禁用户登录直接拒绝（含重登）；到期自动解封；完整文案含原因与解封时间
    info = _check_banned(row)
    if info:
        raise HTTPException(status_code=403, detail=_ban_detail(info))
    _clear_login_fail(req.account)
    # v9.109：登录成功记录
    db.execute("INSERT INTO login_history (user_id, device, result) VALUES (?,?,?)",
               (row["id"], device, "success"))
    return _issue_tokens(row["id"], device)


@router.post("/login/sms", response_model=TokenResp)
def login_sms(req: LoginSmsReq):
    if not sms.verify_sms_code(req.phone, "login", req.sms_code):
        # 登录短信不单独发（前端复用 register 用途发码？）——登录用途单独支持
        raise HTTPException(status_code=400, detail="短信验证码错误或已失效")
    row = db.query_one("SELECT * FROM users WHERE phone=?", (req.phone,))
    if not row:
        raise HTTPException(status_code=404, detail="该手机号未注册")
    # v9.93/v9.95：封禁用户登录直接拒绝；到期自动解封；完整文案
    info = _check_banned(row)
    if info:
        raise HTTPException(status_code=403, detail=_ban_detail(info))
    device = (req.device or "webview").strip()[:60] or "webview"
    db.execute("INSERT INTO login_history (user_id, device, result) VALUES (?,?,?)",
               (row["id"], device, "success"))
    return _issue_tokens(row["id"], device)


@router.post("/refresh", response_model=TokenResp)
def refresh(req: RefreshReq):
    return _rotate_refresh(req.refresh_token, req.device_id)


@router.get("/ban-status")
def ban_status(request: Request):
    """查询当前账号封禁状态（v9.95）。带 Bearer access token 或 ?account= 均可。
    返回结构化封禁信息，供客户端强制下线弹窗展示原因与解封时间。"""
    row = None
    try:
        authorization = _bearer(request)
        payload = security.decode_token(authorization, "access")
        row = db.query_one("SELECT * FROM users WHERE id=?", (int(payload["sub"]),))
    except Exception:
        account = request.query_params.get("account")
        if account:
            row = db.query_one("SELECT * FROM users WHERE account=? OR phone=?", (account, account))
    if not row:
        return {"banned": False}
    info = _check_banned(row)
    return info if info else {"banned": False}


@router.post("/logout")
def logout(req: LogoutReq):
    try:
        payload = security.decode_token(req.refresh_token, "refresh")
        jti = payload.get("jti")
        row = db.query_one("SELECT id FROM refresh_tokens WHERE token_hash=? AND revoked=0",
                           (security.sha256_hex(req.refresh_token),))
        if row:
            db.execute("UPDATE refresh_tokens SET revoked=1 WHERE id=?", (row["id"],))
    except pyjwt.PyJWTError:
        pass  # token 已失效则无需撤销
    return {"ok": True, "msg": "已退出登录"}


@router.post("/password/reset")
def reset_password(req: ResetPasswordReq, request: Request):
    # v9.101：滑块验证码（前端强制先滑块；兼容旧客户端未提供时由短信码兜底）
    _verify_required_captcha(request, req.slider_token, "", "", fallback_pass=True)
    if not sms.verify_sms_code(req.phone, "reset", req.sms_code):
        raise HTTPException(status_code=400, detail="短信验证码错误或已失效")
    strength_err = security.validate_password_strength(req.new_password)
    if strength_err:
        raise HTTPException(status_code=400, detail=strength_err)
    row = db.query_one("SELECT id FROM users WHERE phone=?", (req.phone,))
    if not row:
        raise HTTPException(status_code=404, detail="该手机号未注册")
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (security.hash_password(req.new_password), row["id"]))
    # 安全：改密后撤销该用户全部 refresh token
    db.execute("UPDATE refresh_tokens SET revoked=1 WHERE user_id=? AND revoked=0", (row["id"],))
    return {"ok": True, "msg": "密码已重置，请重新登录"}


# ---------------- v9.101 滑块验证码 ----------------
@router.get("/slider/captcha")
def slider_captcha_new(request: Request):
    """生成滑块拼图验证码：背景图（含缺口凹槽）+ 拼图块 + 缺口 y（仅渲染用）。
    缺口 x 只保存在服务端，响应中不含。"""
    return slider_captcha.generate_captcha(request)


class SliderVerifyReq(BaseModel):
    captcha_id: str
    x: float
    dbg: dict | None = None   # v9.111 排查：前端附带显示宽度/缩放/UI 坐标，仅服务端日志使用，不返回


@router.post("/slider/verify")
def slider_verify(req: SliderVerifyReq, request: Request):
    """校验滑块位置（服务端比对真实缺口，容差 ±TOLERANCE 像素，v9.106 为 ±10px，v9.108 为 ±15px）。

    v9.106 变更：**无论成败均返回 200**，携带坐标判定调试信息：
      {"ok": bool, "detail": str, "slider_token": str|None, "debug": {...}}
    - debug.submitted_x / tolerance / result 恒返回（无害）；
    - debug.target_x / error（=|submitted-target|）仅本机请求返回（供开发联调判断
      "坐标转换问题"还是"阈值问题"），公网请求不泄露真实缺口坐标。
    成功：验证码作废并签发一次性滑块凭证 slider_token（3 分钟）。

    v9.108+：每次验证（无论成败）打印服务端日志（含真实缺口与误差）——
    真机公网用户失败时，开发者可在服务器日志看到 submitted/target/error 精确定位根因。
    v9.111：日志不再依赖 local（公网失败也能看到 target/err）；前端 dbg 字段打印显示宽/缩放。"""
    r = slider_captcha.verify_captcha(request, req.captcha_id, req.x, local=_is_local(request), dbg=req.dbg)
    dbg = r.get("debug") or {}
    print("[SLIDER] ip=%s cid=%s submitted=%s target=%s err=%s tol=%s result=%s detail=%s"
          % (_client_ip_str(request), (req.captcha_id or "")[:16], dbg.get("submitted_x"),
             dbg.get("target_x"), dbg.get("error"), dbg.get("tolerance"), dbg.get("result"), r.get("detail")))
    return {"ok": r["ok"], "detail": r["detail"], "slider_token": r["slider_token"],
            "refresh": r["refresh"], "debug": r["debug"]}


@router.get("/slider/debug", dependencies=[Depends(_local_only)])
def slider_debug(captcha_id: str):
    """仅本机/DEBUG：读取滑块验证码真实缺口位置（自动化测试与本地联调用）。
    v9.101：_local_only 保护——公网 Tunnel 访问一律 404。"""
    if not config.DEBUG:
        raise HTTPException(status_code=404, detail="调试接口未开放")
    info = slider_captcha.debug_peek(captcha_id)
    if not info:
        raise HTTPException(status_code=404, detail="验证码不存在或已过期")
    return info


@router.get("/debug/last-sms", dependencies=[Depends(_local_only)])
def debug_last_sms(phone: str, purpose: str = "register"):
    """仅开发模式（VOCAB_DEBUG=1）开放：本地联调读取最近一条短信验证码。
    v9.100：增加 _local_only 保护——经公网 Tunnel 访问一律 404（不暴露调试接口）。"""
    if not config.DEBUG:
        raise HTTPException(status_code=404, detail="调试接口未开放")
    code = sms.last_sms_code(phone, purpose)
    return {"phone": phone, "purpose": purpose, "code": code}


@router.get("/debug/captcha", dependencies=[Depends(_local_only)])
def debug_captcha(captcha_id: str):
    """仅开发模式开放：读取图形验证码明文（端到端测试/联调用）。
    v9.100：增加 _local_only 保护——经公网 Tunnel 访问一律 404。"""
    if not config.DEBUG:
        raise HTTPException(status_code=404, detail="调试接口未开放")
    return {"captcha_id": captcha_id, "text": captcha.debug_peek(captcha_id)}


# ---------------- 受保护接口（Bearer Access Token） ----------------
@me_router.get("")
def me(user=Depends(require_user)):
    return {"user": _user_dict(user)}


@me_router.patch("")
def update_me(req: UserPatchReq, user=Depends(require_user)):
    fields, params = [], []
    if req.username is not None:
        fields.append("username=?")
        params.append(req.username.strip() or user["username"])
    if req.avatar is not None:
        fields.append("avatar=?")
        params.append(req.avatar[: 2_500_000] if req.avatar else "")  # 限制 2.5MB
    if req.gender is not None:
        if req.gender not in ("male", "female", "secret"):
            raise HTTPException(status_code=400, detail="性别取值不合法")
        fields.append("gender=?")
        params.append(req.gender)
    if req.signature is not None:
        sig = req.signature.strip()
        if len(sig) > 120:
            raise HTTPException(status_code=400, detail="个性签名不能超过 120 字")
        fields.append("signature=?")
        params.append(sig)
    if fields:
        params.append(user["id"])
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", tuple(params))
    row = db.query_one("SELECT * FROM users WHERE id=?", (user["id"],))
    return {"user": _user_dict(row)}


@me_router.post("/phone/rebind")
def rebind_phone(req: RebindPhoneReq, user=Depends(require_user)):
    if not sms.verify_sms_code(req.new_phone, "rebind", req.sms_code):
        raise HTTPException(status_code=400, detail="短信验证码错误或已失效")
    if db.query_one("SELECT id FROM users WHERE phone=? AND id!=?", (req.new_phone, user["id"])):
        raise HTTPException(status_code=409, detail="该手机号已被其他账号绑定")
    db.execute("UPDATE users SET phone=? WHERE id=?", (req.new_phone, user["id"]))
    row = db.query_one("SELECT * FROM users WHERE id=?", (user["id"],))
    return {"user": _user_dict(row)}


@me_router.post("/password/change")
def change_password(req: ChangePwdReq, user=Depends(require_user)):
    if not security.verify_password(user["password_hash"], req.old_password):
        raise HTTPException(status_code=400, detail="原密码错误")
    strength_err = security.validate_password_strength(req.new_password)
    if strength_err:
        raise HTTPException(status_code=400, detail=strength_err)
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (security.hash_password(req.new_password), user["id"]))
    db.execute("UPDATE refresh_tokens SET revoked=1 WHERE user_id=? AND revoked=0", (user["id"],))
    return {"ok": True, "msg": "密码已修改，请重新登录"}


# ---------------- v9.109 最近登录设备 / 登录记录 / 学习上报 ----------------
@me_router.get("/devices")
def my_devices(current: str = "", user=Depends(require_user)):
    """当前账号近期登录设备列表（按设备聚合 refresh_tokens）。
    仅返回设备名/最近登录时间/是否当前设备/在线状态，不返回 Token 等敏感信息。"""
    rows = db.query(
        """SELECT device_id,
                  MAX(created_at) AS last_login,
                  SUM(CASE WHEN revoked=0 THEN 1 ELSE 0 END) AS active_cnt
           FROM refresh_tokens WHERE user_id=?
           GROUP BY device_id ORDER BY last_login DESC""", (user["id"],))
    me = user["id"]
    now = _local_now_str()
    devices = []
    for r in rows:
        dev = r["device_id"] or "webview"
        devices.append({
            "device": dev,
            "last_login": r["last_login"] or "",
            "active": bool(r["active_cnt"]),
            "current": dev == (current or ""),
            "expired": not bool(r["active_cnt"]),   # 无有效 token = 已退出/过期
        })
    return {"devices": devices, "now": now}


@me_router.get("/login-history")
def my_login_history(user=Depends(require_user)):
    """当前账号最近登录记录（时间/设备/结果），不暴露 IP/Token。"""
    rows = db.query(
        "SELECT device, result, detail, created_at FROM login_history "
        "WHERE user_id=? ORDER BY id DESC LIMIT 50", (user["id"],))
    return {"history": [{
        "device": r["device"] or "webview",
        "result": r["result"],
        "detail": r["detail"] or "",
        "created_at": r["created_at"],
    } for r in rows]}


class StudyLogReq(BaseModel):
    word_count: int = Field(ge=0, le=500)
    duration_sec: int = Field(ge=0, le=86400)


@me_router.post("/study/log")
def study_log(req: StudyLogReq, user=Depends(require_user)):
    """学习行为上报（真实统计）：当日背词数 + 学习时长（秒），按日累加。"""
    if req.word_count <= 0 and req.duration_sec <= 0:
        return {"ok": True}
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    db.execute(
        """INSERT INTO study_daily (user_id, date, word_count, duration_sec)
           VALUES (?,?,?,?)
           ON CONFLICT(user_id, date) DO UPDATE SET
             word_count=word_count+excluded.word_count,
             duration_sec=duration_sec+excluded.duration_sec,
             updated_at=datetime('now','localtime')""",
        (user["id"], today, max(0, req.word_count), max(0, req.duration_sec)),
    )
    return {"ok": True}


@me_router.get("/study/stats")
def my_study_stats(user=Depends(require_user)):
    """当前账号学习统计（今日词数/今日时长/总时长），供"我的"页展示。"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    today_row = db.query_one(
        "SELECT word_count, duration_sec FROM study_daily WHERE user_id=? AND date=?",
        (user["id"], today))
    total_row = db.query_one(
        "SELECT COALESCE(SUM(duration_sec),0) d FROM study_daily WHERE user_id=?", (user["id"],))
    return {
        "today_words": today_row["word_count"] if today_row else 0,
        "today_duration": today_row["duration_sec"] if today_row else 0,
        "total_duration": total_row["d"] if total_row else 0,
    }
