"""
Generate and activate the LINE Rich Menu for French Delicatessen PRO bot.
Run once via Render endpoint, or: python setup_richmenu.py
"""
import os, sys, json, io, math, requests
from PIL import Image, ImageDraw, ImageFont

LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
if not LINE_TOKEN:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith("LINE_CHANNEL_ACCESS_TOKEN="):
                LINE_TOKEN = line.strip().split("=", 1)[1].strip().strip('"')

LINE_API = "https://api.line.me/v2/bot"
HEADERS  = {"Authorization": f"Bearer {LINE_TOKEN}"}

# ── Dimensions: half-height rich menu ────────────────────────────────────────
W, H  = 2500, 843
COLS  = 3
ROWS  = 2
CW    = W // COLS        # 833
CH    = H // ROWS        # 421

NAVY   = "#1A3A6B"
RED    = "#C8102E"
WHITE  = "#FFFFFF"
LIGHT  = "#AABBD4"      # muted blue for Thai text
SEP    = "#2E5096"      # separator line color

# Fonts — bundled in fonts/ directory for cross-platform (Windows + Linux/Render)
_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_EN_BOLD = os.path.join(_DIR, "fonts", "Montserrat-Bold.ttf")
FONT_THAI    = os.path.join(_DIR, "fonts", "NotoSansThai.ttf")

CELLS = [
    # row, col, bg,   icon,      en_label,          th_label,           action
    (0, 0, NAVY, "catalog",  "Product Catalog",  "แคตตาล็อกสินค้า",  "menu"),
    (0, 1, NAVY, "cart",     "My Cart",          "ตะกร้าสินค้า",      "cart"),
    (0, 2, NAVY, "reorder",  "Reorder",          "สั่งซ้ำ",            "reorder"),
    (1, 0, NAVY, "orders",   "My Orders",        "ประวัติคำสั่งซื้อ",  "orders"),
    (1, 1, NAVY, "help",     "Help",             "ช่วยเหลือ",          "help"),
    (1, 2, RED,  "contact",  "Contact us",       "ติดต่อเรา",          "contact"),
]


def draw_icon(d: ImageDraw, cx: int, cy: int, name: str, size: int):
    """Draw a clean icon centered at (cx, cy) in white."""
    s  = size
    lw = max(3, s // 14)
    c  = WHITE

    if name == "catalog":
        # Open book
        spine_x = cx
        w2 = int(s * 0.45)
        h2 = int(s * 0.55)
        # Left page
        d.polygon([(spine_x, cy - h2), (spine_x - w2, cy - h2 + 10),
                   (spine_x - w2, cy + h2), (spine_x, cy + h2 - 5)],
                  outline=c, width=lw)
        # Right page
        d.polygon([(spine_x, cy - h2), (spine_x + w2, cy - h2 + 10),
                   (spine_x + w2, cy + h2), (spine_x, cy + h2 - 5)],
                  outline=c, width=lw)
        # Lines on right page
        for i in range(1, 4):
            y = cy - h2 + 10 + int((h2 * 2 - 15) * i / 4.5)
            d.line([(spine_x + lw*2, y), (spine_x + w2 - lw*3, y)], fill=c, width=lw//2 or 1)

    elif name == "cart":
        # Shopping bag
        bag_w = int(s * 0.55)
        bag_h = int(s * 0.55)
        bag_x = cx - bag_w // 2
        bag_y = cy - int(s * 0.1)
        # Handle
        hr = int(s * 0.22)
        d.arc([cx - hr, bag_y - hr * 2, cx + hr, bag_y],
              start=180, end=0, fill=c, width=lw)
        # Bag body
        d.rounded_rectangle([bag_x, bag_y, bag_x + bag_w, bag_y + bag_h],
                             radius=lw * 2, outline=c, width=lw)

    elif name == "reorder":
        # Two arrows in a circle (refresh)
        r = int(s * 0.45)
        # Top arc + arrow
        d.arc([cx - r, cy - r, cx + r, cy + r], start=200, end=20, fill=c, width=lw)
        # Top arrowhead (pointing right at ~20°)
        ax = cx + int(r * math.cos(math.radians(20)))
        ay = cy + int(r * math.sin(math.radians(20)))
        a = int(s * 0.12)
        d.polygon([(ax, ay), (ax - a, ay - a), (ax - a//2, ay + a)], fill=c)
        # Bottom arc + arrow
        d.arc([cx - r, cy - r, cx + r, cy + r], start=20, end=200, fill=c, width=lw)
        ax2 = cx + int(r * math.cos(math.radians(200)))
        ay2 = cy + int(r * math.sin(math.radians(200)))
        d.polygon([(ax2, ay2), (ax2 + a, ay2 + a), (ax2 + a//2, ay2 - a)], fill=c)

    elif name == "orders":
        # Clipboard
        bx, by = cx - int(s * 0.38), cy - int(s * 0.48)
        bw, bh = int(s * 0.76), int(s * 0.96)
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=lw * 2, outline=c, width=lw)
        # Clip at top
        cw = int(s * 0.28)
        d.rectangle([cx - cw // 2, by - lw * 2, cx + cw // 2, by + lw * 4], fill=c)
        # Lines
        for i in range(3):
            y = by + int(bh * (0.35 + i * 0.2))
            d.line([(bx + lw * 3, y), (bx + bw - lw * 3, y)], fill=c, width=lw // 2 or 1)

    elif name == "help":
        # Circle with ?
        r = int(s * 0.48)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=lw)
        try:
            fq = ImageFont.truetype(FONT_EN_BOLD, int(s * 0.65))
        except Exception:
            fq = ImageFont.load_default()
        bb = d.textbbox((0, 0), "?", font=fq)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        d.text((cx - tw // 2, cy - th // 2 - th // 8), "?", font=fq, fill=c)

    elif name == "contact":
        # Speech bubble
        bx, by = cx - int(s * 0.48), cy - int(s * 0.42)
        bw, bh = int(s * 0.96), int(s * 0.72)
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=int(s * 0.12),
                             outline=c, width=lw)
        # Tail
        tail_x = cx - int(s * 0.15)
        tail_y = by + bh
        d.polygon([(tail_x, tail_y), (tail_x - int(s * 0.12), tail_y + int(s * 0.22)),
                   (tail_x + int(s * 0.15), tail_y)], fill=c)
        # Dots
        dot_r = lw + 1
        for dx in [-int(s * 0.18), 0, int(s * 0.18)]:
            d.ellipse([cx + dx - dot_r, cy - dot_r - int(s * 0.06),
                       cx + dx + dot_r, cy + dot_r - int(s * 0.06)], fill=c)


def make_image() -> Image.Image:
    img = Image.new("RGB", (W, H), NAVY)
    d   = ImageDraw.Draw(img)

    try:
        f_en = ImageFont.truetype(FONT_EN_BOLD, 62)
    except Exception:
        f_en = ImageFont.load_default()
    try:
        f_th = ImageFont.truetype(FONT_THAI, 88)
    except Exception:
        f_th = f_en

    ICON_SIZE = 150   # icon fits in this box

    for (row, col, bg, icon_name, en_label, th_label, _action) in CELLS:
        x0, y0 = col * CW, row * CH
        x1, y1 = x0 + CW, y0 + CH

        d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=bg)

        # Separator lines
        d.line([(x1 - 1, y0), (x1 - 1, y1)], fill=SEP, width=3)
        d.line([(x0, y1 - 1), (x1, y1 - 1)], fill=SEP, width=3)

        cx = x0 + CW // 2

        # Vertical layout: icon — TH label (big) — EN label (small)
        en_bb = d.textbbox((0, 0), en_label, font=f_en)
        en_h  = en_bb[3] - en_bb[1]
        th_bb = d.textbbox((0, 0), th_label, font=f_th)
        th_h  = th_bb[3] - th_bb[1]

        gap    = 20
        total  = ICON_SIZE + gap + th_h + gap + en_h
        top    = y0 + (CH - total) // 2

        # Icon center
        icon_cy = top + ICON_SIZE // 2
        draw_icon(d, cx, icon_cy, icon_name, ICON_SIZE)

        # Thai label (primary — larger, on top)
        th_y = top + ICON_SIZE + gap
        th_w = th_bb[2] - th_bb[0]
        d.text((cx - th_w // 2, th_y), th_label, font=f_th, fill=WHITE)

        # English label (secondary — smaller, below)
        en_y = th_y + th_h + gap
        en_w = en_bb[2] - en_bb[0]
        d.text((cx - en_w // 2, en_y), en_label, font=f_en, fill=LIGHT)

    return img


# ── LINE Rich Menu definition ────────────────────────────────────────────────

def build_richmenu_def() -> dict:
    areas = []
    for (row, col, _bg, _icon, _en, _th, action) in CELLS:
        areas.append({
            "bounds": {"x": col * CW, "y": row * CH, "width": CW, "height": CH},
            "action": {"type": "message", "text": action}
        })
    return {
        "size": {"width": W, "height": H},
        "selected": True,
        "name": "French Delicatessen PRO",
        "chatBarText": "Menu",
        "areas": areas
    }


def delete_existing(token: str):
    hdrs = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{LINE_API}/richmenu/list", headers=hdrs)
    for m in r.json().get("richmenus", []):
        requests.delete(f"{LINE_API}/richmenu/{m['richMenuId']}", headers=hdrs)
        print(f"  Deleted: {m['richMenuId']}")


def create_and_activate(token: str, img: Image.Image) -> str:
    hdrs = {"Authorization": f"Bearer {token}"}
    # Create
    r = requests.post(f"{LINE_API}/richmenu",
        headers={**hdrs, "Content-Type": "application/json"},
        data=json.dumps(build_richmenu_def()))
    r.raise_for_status()
    mid = r.json()["richMenuId"]
    print(f"  Created: {mid}")
    # Upload image
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    r2 = requests.post(f"https://api-data.line.me/v2/bot/richmenu/{mid}/content",
        headers={**hdrs, "Content-Type": "image/png"}, data=buf.read())
    r2.raise_for_status()
    print(f"  Image uploaded")
    # Set default
    r3 = requests.post(f"{LINE_API}/user/all/richmenu/{mid}", headers=hdrs)
    r3.raise_for_status()
    print(f"  Activated as default")
    return mid


if __name__ == "__main__":
    if not LINE_TOKEN:
        print("ERROR: LINE_CHANNEL_ACCESS_TOKEN not set.")
        sys.exit(1)
    print("Generating image...")
    img = make_image()
    img.save(os.path.join(os.path.dirname(__file__), "richmenu_preview.png"))
    print("Removing old rich menus...")
    delete_existing(LINE_TOKEN)
    mid = create_and_activate(LINE_TOKEN, img)
    print(f"\nDone! Rich menu active: {mid}")
