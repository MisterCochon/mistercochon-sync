               f"Cart: {len(cart)} item{'s' if len(cart)>1 else ''} — {total:,.0f} ฿"
            )
            prods = sess.get("category_products", [])
            cur_page = sess.get("page", 0)
            msgs = [confirm_msg]
            if prods:
                msgs += _line_build_carousel(prods, pricelist, cur_page)
            line_reply(reply_token, msgs[:5])
            continue

        # ── Qty button: __aq_{product_id}_{sku}_{price}_{qty} ────────────
        if text.startswith("__aq_"):
            # parts: ['','','aq', pid, sku, price, qty]
            parts = text.split("_")
            try:
                qty        = int(parts[-1])
                price_int  = int(parts[-2])
                sku        = parts[-3].upper()
                product_id = int(parts[-4]) if len(parts) >= 7 and parts[-4].isdigit() else 0
                price      = float(price_int)
            except (ValueError, IndexError):
                line_reply(reply_token, [line_text("Invalid selection. Please try again.")])
                continue
            # Get name from session cache — no Odoo call needed
            sess  = _line_sessions.get(user_id, {})
            prods = sess.get("category_products", [])
            prod  = next((p for p in prods if p.get("id") == product_id
                          or str(p.get("default_code") or "").upper() == sku), None)
            name  = prod.get("name", sku) if prod else sku
            if prod and not product_id:
                product_id = prod["id"]
            cart = list(sess.get("cart", []))
            for item in cart:
                if item["sku"] == sku:
                    item["qty"] += qty
                    break
            else:
                cart.append({"sku": sku, "name": name,
                              "price": price, "product_id": product_id, "qty": qty})
            _line_sessions[user_id] = {**sess, "cart": cart}
            short = _shorten_product_name(name)
            total = sum(i["price"] * i["qty"] for i in cart)
            # Confirmation + return to product list
            confirm_msg = line_text(
                f"✅ {short} ×{qty} added\n"
                f"Cart: {len(cart)} item{'s' if len(cart)>1 else ''} — {total:,.0f} ฿"
            )
            prods = sess.get("category_products", [])
            cur_page = sess.get("page", 0)
            msgs = [confirm_msg]
            if prods:
                msgs += _line_build_carousel(prods, pricelist, cur_page)
            line_reply(reply_token, msgs[:5])
            continue

        # ── Cart display ───────────────────────────────────────────────────
        if text_low in ("cart", "panier", "my cart"):
            cart = _line_sessions.get(user_id, {}).get("cart", [])
            if not cart:
                line_reply(reply_token, [line_quick_reply(
                    "Your cart is empty.",
                    [("Browse catalog", "menu"), ("Reorder last", "reorder")]
                )])
            else:
                line_reply(reply_token, _line_cart_messages(cart))
            continue

        # ── Checkout ───────────────────────────────────────────────────────
        if text_low in ("checkout", "confirm", "order", "commander"):
            cart = _line_sessions.get(user_id, {}).get("cart", [])
            if not cart:
                line_reply(reply_token, [line_quick_reply(
                    "🛒 Your cart is empty. Browse our catalog first.",
                    [("📋 Browse catalog", "menu")]
                )])
            else:
                result = _line_create_order(partner, cart)
                sess = _line_sessions.get(user_id, {})
                _line_sessions[user_id] = {k: v for k, v in sess.items()
                                            if k not in ("cart", "pending_product")}
                line_reply(reply_token, [line_text(result)])
            continue

        # ── Cancel / clear cart ────────────────────────────────────────────
        if text_low in ("cancel", "clear", "annuler"):
            sess = _line_sessions.get(user_id, {})
            _line_sessions[user_id] = {k: v for k, v in sess.items()
                                        if k not in ("cart", "pending_product")}
            line_reply(reply_token, [line_quick_reply(
                "🗑️ Cart cleared.",
                [("📋 Browse catalog", "menu"), ("❓ Help", "help")]
            )])
            continue

        # ── Sub-family selected: __subcat_{tag_id}_{categ_id} ─────────────
        if text.startswith("__subcat_"):
            parts = text[9:].split("_")
            tag_id = int(parts[0])
            categ_id = int(parts[1])
            domain = [["active", "=", True], ["sale_ok", "=", True],
                       ["product_tag_ids", "in", [tag_id]],
                       ["categ_id", "=", categ_id]]
            prods = odoo_execute("product.product", "search_read",
                [domain],
                {"fields": ["id", "name", "default_code", "list_price", "description_sale"], "limit": 200,
                 "context": {"lang": "en_US"}})
            if not prods:
                line_reply(reply_token, [line_text("No products in this category.")])
                continue
            _line_sessions[user_id] = {**_line_sessions.get(user_id, {}),
                                        "category_products": prods, "page": 0}
            line_reply(reply_token, _line_build_carousel(prods, pricelist, 0))
            continue

        # ── Category selected ──────────────────────────────────────────────
        if text.startswith("__cat_"):
            cat_id = int(text.split("__cat_")[1])
            line_tag_id = _line_get_line_tag_id()
            if cat_id > 0:
                # Check if this tag uses sub-families (Gamme Tradition only)
                TAGS_WITH_SUBFAMILIES = {"LINE-Gamme Tradition"}
                tag_rows = odoo_execute("product.tag", "search_read",
                    [[["id", "=", cat_id]]], {"fields": ["name"], "limit": 1})
                tag_name = tag_rows[0]["name"] if tag_rows else ""
                if tag_name in TAGS_WITH_SUBFAMILIES:
                    subcats = _line_get_subcategories_for_tag(cat_id)
                    if subcats:
                        line_reply(reply_token, [_line_build_subcat_flex(cat_id, subcats, tag_name[5:])])
                        continue
                # Regular tag: filter products directly
                domain = [["active", "=", True], ["sale_ok", "=", True],
                           ["product_tag_ids", "in", [cat_id]]]
            else:
                # Mode Odoo category: filter by categ + LINE tag
                domain = [["active", "=", True], ["sale_ok", "=", True],
                           ["categ_id", "=", -cat_id]]
                if line_tag_id:
                    domain.append(["product_tag_ids", "in", [line_tag_id]])
            prods = odoo_execute("product.product", "search_read",
                [domain],
                {"fields": ["id", "name", "default_code", "list_price", "description_sale"], "limit": 200,
                 "context": {"lang": "en_US"}}
            )
            if not prods:
                line_reply(reply_token, [line_text("No products in this category.")])
                continue
            _line_sessions[user_id] = {**_line_sessions.get(user_id, {}),
                                        "category_products": prods, "page": 0}
            line_reply(reply_token, _line_build_carousel(prods, pricelist, 0))
            continue

        # ── Menu / catalogue ───────────────────────────────────────────────
        if text_low in ("menu", "catalog", "catalogue", "products", "shop"):
            cats = _line_get_pro_categories(_line_extra_tags_for_partner(partner))
            if not cats:
                line_reply(reply_token, [line_text("No PRO products available yet.")])
                continue
            # Single category → skip menu, show products directly
            if len(cats) == 1:
                cat_id, label = cats[0]
                line_tag_id = _line_get_line_tag_id()
                if cat_id > 0:
                    domain = [["active", "=", True], ["sale_ok", "=", True],
                               ["product_tag_ids", "in", [cat_id]]]
                else:
                    domain = [["active", "=", True], ["sale_ok", "=", True],
                               ["categ_id", "=", -cat_id]]
                    if line_tag_id:
                        domain.append(["product_tag_ids", "in", [line_tag_id]])
                prods = odoo_execute("product.product", "search_read",
                    [domain],
                    {"fields": ["id", "name", "default_code", "list_price", "description_sale"], "limit": 200,
                     "context": {"lang": "en_US"}}
                )
                _line_sessions[user_id] = {"category_products": prods, "page": 0}
                line_reply(reply_token, _line_build_carousel(prods, pricelist, 0))
                continue
            # Flex bubble with category buttons — all visible on one screen
            line_reply(reply_token, [_line_build_cat_flex(cats)])

        # ── My orders ─────────────────────────────────────────────────────
        elif text_low in ("orders", "my orders", "history"):
            orders = odoo_execute("sale.order", "search_read",
                [[["partner_id", "=", partner["id"]]]],
                {"fields": ["name", "date_order", "amount_total", "state"],
                 "limit": 5, "order": "date_order desc"}
            )
            if not orders:
                line_reply(reply_token, [line_text("No orders found.")])
            else:
                lines = ["📦 *Your recent orders:*\n"]
                for o in orders:
                    date = str(o["date_order"])[:10]
                    state = {"draft": "Draft", "sale": "Confirmed",
                              "done": "Done", "cancel": "Cancelled"}.get(o["state"], o["state"])
                    lines.append(f"• {o['name']} — {o['amount_total']:.0f} ฿ — {state} ({date})")
                line_reply(reply_token, [line_quick_reply(
                    "\n".join(lines),
                    [("Reorder last", "reorder"), ("New order", "menu")]
                )])

        # ── Reorder: show previously ordered products as carousel ──────────
        elif text_low in ("reorder", "recommander", "last order"):
            # Collect unique products from last 5 confirmed orders
            past_orders = odoo_execute("sale.order", "search_read",
                [[["partner_id", "child_of", partner["id"]], ["state", "in", ["sale", "done"]]]],
                {"fields": ["id"], "limit": 5, "order": "date_order desc"}
            )
            if not past_orders:
                line_reply(reply_token, [line_quick_reply(
                    "No previous orders found.",
                    [("Browse catalog", "menu")]
                )])
            else:
                oid_list = [o["id"] for o in past_orders]
                all_lines = odoo_execute("sale.order.line", "search_read",
                    [[["order_id", "in", oid_list]]],
                    {"fields": ["product_id", "product_uom_qty"], "limit": 200}
                )
                # Deduplicate products, keep max qty ordered
                seen, prod_ids = {}, []
                for ln in all_lines:
                    if not ln.get("product_id"):
                        continue
                    pid = ln["product_id"][0]
                    qty = int(ln.get("product_uom_qty", 1))
                    if pid not in seen:
                        seen[pid] = qty
                        prod_ids.append(pid)
                    else:
                        seen[pid] = max(seen[pid], qty)

                if not prod_ids:
                    line_reply(reply_token, [line_quick_reply(
                        "No products found in previous orders.",
                        [("Browse catalog", "menu")]
                    )])
                else:
                    domain = [["id", "in", prod_ids],
                              ["name", "not ilike", "frozen"],
                              ["name", "not ilike", "livraison"],
                              ["name", "not ilike", "delivery"]]
                    prods = odoo_execute("product.product", "search_read",
                        [domain],
                        {"fields": ["id", "name", "default_code", "list_price", "description_sale"],
                         "limit": 100, "context": {"lang": "en_US", "active_test": False}}
                    )
                    if not prods:
                        line_reply(reply_token, [line_quick_reply(
                            "No catalog products found in previous orders.",
                            [("Browse catalog", "menu")]
                        )])
                    else:
                        sess = _line_sessions.get(user_id, {})
                        _line_sessions[user_id] = {**sess, "category_products": prods, "page": 0}
                        line_reply(reply_token,
                            _line_build_carousel(prods, pricelist, 0, "Your usual products")
                        )

        # ── Contact (human support) ────────────────────────────────────────
        elif text_low in ("contact", "ติดต่อ", "ติดต่อเรา"):
            client_name = partner.get("name", "Unknown client")
            line_reply(reply_token, [line_text(
                "Our team will contact you shortly.\n\n"
                "ทีมงานของเราจะติดต่อกลับหาคุณโดยเร็ว\n\n"
                "📞 LINE: @jfbuc"
            )])
            # Notify admin
            admin_id = os.getenv("LINE_ADMIN_ID", "")
            if admin_id:
                try:
                    requests.post("https://api.line.me/v2/bot/message/push",
                        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                                 "Content-Type": "application/json"},
                        json={"to": admin_id, "messages": [{"type": "text",
                            "text": f"📩 Contact request\nClient: {client_name}\nLINE ID: {user_id}\n\nReply directly in OA Manager."}]},
                        timeout=5)
                except Exception:
                    pass

        # ── My LINE ID (admin helper) ──────────────────────────────────────
        elif text == "!myid":
            line_reply(reply_token, [line_text(f"Your LINE User ID:\n{user_id}")])

        # ── SKU search prompt ─────────────────────────────────────────────
        elif text_low in ("search_sku", "search", "ค้นหา sku"):
            _line_sessions[user_id] = {**_line_sessions.get(user_id, {}), "state": "awaiting_sku"}
            line_reply(reply_token, [line_text(
                "🔍 Type a SKU code to search:\n\n"
                "Example: JAMB2160 or MNCL2029"
            )])

        # ── Help ──────────────────────────────────────────────────────────
        elif text_low in ("help", "?"):
            line_reply(reply_token, [line_text(
                "🇫🇷 *French Delicatessen — B2B Portal*\n\n"
                "📋 *menu* — Browse the PRO catalog\n"
                "🔄 *reorder* — Your usual products (last 5 orders)\n"
                "🛒 *cart* — View your current cart\n"
                "✅ *checkout* — Confirm & place your order\n"
                "🗑️ *cancel* — Clear your cart\n"
                "📦 *orders* — View your recent orders\n"
                "🔍 *FD001* — Search directly by SKU code\n\n"
                "📞 Support: @jfbuc"
            )])

        # ── SKU lookup: type a SKU to open product detail ─────────────────
        else:
            sess = _line_sessions.get(user_id, {})
            awaiting_sku = sess.get("state") == "awaiting_sku"
            sku_match = re.match(r"^([A-Za-z0-9_\-]{2,20})(?:\s+(\d+))?$", text.strip()) if awaiting_sku else None
            if sku_match:
                raw_sku = sku_match.group(1).upper()
                prods_found = odoo_execute("product.product", "search_read",
                    [[["default_code", "=ilike", raw_sku]]],
                    {"fields": ["id", "name", "default_code", "list_price", "description_sale"],
                     "limit": 1, "context": {"lang": "en_US", "active_test": False}}
                )
                if prods_found:
                    p = prods_found[0]
                    sess = _line_sessions.get(user_id, {})
                    back_page = sess.get("page", 0)
                    _line_sessions[user_id] = {k: v for k, v in sess.items() if k != "state"}
                    line_reply(reply_token, _line_product_detail(p, pricelist, back_page))
                else:
                    line_reply(reply_token, [line_quick_reply(
                        f"❌ SKU *{raw_sku}* not found.\n\nBrowse the catalog to find products:",
                        [("📋 Catalog", "menu"), ("🛒 My cart", "cart")]
                    )])
            else:
                line_reply(reply_token, [line_quick_reply(
                    "What would you like to do?",
                    [("📋 Catalog", "menu"),
                     ("🛒 My cart", "cart"),
                     ("📦 Orders", "orders"),
                     ("❓ Help", "help")]
                )])

    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════════════════
# LINE RETAIL BOT — Mister Cochon (@920gsiph)
# ═══════════════════════════════════════════════════════════════════════════════

LINE_RETAIL_SECRET  = os.getenv("LINE_RETAIL_CHANNEL_SECRET", "")
LINE_RETAIL_TOKEN   = os.getenv("LINE_RETAIL_ACCESS_TOKEN", "")
PROMPTPAY_NUMBER    = os.getenv("PROMPTPAY_NUMBER", "0957291373")
STRIPE_SECRET_KEY   = os.getenv("STRIPE_SECRET_KEY", "")
if STRIPE_SECRET_KEY:
    _stripe.api_key = STRIPE_SECRET_KEY
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://mistercochon-backend.onrender.com")

_retail_sessions: dict = {}


def retail_reply(reply_token: str, messages: list):
    requests.post("https://api.line.me/v2/bot/message/reply",
        headers={"Authorization": f"Bearer {LINE_RETAIL_TOKEN}",
                 "Content-Type": "application/json"},
        json={"replyToken": reply_token, "messages": messages[:5]},
        timeout=10)


def retail_push(user_id: str, messages: list):
    requests.post("https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_RETAIL_TOKEN}",
                 "Content-Type": "application/json"},
        json={"to": user_id, "messages": messages[:5]},
        timeout=10)


def _retail_get_partner(user_id: str) -> dict | None:
    results = odoo_execute("res.partner", "search_read",
        [[["comment", "like", f"line_retail:{user_id}"]]],
        {"fields": ["id", "name", "email", "property_product_pricelist"], "limit": 1}
    )
    return results[0] if results else None


def _retail_email_login(email: str) -> dict | None:
    results = odoo_execute("res.partner", "search_read",
        [[["email", "=ilike", email.strip()], ["customer_rank", ">", 0]]],
        {"fields": ["id", "name", "email", "comment"], "limit": 1}
    )
    return results[0] if results else None


def _retail_get_categories() -> list:
    """Return (categ_id, label) pairs from Odoo product categories that have active sale products."""
    cats = odoo_execute("product.category", "search_read",
        [[]],
        {"fields": ["id", "name"], "limit": 100}
    )
    EXCLUDE = {"all", "tous", "all products", "deliveries", "livraisons",
               "matieres premieres", "matières premières", "pro", "expense",
               "expenses", "default", "internal"}
    result = []
    for c in (cats or []):
        if c["name"].lower() in EXCLUDE:
            continue
        count = odoo_execute("product.product", "search_count",
            [[["active", "=", True], ["sale_ok", "=", True], ["categ_id", "=", c["id"]]]]
        )
        if count:
            result.append((c["id"], c["name"]))
    return result[:10]


def _retail_build_cat_flex(cats: list) -> dict:
    buttons = []
    for cid, label in cats[:10]:
        buttons.append({
            "type": "button", "style": "secondary", "height": "sm",
            "action": {"type": "postback", "label": label[:40], "data": f"__rcat_{cid}"}
        })
    return {
        "type": "flex", "altText": "เลือกหมวดหมู่สินค้า",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical",
                "backgroundColor": "#8B0000", "paddingAll": "14px",
                "contents": [{"type": "text", "text": "🐷 Mister Cochon",
                              "weight": "bold", "color": "#FFFFFF", "size": "md"}]
            },
            "body": {
                "type": "box", "layout": "vertical",
                "paddingAll": "10px", "spacing": "sm",
                "contents": buttons
            }
        }
    }


def _retail_checkout_messages(cart: list, partner: dict) -> list:
    """Generate checkout message with PromptPay info and QR code."""
    total = sum(i["qty"] * i["price"] for i in cart)
    lines = []
    for i in cart:
        lines.append(f"• {i['name'][:30]} ×{i['qty']} = {i['qty']*i['price']:,.0f}฿")
    order_text = "\n".join(lines)
    # PromptPay QR code with amount via promptpay.io
    qr_url = f"https://promptpay.io/{PROMPTPAY_NUMBER}/{int(total)}.png"
    return [
        {
            "type": "flex", "altText": f"ยอดชำระ {total:,.0f}฿",
            "contents": {
                "type": "bubble",
                "header": {
                    "type": "box", "layout": "vertical",
                    "backgroundColor": "#8B0000", "paddingAll": "14px",
                    "contents": [{"type": "text", "text": "💳 ชำระเงิน / Payment",
                                  "weight": "bold", "color": "#FFFFFF", "size": "md"}]
                },
                "body": {
                    "type": "box", "layout": "vertical", "spacing": "md",
                    "contents": [
                        {"type": "text", "text": order_text, "wrap": True, "size": "sm", "color": "#333333"},
                        {"type": "separator"},
                        {"type": "box", "layout": "horizontal", "contents": [
                            {"type": "text", "text": "ยอดรวม / Total", "weight": "bold", "flex": 2},
                            {"type": "text", "text": f"{total:,.0f} ฿", "weight": "bold",
                             "color": "#C8102E", "align": "end", "flex": 1}
                        ]},
                        {"type": "separator"},
                        {"type": "text", "text": "📱 PromptPay", "weight": "bold", "size": "sm"},
                        {"type": "text", "text": PROMPTPAY_NUMBER, "size": "xl",
                         "weight": "bold", "color": "#1A3A6B", "align": "center"},
                        {"type": "text", "text": "Mister Cochon / French Delicatessen",
                         "size": "xs", "color": "#888888", "align": "center"},
                        {"type": "separator"},
                        {"type": "text", "wrap": True, "size": "xs", "color": "#666666",
                         "text": "สแกน QR หรือโอนผ่านเบอร์โทร\nScan QR or transfer via phone number\nแล้วส่งสลิปในแชทนี้ / Then send slip here."}
                    ]
                }
            }
        },
        {
            "type": "image",
            "originalContentUrl": qr_url,
            "previewImageUrl": qr_url
        }
    ]


@app.post("/webhook/line-retail")
async def webhook_line_retail(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"status": "ignored"}

    for event in body.get("events", []):
        event_type = event.get("type")
        reply_token = event.get("replyToken", "")
        user_id = event.get("source", {}).get("userId", "")
        sess = _retail_sessions.get(user_id, {})

        # ── Handle image (ignored — payment via Stripe QR) ─────────────────
        if event_type == "message" and event.get("message", {}).get("type") == "image":
            continue

        # ── Extract text ───────────────────────────────────────────────────
        if event_type == "postback":
            text = event.get("postback", {}).get("data", "").strip()
        elif event_type == "message" and event.get("message", {}).get("type") == "text":
            text = event["message"]["text"].strip()
        else:
            continue

        text_low = text.lower().strip()
        partner = _retail_get_partner(user_id)

        # ── Not authenticated ──────────────────────────────────────────────
        if not partner:
            if sess.get("state") == "awaiting_confirm":
                if text_low in ("ใช่", "yes", "ยืนยัน", "confirm", "ok", "✅"):
                    pid = sess["partner_id"]
                    pname = sess["partner_name"]
                    p_raw = odoo_execute("res.partner", "read", [[pid], ["comment"]], {})[0]
                    comment = (p_raw.get("comment") or "") + f"\nline_retail:{user_id}"
                    odoo_execute("res.partner", "write", [[pid], {"comment": comment.strip()}])
                    _retail_sessions[user_id] = {}
                    partner = _retail_get_partner(user_id)
                    cats = _retail_get_categories()
                    retail_reply(reply_token, [
                        line_text(f"✅ ยินดีต้อนรับ คุณ{pname}!\nWelcome, {pname}!"),
                        _retail_build_cat_flex(cats) if cats else line_text("พิมพ์ เมนู เพื่อดูสินค้า")
                    ])
                elif text_low in ("ไม่ใช่", "no", "ไม่", "cancel"):
                    _retail_sessions.pop(user_id, None)
                    retail_reply(reply_token, [line_text(
                        "กรุณากรอกอีเมลของคุณอีกครั้ง\nPlease enter your email again."
                    )])
                else:
                    retail_reply(reply_token, [line_text(
                        "กรุณาพิมพ์ ใช่ หรือ ไม่ใช่\nPlease reply ใช่ (yes) or ไม่ใช่ (no)"
                    )])
                continue

            p = _retail_email_login(text)
            if p:
                _retail_sessions[user_id] = {
                    "state": "awaiting_confirm",
                    "partner_id": p["id"],
                    "partner_name": p["name"],
                }
                retail_reply(reply_token, [line_text(
                    f"✅ พบบัญชี:\n*{p['name']}*\n\nถูกต้องไหม? Is this you?\n\n"
                    "ใช่ / Yes   |   ไม่ใช่ / No"
                )])
            else:
                retail_reply(reply_token, [line_text(
                    "🐷 *Mister Cochon* — French Delicatessen\n\n"
                    "กรุณากรอกอีเมลที่ลงทะเบียนไว้:\n"
                    "Please enter your registered email address:"
                )])
            continue

        # ── Authenticated ──────────────────────────────────────────────────
        pricelist_id = partner.get("property_product_pricelist")
        pricelist = pricelist_id[0] if isinstance(pricelist_id, (list, tuple)) else pricelist_id
        cart = sess.get("cart", [])

        # ── Logout ────────────────────────────────────────────────────────
        if text_low in ("logout", "ออกจากระบบ"):
            p_raw = odoo_execute("res.partner", "read", [[partner["id"]], ["comment"]], {})[0]
            comment = (p_raw.get("comment") or "").replace(f"line_retail:{user_id}", "").strip()
            odoo_execute("res.partner", "write", [[partner["id"]], {"comment": comment}])
            _retail_sessions.pop(user_id, None)
            retail_reply(reply_token, [line_text(
                "👋 ออกจากระบบแล้ว\nYou have been logged out.\n\nกรอกอีเมลเพื่อเข้าสู่ระบบใหม่\nEnter your email to log back in."
            )])
            continue

        # ── Postback: category selected ────────────────────────────────────
        if text.startswith("__rcat_"):
            cat_id = int(text.split("__rcat_")[1])
            domain = [["active", "=", True], ["sale_ok", "=", True],
                      ["categ_id", "=", cat_id]]
            prods = odoo_execute("product.product", "search_read",
                [domain],
                {"fields": ["id", "name", "default_code", "list_price", "description_sale"],
                 "limit": 200, "context": {"lang": "en_US"}}
            )
            _retail_sessions[user_id] = {**sess, "category_products": prods, "page": 0}
            retail_reply(reply_token, _line_build_carousel(prods, pricelist, 0))
            continue

        # ── Postback: view product detail ──────────────────────────────────
        if text.startswith("__view_"):
            raw = text[7:]
            first, _, sku = raw.partition("_")
            product_id = int(first) if first.isdigit() else 0
            prods = sess.get("category_products", [])
            prod = next((p for p in prods if p.get("id") == product_id), None)
            if not prod:
                found = odoo_execute("product.product", "search_read",
                    [[["id", "=", product_id]]],
                    {"fields": ["id", "name", "default_code", "list_price", "description_sale"],
                     "limit": 1, "context": {"lang": "en_US"}}
                )
                prod = found[0] if found else None
            if prod:
                retail_reply(reply_token, _line_product_detail(prod, pricelist, sess.get("page", 0)))
            continue

        # ── Postback: page navigation ──────────────────────────────────────
        if text.startswith("__page_"):
            page = int(text.split("__page_")[1])
            prods = sess.get("category_products", [])
            _retail_sessions[user_id] = {**sess, "page": page}
            retail_reply(reply_token, _line_build_carousel(prods, pricelist, page))
            continue

        # ── Postback: add to cart (fixed qty) ─────────────────────────────
        if text.startswith("__aq_"):
            parts = text[5:].split("_")
            if len(parts) >= 4:
                pid, sku, price_int, qty = int(parts[0]), parts[1], int(parts[2]), int(parts[3])
                price = _line_get_client_price(pid, float(price_int), pricelist)
                prods = sess.get("category_products", [])
                prod = next((p for p in prods if p.get("id") == pid), None)
                name = prod["name"] if prod else sku
                short = name[:25]
                existing = next((i for i in cart if i["pid"] == pid), None)
                if existing:
                    existing["qty"] += qty
                else:
                    cart.append({"pid": pid, "sku": sku, "name": name, "qty": qty, "price": price})
                total = sum(i["qty"] * i["price"] for i in cart)
                _retail_sessions[user_id] = {**sess, "cart": cart}
                cur_page = sess.get("page", 0)
                confirm_msg = line_text(f"✅ {short} ×{qty} เพิ่มแล้ว\nตะกร้า: {len(cart)} รายการ — {total:,.0f}฿")
                msgs = [confirm_msg] + _line_build_carousel(prods, pricelist, cur_page)
                retail_reply(reply_token, msgs[:5])
            continue

        # ── Postback: custom qty prompt ────────────────────────────────────
        if text.startswith("__cq_"):
            parts = text[5:].split("_")
            if len(parts) >= 3:
                _retail_sessions[user_id] = {**sess, "state": "awaiting_qty",
                                              "pending_pid": int(parts[0]),
                                              "pending_sku": parts[1],
                                              "pending_price": int(parts[2])}
                retail_reply(reply_token, [line_text("กรอกจำนวนที่ต้องการ:\nEnter quantity:")])
            continue

        # ── Awaiting custom qty ────────────────────────────────────────────
        if sess.get("state") == "awaiting_qty" and text.isdigit():
            qty = int(text)
            pid = sess.get("pending_pid")
            sku = sess.get("pending_sku", "")
            price = _line_get_client_price(pid, float(sess.get("pending_price", 0)), pricelist)
            prods = sess.get("category_products", [])
            prod = next((p for p in prods if p.get("id") == pid), None)
            name = prod["name"] if prod else sku
            existing = next((i for i in cart if i["pid"] == pid), None)
            if existing:
                existing["qty"] += qty
            else:
                cart.append({"pid": pid, "sku": sku, "name": name, "qty": qty, "price": price})
            total = sum(i["qty"] * i["price"] for i in cart)
            _retail_sessions[user_id] = {**{k: v for k, v in sess.items()
                                            if k not in ("state", "pending_pid", "pending_sku", "pending_price")},
                                          "cart": cart}
            retail_reply(reply_token, [line_text(
                f"✅ {name[:25]} ×{qty} เพิ่มแล้ว\nตะกร้า: {len(cart)} รายการ — {total:,.0f}฿"
            )])
            continue

        # ── Menu / catalogue ───────────────────────────────────────────────
        if text_low in ("menu", "เมนู", "สินค้า", "catalog", "ดูสินค้า"):
            cats = _retail_get_categories()
            if cats:
                retail_reply(reply_token, [_retail_build_cat_flex(cats)])
            else:
                retail_reply(reply_token, [line_text("ไม่พบหมวดหมู่สินค้า\nNo categories found.")])

        # ── Cart ───────────────────────────────────────────────────────────
        elif text_low in ("cart", "ตะกร้า", "ตะกร้าสินค้า"):
            if not cart:
                retail_reply(reply_token, [line_text("ตะกร้าสินค้าว่างเปล่า\nYour cart is empty.")])
            else:
                retail_reply(reply_token, _line_cart_messages(cart))

        # ── Checkout: choose payment method ────────────────────────────────
        elif text_low in ("checkout", "ชำระเงิน", "สั่งซื้อ", "order"):
            if not cart:
                retail_reply(reply_token, [line_text("ตะกร้าสินค้าว่างเปล่า\nYour cart is empty.")])
            else:
                total = sum(i["qty"] * i["price"] for i in cart)
                retail_reply(reply_token, [line_quick_reply(
                    f"ยอดรวม {total:,.0f}฿ — เลือกวิธีชำระเงิน\nTotal {total:,.0f}฿ — Choose payment:",
                    [("📱 PromptPay", "__pay_promptpay"),
                     ("💳 บัตรเครดิต", "__pay_card")]
                )])

        # ── Pay via PromptPay (Stripe) ─────────────────────────────────────
        elif text == "__pay_promptpay":
            if not cart:
                retail_reply(reply_token, [line_text("ตะกร้าสินค้าว่างเปล่า\nYour cart is empty.")])
            elif not STRIPE_SECRET_KEY:
                retail_reply(reply_token, [line_text("❌ Stripe not configured.")])
            else:
                total = sum(i["qty"] * i["price"] for i in cart)
                try:
                    intent = _stripe.PaymentIntent.create(
                        amount=int(total * 100),
                        currency="thb",
                        payment_method_types=["promptpay"],
                        metadata={"line_user_id": user_id, "partner_id": str(partner["id"]),
                                  "cart": json.dumps([{"pid": i["pid"], "name": i["name"],
                                      "qty": i["qty"], "price": i["price"]} for i in cart])},
                    )
                    # Confirm with promptpay payment method to generate QR code
                    partner_email = partner.get("email") or f"{user_id}@line.mistercochon.com"
                    intent = _stripe.PaymentIntent.confirm(
                        intent["id"],
                        payment_method_data={"type": "promptpay",
                                             "billing_details": {"email": partner_email}},
                        return_url=f"{RENDER_URL}/payment-success?user={user_id}",
                    )
                    qr_url = intent["next_action"]["promptpay_display_qr_code"]["image_url_png"]
                    _retail_sessions[user_id] = {**sess, "stripe_cart": cart,
                        "stripe_intent_id": intent["id"], "cart": []}
                    retail_reply(reply_token, [
                        line_text(f"📱 สแกน QR PromptPay เพื่อชำระ {total:,.0f}฿\n"
                                  f"Scan to pay {total:,.0f}฿ via PromptPay"),
                        {"type": "image", "originalContentUrl": qr_url, "previewImageUrl": qr_url}
                    ])
                except Exception as e:
                    retail_reply(reply_token, [line_text(f"❌ Error: {str(e)[:100]}")])

        # ── Pay via Stripe Card ────────────────────────────────────────────
        elif text == "__pay_card":
            if not cart:
                retail_reply(reply_token, [line_text("ตะกร้าสินค้าว่างเปล่า\nYour cart is empty.")])
            elif not STRIPE_SECRET_KEY:
                retail_reply(reply_token, [line_text("❌ Stripe not configured.")])
            else:
                total = sum(i["qty"] * i["price"] for i in cart)
                line_items = []
                for item in cart:
                    price = _line_get_client_price(item["pid"], float(item["price"]), pricelist)
                    line_items.append({
                        "price_data": {"currency": "thb",
                            "unit_amount": int(price * 100),
                            "product_data": {"name": item["name"][:80]}},
                        "quantity": item["qty"],
                    })
                try:
                    session = _stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=line_items,
                        mode="payment",
                        success_url=f"{RENDER_URL}/payment-success?user={user_id}",
                        cancel_url=f"{RENDER_URL}/payment-cancel?user={user_id}",
                        metadata={"line_user_id": user_id, "partner_id": str(partner["id"])},
                    )
                    _retail_sessions[user_id] = {**sess, "stripe_cart": cart,
                        "stripe_session_id": session.id}
                    pay_bubble = {
                        "type": "bubble", "size": "mega",
                        "header": {
                            "type": "box", "layout": "vertical",
                            "backgroundColor": "#1A3A6B", "paddingAll": "14px",
                            "contents": [{"type": "text", "text": "💳 Paiement par carte",
                                          "weight": "bold", "color": "#FFFFFF", "size": "md"}]
                        },
                        "body": {
                            "type": "box", "layout": "vertical", "paddingAll": "14px",
                            "contents": [
                                {"type": "text", "text": f"Total : {total:,.0f} ฿",
                                 "weight": "bold", "size": "xl", "color": "#C8102E"},
                                {"type": "text", "text": "Appuyez sur le bouton pour payer en sécurité via Stripe.",
                                 "size": "sm", "color": "#555555", "wrap": True, "margin": "md"},
                            ]
                        },
                        "footer": {
                            "type": "box", "layout": "vertical", "paddingAll": "10px",
                            "contents": [
                                {"type": "button", "style": "primary", "color": "#1A3A6B",
                                 "action": {"type": "uri", "label": f"Payer {total:,.0f} ฿ par carte",
                                            "uri": session.url}}
                            ]
                        }
                    }
                    retail_reply(reply_token, [{"type": "flex", "altText": f"Paiement carte {total:,.0f}฿", "contents": pay_bubble}])
                except Exception as e:
                    retail_reply(reply_token, [line_text(f"❌ Stripe error: {str(e)[:100]}")])
        # ── Clear cart ─────────────────────────────────────────────────────
        elif text_low in ("cancel", "ยกเลิก", "clear", "ล้างตะกร้า"):
            _retail_sessions[user_id] = {**sess, "cart": []}
            retail_reply(reply_token, [line_text("🗑️ ล้างตะกร้าแล้ว\nCart cleared.")])

        # ── Help ───────────────────────────────────────────────────────────
        elif text_low in ("help", "ช่วยเหลือ", "?"):
            retail_reply(reply_token, [line_text(
                "🐷 *Mister Cochon — คำสั่งที่ใช้ได้*\n\n"
                "📋 *เมนู* — ดูสินค้าทั้งหมด\n"
                "🛒 *ตะกร้า* — ดูตะกร้าสินค้า\n"
                "💳 *ชำระเงิน* — สั่งซื้อและชำระเงิน\n"
                "🗑️ *ยกเลิก* — ล้างตะกร้า\n\n"
                "📞 ติดต่อ: @jfbuc"
            )])

        # ── Default ────────────────────────────────────────────────────────
        else:
            retail_reply(reply_token, [line_quick_reply(
                "ต้องการทำอะไร? / What would you like to do?",
                [("📋 สินค้า", "เมนู"), ("🛒 ตะกร้า", "ตะกร้า"),
                 ("💳 ชำระเงิน", "ชำระเงิน"), ("❓ ช่วยเหลือ", "help")]
            )])

    return {"status": "ok"}




from fastapi.responses import HTMLResponse

@app.get("/payment-success")
async def payment_success():
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;text-align:center;padding:40px">
    <h1 style="color:#2e7d32">✅ ชำระเงินสำเร็จ! Payment successful!</h1>
    <p>กลับไปที่ LINE เพื่อดูยืนยันคำสั่งซื้อ<br>Return to LINE to see your order confirmation.</p>
    </body></html>""")

@app.get("/payment-cancel")
async def payment_cancel():
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;text-align:center;padding:40px">
    <h1 style="color:#c62828">❌ ยกเลิกการชำระเงิน / Payment cancelled</h1>
    <p>กลับไปที่ LINE / Return to LINE.</p>
    </body></html>""")


# ─── PromptPay Stripe — Page de paiement Ecwid ───────────────────────────────

@app.get("/pay/{order_number}")
async def pay_ecwid_promptpay(order_number: str):
    """Page de paiement PromptPay Stripe pour commande Ecwid."""
    if not STRIPE_SECRET_KEY:
        return HTMLResponse("<h2 style='font-family:sans-serif;padding:40px;color:red'>Stripe non configuré</h2>", status_code=500)

    # Récupérer la commande Ecwid (ID numérique ou orderNumber)
    eco = None
    if str(order_number).isdigit():
        eco = ecwid_get(f"/orders/{order_number}")
    if not eco:
        data = ecwid_get("/orders", {"orderNumber": order_number, "limit": 1})
        if data and data.get("items"):
            eco = data["items"][0]
    if not eco:
        return HTMLResponse("<h1 style='font-family:sans-serif;padding:40px'>Commande introuvable</h1>", status_code=404)
    total = float(eco.get("total", 0))
    amount = int(total * 100)
    customer_name = (eco.get("billingPerson") or {}).get("name", "")
    payment_status = eco.get("paymentStatus", "")

    if payment_status == "PAID":
        return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset=UTF-8>
        <meta name=viewport content="width=device-width,initial-scale=1">
        <title>Déjà payée</title></head>
        <body style="font-family:sans-serif;text-align:center;padding:60px;background:#f4f4f4">
        <div style="background:#fff;border-radius:12px;padding:32px;max-width:400px;margin:0 auto">
        <div style="font-size:60px">✅</div>
        <h2 style="color:#1a7a40;margin:16px 0">Commande #{order_number} payée</h2>
        <p>Merci pour votre paiement !</p>
        </div></body></html>""")

    # Créer PaymentIntent Stripe PromptPay
    try:
        intent = _stripe.PaymentIntent.create(
            amount=amount,
            currency="thb",
            payment_method_types=["promptpay"],
            metadata={"ecwid_order_id": order_number},
            description=f"Commande Ecwid #{order_number}",
        )
        confirmed = _stripe.PaymentIntent.confirm(
            intent.id,
            payment_method_data={"type": "promptpay",
                                 "billing_details": {"email": f"order{order_number}@mistercochon.com"}},
            return_url=f"{RENDER_URL}/payment-success?order={order_number}",
        )
        qr_data = (confirmed.next_action or {}).get("promptpay_display_qr_code", {})
        qr_image = qr_data.get("image_url_png", "")
        amount_display = f"{total:,.0f}"
    except Exception as e:
        return HTMLResponse(f"<h2 style='font-family:sans-serif;padding:40px;color:red'>Erreur: {e}</h2>", status_code=500)

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="fr"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Payer commande #{order_number}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f4f4f4;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}}
.card{{background:#fff;border-radius:16px;padding:28px 24px;max-width:420px;width:100%;box-shadow:0 4px 20px rgba(0,0,0,.1);text-align:center}}
.logo{{color:#c8102e;font-size:20px;font-weight:800;margin-bottom:4px}}
.order{{color:#888;font-size:13px;margin-bottom:20px}}
.amount{{font-size:32px;font-weight:800;color:#222;margin-bottom:6px}}
.currency{{font-size:16px;color:#888;margin-bottom:20px}}
.qr-wrap{{background:#f8f8f8;border-radius:12px;padding:16px;margin-bottom:20px;display:inline-block}}
.qr-wrap img{{width:220px;height:220px;display:block}}
.steps{{text-align:left;background:#fff8f0;border-radius:10px;padding:14px 16px;margin-bottom:20px}}
.steps h3{{font-size:13px;font-weight:700;color:#e65c00;margin-bottom:8px}}
.steps ol{{padding-left:18px;font-size:13px;color:#555;line-height:1.8}}
.waiting{{color:#888;font-size:13px;display:flex;align-items:center;justify-content:center;gap:8px}}
.dot{{width:8px;height:8px;background:#c8102e;border-radius:50%;animation:pulse 1.2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">🐷 Mister Cochon</div>
  <div class="order">Commande #{order_number}{f" — {customer_name}" if customer_name else ""}</div>
  <div class="amount">฿{amount_display}</div>
  <div class="currency">Montant total TTC</div>
  <div class="qr-wrap">
    <img src="{qr_image}" alt="QR PromptPay"/>
  </div>
  <div class="steps">
    <h3>Comment payer / How to pay:</h3>
    <ol>
      <li>Ouvrez votre app bancaire / Open your banking app</li>
      <li>Scannez le QR / Scan the QR code</li>
      <li>Vérifiez ฿{amount_display} et confirmez / Verify and confirm</li>
      <li>Cette page se met à jour automatiquement</li>
    </ol>
  </div>
  <div class="waiting"><div class="dot"></div> En attente de paiement / Waiting for payment...</div>
</div>
<script>
setInterval(async function(){{
  try{{
    var r = await fetch('/check-payment/{order_number}');
    var d = await r.json();
    if(d.paid){{
      document.body.innerHTML = '<div style="text-align:center;padding:60px;font-family:sans-serif"><div style="font-size:80px">✅</div><h2 style="color:#1a7a40;margin:20px 0">Paiement reçu ! / Payment received!</h2><p>Merci, commande #{order_number} confirmée.</p></div>';
    }}
  }}catch(e){{}}
}}, 5000);
</script>
</body></html>""")


@app.get("/check-payment/{order_number}")
async def check_ecwid_payment(order_number: str):
    """Vérifie si une commande Ecwid est marquée PAID."""
    data = ecwid_get("/orders", {"orderNumber": order_number, "limit": 1})
    if not data or not data.get("items"):
        return {"paid": False}
    return {"paid": data["items"][0].get("paymentStatus") == "PAID"}


@app.get("/qr")
async def create_promptpay_qr(amount: float = 0):
    """Crée un PaymentIntent Stripe PromptPay et retourne l'URL du QR."""
    from fastapi.responses import JSONResponse
    if not STRIPE_SECRET_KEY:
        return JSONResponse({"error": "Stripe non configuré"}, status_code=500)
    if amount <= 0:
        return JSONResponse({"error": "Montant invalide"}, status_code=400)
    try:
        int_amount = int(round(amount * 100))
        intent = _stripe.PaymentIntent.create(
            amount=int_amount,
            currency="thb",
            payment_method_types=["promptpay"],
            metadata={"source": "ecwid_checkout"},
        )
        confirmed = _stripe.PaymentIntent.confirm(
            intent.id,
            payment_method_data={"type": "promptpay", "billing_details": {"email": "customer@mistercochon.com"}},
            return_url=f"{RENDER_URL}/payment-success",
        )
        qr_data = (confirmed.next_action or {}).get("promptpay_display_qr_code", {})
        qr_url = qr_data.get("image_url_png", "")
        intent_id = confirmed.id
        return JSONResponse({
            "qr_url": qr_url,
            "intent_id": intent_id,
            "amount": amount,
        }, headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500, headers={"Access-Control-Allow-Origin": "*"})


@app.get("/check-intent/{intent_id}")
async def check_stripe_intent(intent_id: str):
    """Vérifie si un PaymentIntent Stripe est payé."""
    from fastapi.responses import JSONResponse
    if not STRIPE_SECRET_KEY:
        return JSONResponse({"paid": False}, headers={"Access-Control-Allow-Origin": "*"})
    try:
        intent = _stripe.PaymentIntent.retrieve(intent_id)
        return JSONResponse(
            {"paid": intent.status == "succeeded"},
            headers={"Access-Control-Allow-Origin": "*"}
        )
    except Exception as e:
        return JSONResponse({"paid": False, "error": str(e)}, headers={"Access-Control-Allow-Origin": "*"})

render@srv-d7u1m3l0lvsc73ekgrpg-59bd7f46f5-pcskt:~/project/src$ 
