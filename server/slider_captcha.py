"""
Vocabulary APP 本地认证系统 — 滑块拼图验证码（服务端生成与校验，v9.101）

安全模型：
- 服务端随机生成背景图 + 随机缺口位置，**缺口 x 坐标只保存在服务端**（KVStore），
  接口返回中不含 x，客户端仅凭视觉拖动，无法读取真实答案；
- 验证时客户端提交拼图块偏移 x，服务端与真实缺口比较（允许容差，默认 8px）；
- 唯一 captcha_id + 5 分钟 TTL + 一次性（验证成功立即作废）+ 失败 5 次作废；
- 验证成功后签发一次性滑块凭证 slider_token（3 分钟），注册/登录/忘记密码接口消费它；
- 每次重新生成时自动作废该 IP 上一个验证码（需求：旧验证码立即失效）；
- 调试接口（仅本机/DEBUG）可读取缺口 x，供自动化测试与本地联调，公网不可用。

图片生成全部为程序化（PIL 随机渐变/图形/噪点 + 随机缺口），无固定图片/固定答案。
"""
import base64
import io
import random
import secrets

from PIL import Image, ImageDraw

import config
import stores

BG_W = 320                 # 背景图宽（前端按此逻辑像素渲染，不做缩放换算）
BG_H = 160                 # 背景图高
PIECE_SIZE = 44            # 拼图块边长
X_MIN, X_MAX = 58, BG_W - PIECE_SIZE - 34   # 缺口 x 范围
Y_MIN, Y_MAX = 22, BG_H - PIECE_SIZE - 12   # 缺口 y 范围
TOLERANCE = 15              # 容差（像素，服务端统一使用原始验证码图片坐标；v9.106:10 → v9.108:15）
                            # 说明：CDP 真实触摸已证明坐标链路 err=0；15px 为真机手指/设备差异的合理上限（用户授权 ±12~15px 区间）
CAPTCHA_TTL = 5 * 60       # 验证码有效期 5 分钟
TOKEN_TTL = 3 * 60         # 滑块凭证有效期 3 分钟
MAX_FAILS = 5              # 连续失败 5 次作废


def _rand_color():
    return (random.randint(40, 215), random.randint(40, 215), random.randint(40, 215))


def _gen_background() -> Image.Image:
    """随机生成背景图：渐变 + 随机几何图形 + 噪点，避免固定样式。"""
    img = Image.new("RGB", (BG_W, BG_H))
    d = ImageDraw.Draw(img)
    c1, c2 = _rand_color(), _rand_color()
    # 对角渐变（逐行插值）
    for y in range(BG_H):
        t = y / BG_H
        r = int(c1[0] + (c2[0] - c1[0]) * t)
        g = int(c1[1] + (c2[1] - c1[1]) * t)
        b = int(c1[2] + (c2[2] - c1[2]) * t)
        d.line([(0, y), (BG_W, y)], fill=(r, g, b))
    # 随机几何图形
    for _ in range(random.randint(4, 9)):
        col = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255), random.randint(30, 90))
        shape = random.choice(("ellipse", "rect", "line"))
        if shape == "ellipse":
            x0, y0 = random.randint(-20, BG_W - 40), random.randint(-20, BG_H - 40)
            d.ellipse([x0, y0, x0 + random.randint(20, 90), y0 + random.randint(20, 90)],
                      fill=col[:3], outline=None)
        elif shape == "rect":
            x0, y0 = random.randint(-20, BG_W - 50), random.randint(-20, BG_H - 50)
            d.rectangle([x0, y0, x0 + random.randint(30, 110), y0 + random.randint(15, 60)],
                        fill=col[:3])
        else:
            d.line([(random.randint(0, BG_W), random.randint(0, BG_H)),
                    (random.randint(0, BG_W), random.randint(0, BG_H))],
                   fill=col[:3], width=random.randint(1, 3))
    # 噪点
    for _ in range(random.randint(200, 500)):
        x, y = random.randint(0, BG_W - 1), random.randint(0, BG_H - 1)
        img.putpixel((x, y), (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
    return img


def _cut_piece(bg: Image.Image, x: int, y: int) -> Image.Image:
    """从背景图裁剪拼图块（PIECE_SIZE × PIECE_SIZE），加亮色描边 + 底部内侧投影。

    v9.105 修复（坐标偏移根因）：**严禁扩边** —— 旧实现把 piece 画到
    (PIECE_SIZE+4)² 的画布上（右侧/下方 4px 阴影），导致图片固有尺寸 > 拼图尺寸。
    前端按图片固有尺寸渲染时 piece 比缺口大、且背景缩放后 piece 不缩放，
    视觉对齐位置 ≠ 服务端缺口坐标，产生固定偏移（屏幕越窄偏差越大）。
    现在图片尺寸 == 拼图尺寸 == 缺口尺寸，前端按 scale 等比缩放即严格一致。
    """
    piece = bg.crop((x, y, x + PIECE_SIZE, y + PIECE_SIZE)).convert("RGBA")
    edge = (random.randint(150, 255), random.randint(120, 255), random.randint(60, 255), 255)
    d = ImageDraw.Draw(piece)
    d.rectangle([0, 0, PIECE_SIZE - 1, PIECE_SIZE - 1], outline=edge, width=2)
    # 底部/右侧内侧投影（不扩边，仅视觉立体感）
    d.rectangle([0, PIECE_SIZE - 3, PIECE_SIZE - 1, PIECE_SIZE - 1], fill=(0, 0, 0, 55))
    d.rectangle([PIECE_SIZE - 3, 0, PIECE_SIZE - 1, PIECE_SIZE - 1], fill=(0, 0, 0, 55))
    return piece


def _mark_gap(bg: Image.Image, x: int, y: int):
    """在背景图上绘制缺口凹槽（半透明深色块 + 白色虚线边框），让用户可感知缺口位置。"""
    d = ImageDraw.Draw(bg)
    # 凹槽
    d.rectangle([x, y, x + PIECE_SIZE - 1, y + PIECE_SIZE - 1],
                fill=(0, 0, 0, 70), outline=(255, 255, 255, 200), width=2)
    # 内虚线
    for i in range(x + 4, x + PIECE_SIZE - 4, 8):
        d.line([(i, y + 4), (min(i + 4, x + PIECE_SIZE - 4), y + 4)], fill=(255, 255, 255, 160), width=1)
        d.line([(i, y + PIECE_SIZE - 5), (min(i + 4, x + PIECE_SIZE - 4), y + PIECE_SIZE - 5)],
               fill=(255, 255, 255, 160), width=1)


def _to_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _client_ip(request) -> str:
    """识别客户端 IP（经 Tunnel 时取 X-Forwarded-For 第一项，否则取直连地址）。"""
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff
    host = getattr(request.client, "host", None) or "127.0.0.1"
    return host


def generate_captcha(request) -> dict:
    """生成滑块验证码。返回不含缺口 x 的前端渲染数据；旧验证码立即作废。"""
    ip = _client_ip(request)
    cid = "sl_" + secrets.token_urlsafe(10)
    x = random.randint(X_MIN, X_MAX)
    y = random.randint(Y_MIN, Y_MAX)
    bg = _gen_background()
    _mark_gap(bg, x, y)
    piece = _cut_piece(bg, x, y)

    # 旧验证码立即失效（该 IP 上一次生成的验证数据删除）
    prev = stores.store.get(f"slider_ip:{ip}")
    if prev:
        stores.store.delete(f"slider:{prev}")
    stores.store.set(f"slider_ip:{ip}", cid, CAPTCHA_TTL)

    # 服务端保存真实缺口（含 ip 归属，防止跨 IP 冒用）
    stores.store.set(f"slider:{cid}", f"{x}:{ip}", CAPTCHA_TTL)
    print("[SLIDER-DBG] GEN cid=%s x=%d y=%d ip=%s (bg=%dx%d piece=%d tol=%d)"
          % (cid, x, y, ip, BG_W, BG_H, PIECE_SIZE, TOLERANCE))
    return {
        "captcha_id": cid,
        "bg": _to_data_url(bg),
        "piece": _to_data_url(piece),
        "y": y,
        "width": BG_W,
        "height": BG_H,
        "piece_size": PIECE_SIZE,
    }


def debug_peek(captcha_id: str) -> dict | None:
    """仅本机联调：读取真实缺口位置。"""
    raw = stores.store.get(f"slider:{captcha_id}")
    if not raw:
        return None
    x, _ = raw.split(":", 1)
    return {"captcha_id": captcha_id, "x": int(x), "width": BG_W}


def verify_captcha(request, captcha_id: str, submit_x, local: bool = False, dbg: dict | None = None) -> dict:
    """验证滑块位置。成功：作废验证码并签发一次性滑块凭证。
    返回 {"ok": bool, "detail": str, "slider_token": str|None, "refresh": bool, "debug": dict|None}
    - refresh=True 表示需前端刷新验证码（过期/次数超限）。
    - debug 为坐标判定调试信息（v9.106）：submitted_x/tolerance 恒返回（无害）；
      target_x/error（= |submitted - target|）仅 local=True（本机联调）时返回，
      公网请求不携带真实缺口坐标。
    - dbg（可选）：前端附带显示宽度/缩放等调试字段，仅用于服务端内部日志定位（不返回客户端）。"""
    ip = _client_ip(request)
    raw = stores.store.get(f"slider:{captcha_id}")
    exists = raw is not None
    if not raw:
        r = {"ok": False, "detail": "验证码已过期，请刷新后重试", "slider_token": None, "refresh": True,
             "debug": _verify_debug(None, None, local)}
        _log_verify(captcha_id, ip, None, None, None, exists, False, r, dbg)
        return r
    # v9.111 修复：split 限 1 次——IPv6 公网 IP 含冒号，旧代码 raw.split(":") 会抛 ValueError（公网注册/登录 500）
    real_x, owner_ip = raw.split(":", 1)
    if owner_ip != ip:
        r = {"ok": False, "detail": "验证码无效", "slider_token": None, "refresh": True,
             "debug": _verify_debug(None, None, local)}
        _log_verify(captcha_id, ip, None, int(real_x), owner_ip, exists, False, r, dbg)
        return r
    try:
        sx = int(float(submit_x))
    except (TypeError, ValueError):
        sx = -10 ** 9
    if abs(sx - int(real_x)) <= TOLERANCE:
        # 验证成功：验证码立即作废，签发一次性凭证
        stores.store.delete(f"slider:{captcha_id}")
        token = "st_" + secrets.token_urlsafe(10)
        stores.store.set(f"slider_token:{token}", ip, TOKEN_TTL)
        r = {"ok": True, "detail": "验证成功", "slider_token": token, "refresh": False,
             "debug": _verify_debug(sx, int(real_x), local)}
        _log_verify(captcha_id, ip, sx, int(real_x), owner_ip, exists, True, r, dbg)
        return r
    fails = stores.store.incr_fail(f"slider:{captcha_id}", MAX_FAILS)
    if fails >= MAX_FAILS:
        r = {"ok": False, "detail": "尝试次数过多，请刷新验证码", "slider_token": None, "refresh": True,
             "debug": _verify_debug(sx, int(real_x), local)}
        _log_verify(captcha_id, ip, sx, int(real_x), owner_ip, exists, False, r, dbg)
        return r
    r = {"ok": False, "detail": "验证失败，请调整滑块位置", "slider_token": None, "refresh": False,
         "debug": _verify_debug(sx, int(real_x), local)}
    _log_verify(captcha_id, ip, sx, int(real_x), owner_ip, exists, False, r, dbg)
    return r


def _log_verify(cid, ip, submit_x, target_x, owner_ip, exists, ok, r, dbg) -> None:
    """服务端内部完整调试日志（含真实缺口/误差/归属 IP，不依赖 local，不返回客户端 → 公网也不泄露）。
    附带前端 dbg（显示宽度/缩放/ui 坐标）用于核对两端坐标系。"""
    err = abs(submit_x - target_x) if (submit_x is not None and target_x is not None) else None
    ip_note = ""
    if owner_ip is not None and owner_ip != ip:
        ip_note = " <== IP不匹配(生成IP=%s)" % owner_ip
    fextra = ""
    if dbg and isinstance(dbg, dict):
        fextra = " fe.bg_w=%s fe.box_w=%s fe.scale=%s fe.ui_x=%s fe.submit=%s" % (
            dbg.get("bg_w"), dbg.get("box_w"), dbg.get("scale"), dbg.get("ui_x"), dbg.get("submit"))
    print("[SLIDER-DBG] VER cid=%s ip=%s exists=%d ok=%s submit=%s target=%s err=%s tol=%s fail_n=%s detail=%s%s%s"
          % (cid, ip, 1 if exists else 0, ok, submit_x, target_x, err, TOLERANCE,
             stores.store._fails.get(f"slider:{cid}", 0) if hasattr(stores.store, "_fails") else "?",
             r.get("detail"), ip_note, fextra))


def _verify_debug(sx, target_x, local: bool) -> dict:
    """构造坐标判定调试信息（v9.106）。submitted_x/tolerance 恒返回；
    target_x/error 仅本机（local=True）返回，避免公网泄露真实缺口位置。"""
    d = {"submitted_x": sx, "tolerance": TOLERANCE, "result": None}
    if sx is not None:
        d["result"] = "ok" if (target_x is not None and abs(sx - target_x) <= TOLERANCE) else "fail"
    if local and sx is not None and target_x is not None:
        d["target_x"] = target_x
        d["error"] = abs(sx - target_x)
    return d


def consume_slider_token(request, token: str) -> bool:
    """业务接口消费滑块凭证（一次性）。"""
    if not token:
        return False
    ip = _client_ip(request)
    owner = stores.store.get(f"slider_token:{token}")
    if owner is None or owner != ip:
        return False
    stores.store.delete(f"slider_token:{token}")
    return True
