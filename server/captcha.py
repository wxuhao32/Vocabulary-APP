"""
CET4Prep 本地认证系统 — 图形验证码（本地生成，PIL）
- 随机 4 字符（剔除易混淆 0/O/1/l/I）
- 干扰线 + 噪点 + 字符旋转 + 随机色
- 存储走 KVStore 抽象（内存 TTL），一次性校验 + 失败次数限制
"""
import base64
import io
import random
import string

from PIL import Image, ImageDraw, ImageFont

import config
import stores

_CAPTCHA_CHARS = string.ascii_uppercase + string.digits
for _c in "0O1lI":
    _CAPTCHA_CHARS = _CAPTCHA_CHARS.replace(_c, "")

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def generate_captcha() -> tuple[str, str]:
    """生成验证码；返回 (captcha_id, data:image/png;base64,...)。"""
    text = "".join(random.choice(_CAPTCHA_CHARS) for _ in range(4))
    captcha_id = stores.new_key()

    width, height = 86, 36
    img = Image.new("RGB", (width, height), (247, 239, 228))   # 米色底
    draw = ImageDraw.Draw(img)
    # 干扰线
    for _ in range(5):
        draw.line(
            [(random.randint(0, width), random.randint(0, height)),
             (random.randint(0, width), random.randint(0, height))],
            fill=(240, 130, 31, 40), width=1,
        )
    # 噪点
    for _ in range(60):
        draw.point((random.randint(0, width - 1), random.randint(0, height - 1)),
                   fill=(21, 49, 75, 20))
    # 字符（旋转 + 随机色）
    colors = [(21, 49, 75), (214, 69, 53), (46, 158, 107), (240, 130, 31)]
    font = _load_font(22)
    x = 8
    for ch in text:
        draw.text((x + random.randint(-1, 1), random.randint(2, 8)), ch,
                  font=font, fill=random.choice(colors))
        x += 19
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    stores.store.set(f"captcha:{captcha_id}", text, config.CAPTCHA_TTL_SECONDS)
    return captcha_id, b64


def verify_captcha(captcha_id: str, text: str) -> bool:
    """一次性校验：成功即作废；连续错满 CAPTCHA_MAX_ATTEMPTS 作废。"""
    key = f"captcha:{captcha_id}"
    expect = stores.store.get(key)
    if expect is None:
        return False
    if (text or "").strip().lower() != expect.lower():
        if stores.store.incr_fail(key, config.CAPTCHA_MAX_ATTEMPTS) >= config.CAPTCHA_MAX_ATTEMPTS:
            stores.store.delete(key)
        return False
    stores.store.delete(key)
    return True


def debug_peek(captcha_id: str) -> str | None:
    """开发调试：读取验证码明文（仅 DEBUG 模式开放，生产必关）。"""
    return stores.store.get(f"captcha:{captcha_id}")
