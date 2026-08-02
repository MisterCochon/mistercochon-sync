"""
Rich Menu for Mister Cochon retail LINE bot (@920gsiph).
Run once via: /setup-richmenu-retail?secret=XXX
"""
import os, sys, json, io, math, requests
from PIL import Image, ImageDraw, ImageFont

LINE_API = "https://api.line.me/v2/bot"

W, H = 2500, 843
COLS = 3
ROWS = 2
CW   = W // COLS   # 833
CH   = H // ROWS   # 421

BORDEAUX = "#6B0000"
RED      = "#8B0000"
DARK     = "#4A0000"
WHITE    = "#FFFFFF"
LIGHT    = "#FFAAAA"
SEP      = "#9B2020"

_DIR         = os.path.dirname(os.path.abspath(__file__))
FONT_EN_BOLD = os.path.join(_DIR, "fonts", "Montserrat-Bold.ttf")
FONT_THAI    = os.path.join(_DIR, "fonts", "NotoSansThai.ttf")

CELLS = [
    # row, col, bg,       icon,       en_label,         th_label,            action_text
    (0, 0, BORDEAUX, "catalog",   "Products",       "สินค้า",             "สินค้า"),
    (0, 1, BORDEAUX, "cart",      "My Cart",        "ตะกร้าสินค้า",       "ตะกร้า"),
    (0, 2, RED,      "checkout",  "Checkout",       "ชำระเงิน",           "checkout"),
    (1, 0, BORDEAUX, "clear",     "Clear Cart",     "ล้างตะกร้า",          "cancel"),
    (1, 1, BORDEAUX, "help",      "Help",           "ช่วยเหลือ",           "help"),
    (1, 2, DARK,     "logout",    "Log Out",        "ออกจากระบบ",          "logout"),
]


def draw_icon(d: ImageDraw, cx: int, cy: int, name: str, size: int):
    s  = size
    lw = max(3, s // 14)
    c  = WHITE

    if name == "catalog":
        # Shop/store icon
        sw = int(s * 0.7)
        sh = int(s * 0.55)
        sx = cx - sw // 2
        sy = cy - int(s * 0.05)
        d.rectangle([sx, sy, sx + sw, sy + sh], outline=c, width=lw)
        # Roof/awning
        aw = int(s * 0.9)
        d.polygon([(cx - aw//2, sy), (cx, sy - int(s*0.3)), (cx + aw//2, sy)],
                  outline=c, fill=None, width=lw)
        # Door
        dw, dh = int(s*0.22), int(s*0.3)
        d.rectangle([cx - dw//2, sy + sh - dh, cx + dw//2, sy + sh], outline=c, width=lw)

    elif name == "cart":
        bag_w = int(s * 0.55)
        bag_h = int(s * 0.55)
        bag_x = cx - bag_w // 2
        bag_y = cy - int(s * 0.1)
        hr = int(s * 0.22)
        d.arc([cx - hr, bag_y - hr * 2, cx + hr, bag_y],
              start=180, end=0, fill=c, width=lw)
        d.rounded_rectangle([bag_x, bag_y, bag_x + bag_w, bag_y + bag_h],
                             radius=lw * 2, outline=c, width=lw)

    elif name == "checkout":
        # Circle with checkmark
        r = int(s * 0.45)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=lw)
        # Checkmark
        p1 = (cx - int(s*0.22), cy)
        p2 = (cx - int(s*0.05), cy + int(s*0.2))
        p3 = (cx + int(s*0.28), cy - int(s*0.2))
        d.line([p1, p2, p3], fill=c, width=lw + 2)

    elif name == "clear":
        # Trash bin
        tw = int(s * 0.5)
        th = int(s * 0.55)
        tx = cx - tw // 2
        ty = cy - int(s * 0.1)
        d.rounded_rectangle([tx, ty, tx + tw, ty + th], radius=lw, outline=c, width=lw)
        # Lid
        lid_w = int(s * 0.62)
        d.line([(cx - lid_w//2, ty), (cx + lid_w//2, ty)], fill=c, width=lw + 1)
        # Handle
        hw = int(s * 0.24)
        d.arc([cx - hw//2, ty - lw*6, cx + hw//2, ty], start=180, end=0, fill=c, width=lw)
        # Lines inside
        for i in range(3):
            xv = tx + int(tw * (0.25 + i * 0.25))
            d.line([(xv, ty + lw*4), (xv, ty + th - lw*3)], fill=c, width=lw - 1 or 1)

    elif name == "help":
        # Question mark in circle
        r = int(s * 0.45)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=lw)
        # ? glyph (approximate)
        qr = int(s * 0.18)
        d.arc([cx - qr, cy - int(s*0.28) - qr, cx + qr, cy - int(s*0.28) + qr],
              start=200, end=340, fill=c, width=lw)
        d.line([(cx, cy - int(s*0.28) + qr), (cx, cy - int(s*0.04))], fill=c, width=lw)
        dot_r = lw
        d.ellipse([cx - dot_r, cy + int(s*0.1) - dot_r,
                   cx + dot_r, cy + int(s*0.1) + dot_r], fill=c)

    elif name == "logout":
        # Door with arrow
        dw, dh = int(s*0.44), int(s*0.7)
        dx = cx - dw // 2 - int(s*0.06)
        dy = cy - dh // 2
        d.rectangle([dx, dy, dx + dw, dy + dh], outline=c, width=lw)
        # Arrow pointing right (exit)
        aw = int(s * 0.38)
        ah = int(s * 0.14)
        ax = dx + dw + int(s*0.04)
        d.line([(ax, cy), (ax + aw, cy)], fill=c, width=lw)
        d.polygon([(ax + aw, cy), (ax + aw - ah, cy - ah),
                   (ax + aw - ah, cy + ah)], fill=c)


def make_image() -> Image.Image:
    img = Image.new("RGB", (W, H), BORDEAUX)
    d   = ImageDraw.Draw(img)

    try:
        f_en = ImageFont.truetype(FONT_EN_BOLD, 58)
    except Exception:
        f_en = ImageFont.load_default()
    try:
        f_th = ImageFont.truetype(FONT_THAI, 82)
    except Exception:
        f_th = f_en

    ICON_SIZE = 140

    for (row, col, bg, icon_name, en_label, th_label, _) in CELLS:
        x0, y0 = col * CW, row * CH
        x1, y1 = x0 + CW, y0 + CH
        d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=bg)
        d.line([(x1 - 1, y0), (x1 - 1, y1)], fill=SEP, width=3)
        d.line([(x0, y1 - 1), (x1, y1 - 1)], fill=SEP, width=3)

        cx = x0 + CW // 2

        en_bb  = d.textbbox((0, 0), en_label, font=f_en)
        en_h   = en_bb[3] - en_bb[1]
        th_bb  = d.textbbox((0, 0), th_label, font=f_th)
        th_h   = th_bb[3] - th_bb[1]

        gap   = 30
        total = ICON_SIZE + gap + th_h + gap + en_h
        top   = y0 + (CH - total) // 2

        draw_icon(d, cx, top + ICON_SIZE // 2, icon_name, ICON_SIZE)

        th_y = top + ICON_SIZE + gap
        th_w = th_bb[2] - th_bb[0]
        d.text((cx - th_w // 2, th_y), th_label, font=f_th, fill=WHITE)

        en_y = th_y + th_h + gap - 5
        en_w = en_bb[2] - en_bb[0]
        d.text((cx - en_w // 2, en_y), en_label, font=f_en, fill=LIGHT)

    return img


def build_richmenu_def() -> dict:
    areas = []
    for (row, col, _bg, _icon, _en, _th, action_text) in CELLS:
        areas.append({
            "bounds": {"x": col * CW, "y": row * CH, "width": CW, "height": CH},
            "action": {"type": "message", "text": action_text}
        })
    return {
        "size": {"width": W, "height": H},
        "selected": True,
        "name": "Mister Cochon Retail",
        "chatBarText": "เมนู",
        "areas": areas
    }


def delete_existing(token: str):
    hdrs = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{LINE_API}/richmenu/list", headers=hdrs)
    for m in (r.json().get("richmenus") or []):
        requests.delete(f"{LINE_API}/richmenu/{m['richMenuId']}", headers=hdrs)


def create_and_activate(token: str, img: Image.Image) -> str:
    hdrs = {"Authorization": f"Bearer {token}"}
    r = requests.post(f"{LINE_API}/richmenu",
        headers={**hdrs, "Content-Type": "application/json"},
        data=json.dumps(build_richmenu_def()))
    r.raise_for_status()
    mid = r.json()["richMenuId"]
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    requests.post(f"https://api-data.line.me/v2/bot/richmenu/{mid}/content",
        headers={**hdrs, "Content-Type": "image/png"}, data=buf.read()).raise_for_status()
    requests.post(f"{LINE_API}/user/all/richmenu/{mid}", headers=hdrs).raise_for_status()
    return mid
