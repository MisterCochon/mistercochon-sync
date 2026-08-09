import os
import csv
import io
import re
import json
import asyncio
import requests
import xmlrpc.client
import stripe as _stripe
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Request
from pydantic import BaseModel
from typing import Dict
from fastapi.responses import StreamingResponse
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

POLL_INTERVAL = 120  # secondes entre chaque vérification Ecwid → Odoo

def _get_ecwid_subdistrict(eco: dict) -> str:
    """Extrait le sous-district (champ personnalise Ecwid 'Sub-District') d'une commande."""
    for f in eco.get("orderExtraFields") or []:
        if str(f.get("title", "")).strip().lower() == "sub-district":
            return str(f.get("value") or "").strip()
    return ""

async def _poll_ecwid_orders():
    """Tâche de fond : vérifie les nouvelles commandes Ecwid toutes les 2 min."""
    await asyncio.sleep(30)
    while True:
        try:
            from datetime import datetime as _dt
            ecwid_base = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}"
            headers    = {"Authorization": f"Bearer {ECWID_TOKEN}"}

            # Toutes les refs existantes (ECWID-, FD sans préfixe, etc.)
            existing = odoo_execute("sale.order", "search_read",
                [[["client_order_ref", "!=", False]]],
                {"fields": ["client_order_ref"], "limit": 10000})
            existing_refs = set(o["client_order_ref"] for o in existing if o["client_order_ref"])

            # Produits par SKU
            products = odoo_execute("product.product", "search_read",
                [[["default_code", "!=", False]]],
                {"fields": ["id", "default_code", "taxes_id"], "limit": 5000})
            sku_map = {p["default_code"].strip(): p for p in products}

            # Contacts par email (rechargé à chaque cycle)
            partners = odoo_execute("res.partner", "search_read",
                [[["email", "!=", False]]],
                {"fields": ["id", "email", "name"], "limit": 10000})
            email_map = {p["email"].strip().lower(): p["id"] for p in partners}
            name_map  = {p["name"].strip().lower(): p["id"] for p in partners if p["name"]}

            default_partner = odoo_execute("res.partner", "search", [[["name", "=", "Mister Cochon"]]])
            default_partner_id = default_partner[0] if default_partner else 1

            # 200 dernières commandes Ecwid
            r = requests.get(f"{ecwid_base}/orders", headers=headers,
                params={"limit": 200, "sortBy": "CREATED_DATE_DESC"})
            orders = r.json().get("items", []) if r.ok else []

            for eco in orders:
                # Ignorer les commandes Ecwid à 0 (paniers vides, tests, abandons)
                eco_total = float(eco.get("total") or eco.get("grandTotal") or 0)
                if eco_total <= 0:
                    continue

                # Blocage permanent : les commandes avant juillet 2026 sont deja
                # suivies sur un autre systeme, on ne les importe plus dans Odoo.
                eco_date = _parse_ecwid_date(eco.get("createDate") or eco.get("updateDate"))
                if eco_date and eco_date < "2026-07-01":
                    continue

                ecwid_id  = str(eco.get("id") or eco.get("orderNumber", ""))
                order_num = str(eco.get("orderNumber") or eco.get("id", ""))
                ref      = f"ECWID-{ecwid_id}"
                ref_alt  = f"ECWID-{order_num}"
                # Détecte toutes les formes : ECWID-xxx, ou le raw id (FD orders)
                if ref in existing_refs or ref_alt in existing_refs \
                   or ecwid_id in existing_refs or order_num in existing_refs:
                    continue

                billing = eco.get("billingPerson") or eco.get("shippingPerson") or {}
                customer_name  = (billing.get("name") or eco.get("email") or "Client Ecwid").strip()
                customer_phone = (billing.get("phone") or "").strip()
                customer_street= (billing.get("street") or "").strip()
                customer_city  = (billing.get("city") or "").strip()
                customer_zip   = (billing.get("postalCode") or "").strip()
                customer_subdistrict = _get_ecwid_subdistrict(eco)
                email = (eco.get("email") or "").strip().lower()

                # Chercher par email d'abord, puis par nom
                partner_id = None
                if email and email in email_map:
                    partner_id = email_map[email]
                if not partner_id and customer_name and customer_name != "Client Ecwid":
                    partner_id = name_map.get(customer_name.strip().lower())
                    if not partner_id:
                        found = odoo_execute("res.partner", "search_read",
                            [[["name", "=", customer_name]]],
                            {"fields": ["id"], "limit": 1})
                        if found:
                            partner_id = found[0]["id"]
                if not partner_id:
                    if not customer_name or customer_name == "Client Ecwid":
                        customer_name = email or "Client Ecwid"
                    vals = {"name": customer_name, "customer_rank": 1}
                    if email:          vals["email"]  = email
                    if customer_phone: vals["phone"]  = customer_phone
                    if customer_street:vals["street"] = customer_street
                    if customer_city:  vals["city"]   = customer_city
                    if customer_zip:   vals["zip"]    = customer_zip
                    if customer_subdistrict: vals["x_sub_district"] = customer_subdistrict
                    partner_id = odoo_execute("res.partner", "create", [vals])
                    if email:
                        email_map[email] = partner_id
                else:
                    # Mettre à jour l'adresse/téléphone si manquants
                    existing_partner = odoo_execute("res.partner", "read",
                        [[partner_id]], {"fields": ["phone","street","city","zip","x_sub_district"]})[0]
                    update_vals = {}
                    if customer_phone and not existing_partner.get("phone"):
                        update_vals["phone"] = customer_phone
                    if customer_street and not existing_partner.get("street"):
                        update_vals["street"] = customer_street
                    if customer_city and not existing_partner.get("city"):
                        update_vals["city"] = customer_city
                    if customer_zip and not existing_partner.get("zip"):
                        update_vals["zip"] = customer_zip
                    if customer_subdistrict and not existing_partner.get("x_sub_district"):
                        update_vals["x_sub_district"] = customer_subdistrict
                    if update_vals:
                        odoo_execute("res.partner", "write", [[partner_id], update_vals])

                try:
                    ts = eco.get("createDate", "")
                    date_order = _dt.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    date_order = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")

                import sys as _sys, os as _os
                _sys.path.insert(0, _os.path.dirname(_os.path.dirname(__file__)))
                try:
                    from sku_mapping import SKU_MAP as _SKU_MAP
                except Exception:
                    _SKU_MAP = {}

                lines = []
                for item in eco.get("items", []):
                    sku   = (item.get("sku") or "").strip()
                    qty   = item.get("quantity", 1)
                    price = item.get("price", 0)
                    prod  = sku_map.get(sku)
                    if not prod and sku and sku in _SKU_MAP and _SKU_MAP[sku]:
                        prod = sku_map.get(_SKU_MAP[sku])
                    if prod:
                        line = {"product_id": prod["id"], "product_uom_qty": qty, "price_unit": price}
                        if prod.get("taxes_id"):
                            line["tax_ids"] = [(6, 0, prod["taxes_id"])]
                        lines.append((0, 0, line))
                    else:
                        name = item.get("name", sku or "Produit inconnu")
                        lines.append((0, 0, {
                            "name": f"[{sku}] {name}" if sku else name,
                            "product_uom_qty": qty, "price_unit": price, "product_id": False
                        }))

                if not lines:
                    continue

                # Remise
                disc = float(eco.get("discount") or 0) + float(eco.get("couponDiscount") or 0)
                if disc > 0:
                    lines.append((0, 0, {"product_id": 2712, "name": "Remise",
                        "product_uom_qty": 1, "price_unit": -round(disc, 2), "tax_ids": [(5,0,0)]}))
                # Transport
                ship_opt  = eco.get("shippingOption") or {}
                ship_cost = float(ship_opt.get("discountedShippingRate") or ship_opt.get("shippingRate") or 0)
                if ship_cost > 0:
                    lines.append((0, 0, {"product_id": 1915,
                        "name": ship_opt.get("shippingMethodName") or "Transport",
                        "product_uom_qty": 1, "price_unit": round(ship_cost, 2), "tax_ids": [(5,0,0)]}))
                # Supplements (ex: "Delivery Frozen or Chill")
                for surcharge in (eco.get("customSurcharges") or []):
                    amount = surcharge.get("total") or surcharge.get("value") or 0
                    if amount:
                        lines.append((0, 0, {"product_id": 1917,
                            "name": surcharge.get("description") or "Supplement",
                            "product_uom_qty": 1, "price_unit": amount}))

                eco_note = (eco.get("customerComment") or "").strip()
                so_vals = {
                    "partner_id": partner_id,
                    "client_order_ref": ref,
                    "date_order": date_order,
                    "order_line": lines,
                }
                if eco_note:
                    so_vals["note"] = eco_note
                new_id = odoo_execute("sale.order", "create", [so_vals])
                odoo_execute("sale.order", "action_confirm", [[new_id]])
                # Ajouter immédiatement à existing_refs pour éviter les doublons
                # si deux instances tournent simultanément (ex: redeployment Render)
                existing_refs.add(ref)
                existing_refs.add(ref_alt)
                existing_refs.add(ecwid_id)
                existing_refs.add(order_num)
                print(f"[POLL] ✓ Nouvelle commande importée : {ref}")

        except Exception as e:
            print(f"[POLL] Erreur: {e}")
        await asyncio.sleep(POLL_INTERVAL)

STOCK_SYNC_INTERVAL = 600  # secondes entre chaque synchro stock Odoo -> Ecwid

async def _poll_stock_sync():
    """Tâche de fond : pousse le stock Odoo vers Ecwid toutes les 10 min."""
    await asyncio.sleep(60)
    while True:
        try:
            ecwid_base = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}"
            headers    = {"Authorization": f"Bearer {ECWID_TOKEN}"}

            # Stock disponible Odoo par produit (emplacements internes uniquement)
            quants = odoo_execute("stock.quant", "search_read",
                [[["location_id.usage", "=", "internal"]]],
                {"fields": ["product_id", "quantity", "reserved_quantity"], "limit": 10000})
            stock_by_pid = {}
            for q in quants:
                pid = q["product_id"][0]
                available = q["quantity"] - q["reserved_quantity"]
                stock_by_pid[pid] = stock_by_pid.get(pid, 0) + available

            if not stock_by_pid:
                await asyncio.sleep(STOCK_SYNC_INTERVAL)
                continue

            prods = odoo_execute("product.product", "read",
                [list(stock_by_pid.keys())], {"fields": ["id", "default_code", "lst_price"]})
            odoo_data = {}
            for p in prods:
                sku = (p.get("default_code") or "").strip()
                if sku:
                    odoo_data[sku.upper()] = {
                        "qty": max(0, int(stock_by_pid.get(p["id"], 0))),
                        "price": round(float(p.get("lst_price") or 0), 2),
                    }

            # Produits Ecwid (simples + variantes), avec quantite/prix actuels
            ecwid_targets = {}
            offset = 0
            while True:
                r = requests.get(f"{ecwid_base}/products", headers=headers,
                    params={"limit": 100, "offset": offset})
                data = r.json()
                items = data.get("items", [])
                if not items:
                    break
                for prod in items:
                    if prod.get("sku"):
                        ecwid_targets[prod["sku"].strip().upper()] = {
                            "url": f"{ecwid_base}/products/{prod['id']}",
                            "qty": prod.get("quantity", 0),
                            "price": round(float(prod.get("price") or 0), 2),
                        }
                    for combo in (prod.get("combinations") or []):
                        if combo.get("sku"):
                            ecwid_targets[combo["sku"].strip().upper()] = {
                                "url": f"{ecwid_base}/products/{prod['id']}/combinations/{combo['id']}",
                                "qty": combo.get("quantity", 0),
                                "price": round(float(combo.get("price") or prod.get("price") or 0), 2),
                            }
                offset += 100
                if offset >= data.get("total", 0):
                    break

            updated_qty, updated_price = 0, 0
            for sku, vals in odoo_data.items():
                target = ecwid_targets.get(sku)
                if not target:
                    continue
                payload = {}
                if target["qty"] != vals["qty"]:
                    payload.update({"quantity": vals["qty"], "unlimited": False, "inStock": vals["qty"] > 0})
                if vals["price"] and abs(target["price"] - vals["price"]) > 0.01:
                    payload["price"] = vals["price"]
                if not payload:
                    continue
                try:
                    requests.put(target["url"], headers=headers, json=payload)
                    if "quantity" in payload: updated_qty += 1
                    if "price" in payload: updated_price += 1
                except Exception:
                    pass
            print(f"[STOCK] {updated_qty} stocks, {updated_price} prix mis a jour sur Ecwid")
        except Exception as e:
            print(f"[STOCK] Erreur: {e}")
        await asyncio.sleep(STOCK_SYNC_INTERVAL)

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_poll_ecwid_orders())
    task2 = asyncio.create_task(_poll_stock_sync())
    task3 = asyncio.create_task(_poll_ecwid_images())
    yield
    task.cancel()
    task2.cancel()
    task3.cancel()

app = FastAPI(lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

VERSION = "2026-06-23-v50-customer-by-name"

# Enregistrer la police Thai au démarrage
_THAI_FONT = "Helvetica"
for _fp in [
    os.path.join(os.path.dirname(__file__), "NotoSansThai-Regular.ttf"),
    "/opt/delicatessen/fonts/tahoma.ttf",
    "/opt/delicatessen/fonts/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/google-noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/thai-scalable/Garuda.ttf",
    r"C:\Windows\Fonts\THSarabunNew.ttf",
    r"C:\Windows\Fonts\Tahoma.ttf",
]:
    if os.path.exists(_fp):
        try:
            pdfmetrics.registerFont(TTFont("ThaiMC", _fp))
            _THAI_FONT = "ThaiMC"
        except Exception:
            pass
        break

_LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")


def draw_mixed_text(c, x, y, text, size, latin_font="Helvetica", unicode_font=_THAI_FONT):
    """Dessine du texte en alternant police latine et police Unicode (Thai)
    selon les caractères, pour éviter les glyphes manquants entre polices."""
    segments = []
    current, current_is_ascii = "", None
    for ch in text:
        is_ascii = ord(ch) < 128
        if current_is_ascii is None or is_ascii == current_is_ascii:
            current += ch
            current_is_ascii = is_ascii
        else:
            segments.append((current, current_is_ascii))
            current, current_is_ascii = ch, is_ascii
    if current:
        segments.append((current, current_is_ascii))

    cur_x = x
    for seg_text, is_ascii in segments:
        font = latin_font if is_ascii else unicode_font
        c.setFont(font, size)
        c.drawString(cur_x, y, seg_text)
        cur_x += c.stringWidth(seg_text, font, size)

try:
    import sys as _sys
    _sys.path.insert(0, r'C:\Users\LENOVO\OneDrive\Desktop\DELICATESSEN\ERP')
    import config as _cfg
    _CFG_URL = _cfg.ODOO_URL; _CFG_DB = _cfg.ODOO_DB
    _CFG_LOGIN = _cfg.ODOO_LOGIN; _CFG_PWD = _cfg.ODOO_PASSWORD
    _CFG_ECWID_ID = _cfg.ECWID_STORE_ID; _CFG_ECWID_TOKEN = _cfg.ECWID_TOKEN
except Exception:
    _CFG_URL = _CFG_DB = _CFG_LOGIN = _CFG_PWD = _CFG_ECWID_ID = _CFG_ECWID_TOKEN = None

ODOO_URL = os.getenv("ODOO_URL") or _CFG_URL
ODOO_DB = os.getenv("ODOO_DB") or _CFG_DB
ODOO_LOGIN = os.getenv("ODOO_LOGIN") or _CFG_LOGIN
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD") or _CFG_PWD

ECWID_STORE_ID = os.getenv("ECWID_STORE_ID") or _CFG_ECWID_ID
ECWID_TOKEN = os.getenv("ECWID_TOKEN") or _CFG_ECWID_TOKEN

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")


_odoo_cache = {"uid": None, "models": None}


def odoo_connect():
    if _odoo_cache["uid"]:
        return _odoo_cache["uid"], _odoo_cache["models"]
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        version = common.version()
        uid = common.authenticate(ODOO_DB, ODOO_LOGIN, ODOO_PASSWORD, {})
    except Exception as e:
        raise Exception(f"XML-RPC erreur: {e} — URL={ODOO_URL} DB={ODOO_DB} LOGIN={ODOO_LOGIN}")

    if not uid:
        raise Exception(f"Auth échouée (uid=False) — URL={ODOO_URL} DB={ODOO_DB} LOGIN={ODOO_LOGIN} version={version}")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    _odoo_cache["uid"] = uid
    _odoo_cache["models"] = models
    return uid, models


def odoo_execute(model, method, args=None, kwargs=None):
    uid, models = odoo_connect()
    try:
        return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args or [], kwargs or {})
    except Exception:
        _odoo_cache["uid"] = None
        uid, models = odoo_connect()
        return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args or [], kwargs or {})


def ecwid_get(endpoint, params=None, timeout=30):
    url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}{endpoint}"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {ECWID_TOKEN}"},
        params=params or {},
        timeout=timeout
    )

    if response.status_code != 200:
        return None

    return response.json()


def ecwid_put(endpoint, data):
    url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}{endpoint}"
    response = requests.put(
        url,
        headers={"Authorization": f"Bearer {ECWID_TOKEN}", "Content-Type": "application/json"},
        json=data
    )
    return response.status_code, response.json() if response.content else {}


def ecwid_get_all_products():
    all_products = []
    offset = 0
    limit = 100
    while True:
        data = ecwid_get("/products", {"offset": offset, "limit": limit})
        if not data:
            break
        items = data.get("items", [])
        all_products.extend(items)
        if len(items) < limit:
            break
        offset += limit
    return all_products


def get_odoo_stock_by_skus(skus):
    if not skus:
        return {}
    variants = odoo_execute(
        "product.product",
        "search_read",
        [[["default_code", "in", skus], ["active", "=", True]]],
        {"fields": ["default_code", "qty_available"]}
    )
    return {v["default_code"]: v["qty_available"] for v in variants if v.get("default_code")}


def variant_label(combination):
    values = []
    for option in combination.get("options", []):
        value = option.get("value")
        if value:
            values.append(value)
    return " | ".join(values)


def ecwid_variants(product):
    return [
        {
            "variant": variant_label(combination),
            "sku": combination.get("sku"),
            "price": combination.get("price"),
            "options": combination.get("options", [])
        }
        for combination in product.get("combinations", [])
    ]


def find_odoo_template_by_name(name):
    templates = odoo_execute(
        "product.template",
        "search_read",
        [[["name", "=", name]]],
        {
            "fields": ["id", "name", "product_variant_ids"],
            "limit": 1
        }
    )
    return templates[0] if templates else None


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "mistercochon-backend",
        "version": VERSION
    }


@app.get("/odoo-test")
def odoo_test():
    try:
        uid, _ = odoo_connect()
        return {"status": "ok", "uid": uid}
    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/ecwid-test")
def ecwid_test():
    try:
        profile = ecwid_get("/profile")
        return {"status": "ok", "profile": profile}
    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/check-sku")
def check_sku():
    try:
        products = odoo_execute(
            "product.product",
            "search_read",
            [[
                ["active", "=", True],
                ["sale_ok", "=", True],
                ["default_code", "=", False]
            ]],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "product_tmpl_id",
                    "product_variant_count",
                    "lst_price"
                ],
                "limit": 1000,
                "order": "name asc"
            }
        )

        return {
            "status": "ok",
            "missing_sku_count": len(products),
            "missing_sku": products
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/find-product/{sku}")
def find_product(sku: str):
    try:
        products = odoo_execute(
            "product.product",
            "search_read",
            [[["default_code", "=", sku]]],
            {
                "fields": ["id", "name", "default_code", "product_tmpl_id", "lst_price"],
                "limit": 20
            }
        )

        return {
            "status": "ok",
            "sku": sku,
            "count": len(products),
            "products": products
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/product-variants/{template_id}")
def product_variants(template_id: int):
    try:
        template = odoo_execute(
            "product.template",
            "read",
            [[template_id]],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "product_variant_ids",
                    "attribute_line_ids"
                ]
            }
        )

        if not template:
            return {"status": "not_found", "template_id": template_id}

        template = template[0]
        variant_ids = template["product_variant_ids"]

        variants = odoo_execute(
            "product.product",
            "read",
            [variant_ids],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "barcode",
                    "lst_price",
                    "product_tmpl_id",
                    "product_template_attribute_value_ids"
                ]
            }
        )

        all_value_ids = []
        for variant in variants:
            all_value_ids += variant.get("product_template_attribute_value_ids", [])

        all_value_ids = list(set(all_value_ids))

        values_map = {}

        if all_value_ids:
            values = odoo_execute(
                "product.template.attribute.value",
                "read",
                [all_value_ids],
                {
                    "fields": [
                        "id",
                        "display_name",
                        "name"
                    ]
                }
            )

            for value in values:
                values_map[value["id"]] = value.get("display_name") or value.get("name")

        for variant in variants:
            option_ids = variant.get("product_template_attribute_value_ids", [])
            variant["options"] = [
                values_map.get(option_id, str(option_id))
                for option_id in option_ids
            ]
            variant["option_label"] = " | ".join(variant["options"])

        return {
            "status": "ok",
            "template": template,
            "variant_count": len(variants),
            "variants": variants
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/ecwid-product/{product_id}")
def ecwid_product(product_id: int):
    try:
        product = ecwid_get(f"/products/{product_id}")

        if not product:
            return {
                "status": "not_found",
                "product_id": product_id
            }

        variants = ecwid_variants(product)

        return {
            "status": "ok",
            "id": product.get("id"),
            "name": product.get("name"),
            "base_sku": product.get("sku"),
            "variant_count": len(variants),
            "variants": variants
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/ecwid-raw/{product_id}")
def ecwid_raw(product_id: int):
    try:
        return {
            "status": "ok",
            "product": ecwid_get(f"/products/{product_id}")
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/ecwid-sku/{sku}")
def ecwid_sku(sku: str):
    try:
        data = ecwid_get("/products", {"keyword": sku})

        if not data:
            return {
                "status": "error",
                "message": "Aucune réponse Ecwid",
                "sku": sku
            }

        results = []

        for product in data.get("items", []):
            if product.get("sku") == sku:
                results.append({
                    "type": "parent",
                    "id": product.get("id"),
                    "name": product.get("name"),
                    "sku": sku
                })

            for combination in product.get("combinations", []):
                if combination.get("sku") == sku:
                    results.append({
                        "type": "variant",
                        "id": product.get("id"),
                        "name": product.get("name"),
                        "variant": variant_label(combination),
                        "sku": sku,
                        "options": combination.get("options", [])
                    })

        return {
            "status": "ok",
            "sku": sku,
            "count": len(results),
            "results": results
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/sync-product/{product_id}")
def sync_product(product_id: int):
    try:
        product = ecwid_get(f"/products/{product_id}")

        if not product:
            return {"status": "ecwid_not_found", "product_id": product_id}

        product_name = product.get("name")
        variants = ecwid_variants(product)
        template = find_odoo_template_by_name(product_name)

        if not template:
            return {
                "status": "odoo_template_not_found",
                "ecwid_id": product_id,
                "product": product_name,
                "ecwid_variant_count": len(variants),
                "variants": variants
            }

        return {
            "status": "ok",
            "product": product_name,
            "ecwid_id": product_id,
            "template_id": template["id"],
            "odoo_variant_count": len(template["product_variant_ids"]),
            "ecwid_variant_count": len(variants),
            "safe_to_apply": len(template["product_variant_ids"]) == len(variants),
            "variants": variants
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/apply-sync-product/{product_id}")
def apply_sync_product(product_id: int):
    try:
        product = ecwid_get(f"/products/{product_id}")

        if not product:
            return {"status": "ecwid_not_found", "product_id": product_id}

        product_name = product.get("name")
        variants = ecwid_variants(product)
        template = find_odoo_template_by_name(product_name)

        if not template:
            return {
                "status": "odoo_template_not_found",
                "product": product_name
            }

        odoo_variant_ids = template["product_variant_ids"]

        if len(odoo_variant_ids) != len(variants):
            return {
                "status": "blocked_variant_count_mismatch",
                "message": "Aucune modification faite : le nombre de variantes Odoo ne correspond pas au nombre de variantes Ecwid.",
                "product": product_name,
                "template_id": template["id"],
                "odoo_variant_count": len(odoo_variant_ids),
                "ecwid_variant_count": len(variants),
                "odoo_variant_ids": odoo_variant_ids,
                "ecwid_variants": variants
            }

        updated = []

        for odoo_variant_id, variant in zip(odoo_variant_ids, variants):
            sku = variant.get("sku")

            if not sku:
                continue

            odoo_execute(
                "product.product",
                "write",
                [[odoo_variant_id], {"default_code": sku}]
            )

            updated.append({
                "odoo_variant_id": odoo_variant_id,
                "variant": variant.get("variant"),
                "sku": sku
            })

        return {
            "status": "ok",
            "product": product_name,
            "template_id": template["id"],
            "updated_count": len(updated),
            "updated": updated
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/apply-sync-all")
def apply_sync_all(offset: int = 0, limit: int = 50):
    """Applique les SKUs Ecwid vers Odoo. Utiliser offset/limit pour paginer (ex: ?offset=0&limit=50)."""
    try:
        all_products = ecwid_get_all_products()
        total = len(all_products)
        products = all_products[offset:offset + limit]

        if not products:
            return {"status": "ok", "message": "Aucun produit dans cette plage", "total": total, "offset": offset, "limit": limit}

        names = list(set(p.get("name") for p in products if p.get("name")))
        templates = odoo_execute(
            "product.template", "search_read",
            [[["name", "in", names]]],
            {"fields": ["id", "name", "product_variant_ids"], "limit": 500}
        )
        template_map = {t["name"]: t for t in templates}

        results = {"updated": [], "skipped": [], "errors": []}

        for product in products:
            ecwid_id = product.get("id")
            product_name = product.get("name")
            combinations = product.get("combinations", [])
            template = template_map.get(product_name)

            if not template:
                results["skipped"].append({"id": ecwid_id, "name": product_name, "reason": "introuvable dans Odoo"})
                continue

            odoo_variant_ids = template["product_variant_ids"]

            if not combinations:
                sku = product.get("sku")
                if not sku:
                    results["skipped"].append({"id": ecwid_id, "name": product_name, "reason": "pas de SKU"})
                    continue
                if len(odoo_variant_ids) == 1:
                    odoo_execute("product.product", "write", [[odoo_variant_ids[0]], {"default_code": sku}])
                    results["updated"].append({"name": product_name, "sku": sku})
                else:
                    results["skipped"].append({"id": ecwid_id, "name": product_name, "reason": f"{len(odoo_variant_ids)} variantes Odoo"})
                continue

            variants = ecwid_variants(product)
            if not any(v.get("sku") for v in variants):
                results["skipped"].append({"id": ecwid_id, "name": product_name, "reason": "aucun SKU variante"})
                continue

            if len(odoo_variant_ids) != len(variants):
                results["errors"].append({"name": product_name, "odoo": len(odoo_variant_ids), "ecwid": len(variants)})
                continue

            for odoo_id, variant in zip(odoo_variant_ids, variants):
                sku = variant.get("sku")
                if sku:
                    odoo_execute("product.product", "write", [[odoo_id], {"default_code": sku}])

            results["updated"].append({"name": product_name, "variants": len(variants)})

        next_offset = offset + limit
        has_more = next_offset < total

        return {
            "status": "ok",
            "total_products": total,
            "offset": offset,
            "limit": limit,
            "processed": len(products),
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "updated_count": len(results["updated"]),
            "skipped_count": len(results["skipped"]),
            "error_count": len(results["errors"]),
            "updated": results["updated"],
            "errors": results["errors"],
            "skipped": results["skipped"]
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.post("/import-sku-csv")
async def import_sku_csv(file: UploadFile = File(...)):
    return await _process_sku_csv(await file.read())


@app.post("/import-sku-json")
async def import_sku_json(payload: dict):
    """Accepte le CSV encodé en base64 dans un JSON: {"csv": "contenu..."}"""
    import base64
    content = base64.b64decode(payload["csv"])
    return await _process_sku_csv(content)


async def _process_sku_csv(content: bytes):
    """Importe les SKUs depuis un export CSV Ecwid directement dans Odoo."""
    try:
        text = content.decode("utf-8")
        reader = csv.reader(io.StringIO(text))
        next(reader)  # skip header

        # Parser le CSV
        products = []
        current = None
        for row in reader:
            if not row or len(row) < 3:
                continue
            row_type = row[0].strip()
            if row_type == "product":
                current = {
                    "name": row[3].strip() if len(row) > 3 else "",
                    "sku": row[2].strip(),
                    "variations": []
                }
                products.append(current)
            elif row_type == "product_variation" and current:
                var_sku = row[5].strip() if len(row) > 5 else ""
                if var_sku:
                    current["variations"].append(var_sku)

        def is_numeric_sku(sku):
            return not sku or sku.replace(".", "").replace("E+", "").replace("+", "").isdigit()

        stats = {"updated": 0, "skipped": 0, "errors": [], "not_found": []}

        for product in products:
            name = product["name"]
            sku = product["sku"]
            variations = product["variations"]

            if is_numeric_sku(sku):
                stats["skipped"] += 1
                continue

            templates = odoo_execute("product.template", "search_read",
                [[["name", "=", name]]],
                {"fields": ["id", "name", "product_variant_ids"], "limit": 1}
            )

            if not templates:
                stats["not_found"].append(name)
                continue

            template = templates[0]
            odoo_variant_ids = template["product_variant_ids"]

            if not variations:
                odoo_execute("product.product", "write",
                    [[odoo_variant_ids[0]], {"default_code": sku}]
                ) if len(odoo_variant_ids) == 1 else odoo_execute(
                    "product.template", "write", [[template["id"]], {"default_code": sku}]
                )
                stats["updated"] += 1
                continue

            if len(odoo_variant_ids) != len(variations):
                stats["errors"].append({
                    "name": name,
                    "odoo_variants": len(odoo_variant_ids),
                    "csv_variants": len(variations)
                })
                continue

            for odoo_id, var_sku in zip(odoo_variant_ids, variations):
                if not is_numeric_sku(var_sku):
                    odoo_execute("product.product", "write",
                        [[odoo_id], {"default_code": var_sku}]
                    )
            stats["updated"] += 1

        return {
            "status": "ok",
            "total_products": len(products),
            "updated": stats["updated"],
            "skipped_no_sku": stats["skipped"],
            "not_found_in_odoo": len(stats["not_found"]),
            "variant_mismatch": len(stats["errors"]),
            "not_found": stats["not_found"][:20],
            "errors": stats["errors"][:20]
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/apply-bacon-sku")
def apply_bacon_sku():
    try:
        mapping = [
            {
                "odoo_variant_id": 464,
                "sku": "BOCF1262",
                "label": "Format: Smoked 250 Gr | TYPE: whole 250 gr"
            },
            {
                "odoo_variant_id": 468,
                "sku": "LARF1262",
                "label": "Format: Smoked lardons 250 gr | TYPE: Lardons"
            },
            {
                "odoo_variant_id": 472,
                "sku": "BOWF1232",
                "label": "Format: whole 1Kg | TYPE: entiere"
            }
        ]

        updated = []

        for item in mapping:
            odoo_execute(
                "product.product",
                "write",
                [
                    [item["odoo_variant_id"]],
                    {
                        "default_code": item["sku"]
                    }
                ]
            )

            updated.append({
                "variant_id": item["odoo_variant_id"],
                "label": item["label"],
                "sku": item["sku"]
            })

        return {
            "status": "ok",
            "product": "Bacon natural and smoked",
            "updated_count": len(updated),
            "updated": updated
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }
@app.get("/stock-status")
def stock_status():
    """Dry run : compare stock Odoo vs Ecwid pour tous les produits. Aucune modification."""
    try:
        products = ecwid_get_all_products()

        all_skus = []
        for product in products:
            if product.get("sku"):
                all_skus.append(product["sku"])
            for combo in product.get("combinations", []):
                if combo.get("sku"):
                    all_skus.append(combo["sku"])

        odoo_stock = get_odoo_stock_by_skus(list(set(all_skus)))

        results, no_sku, not_in_odoo = [], [], []

        for product in products:
            ecwid_id = product.get("id")
            name = product.get("name")
            combinations = product.get("combinations", [])

            if not combinations:
                sku = product.get("sku")
                if not sku:
                    no_sku.append({"id": ecwid_id, "name": name})
                    continue
                odoo_qty = odoo_stock.get(sku)
                if odoo_qty is None:
                    not_in_odoo.append({"id": ecwid_id, "name": name, "sku": sku})
                    continue
                results.append({
                    "id": ecwid_id, "name": name, "sku": sku,
                    "odoo_qty": odoo_qty, "ecwid_qty": product.get("quantity", 0),
                    "in_sync": odoo_qty == product.get("quantity", 0)
                })
            else:
                for combo in combinations:
                    csku = combo.get("sku")
                    if not csku:
                        no_sku.append({"id": ecwid_id, "name": name, "variant": variant_label(combo)})
                        continue
                    odoo_qty = odoo_stock.get(csku)
                    if odoo_qty is None:
                        not_in_odoo.append({"id": ecwid_id, "name": name, "sku": csku, "variant": variant_label(combo)})
                        continue
                    results.append({
                        "id": ecwid_id, "name": name, "variant": variant_label(combo), "sku": csku,
                        "odoo_qty": odoo_qty, "ecwid_qty": combo.get("quantity", 0),
                        "in_sync": odoo_qty == combo.get("quantity", 0)
                    })

        out_of_sync = [r for r in results if not r["in_sync"]]

        return {
            "status": "ok",
            "total_products": len(products),
            "total_checked": len(results),
            "out_of_sync_count": len(out_of_sync),
            "no_sku_count": len(no_sku),
            "not_in_odoo_count": len(not_in_odoo),
            "out_of_sync": out_of_sync,
            "no_sku": no_sku,
            "not_in_odoo": not_in_odoo
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/sync-stock")
def sync_stock():
    """Pousse les quantités Odoo vers Ecwid pour tous les produits."""
    try:
        products = ecwid_get_all_products()

        all_skus = []
        for product in products:
            if product.get("sku"):
                all_skus.append(product["sku"])
            for combo in product.get("combinations", []):
                if combo.get("sku"):
                    all_skus.append(combo["sku"])

        odoo_stock = get_odoo_stock_by_skus(list(set(all_skus)))

        updated, skipped, errors = [], [], []

        for product in products:
            ecwid_id = product.get("id")
            name = product.get("name")
            combinations = product.get("combinations", [])

            if not combinations:
                sku = product.get("sku")
                if not sku or sku not in odoo_stock:
                    skipped.append({"id": ecwid_id, "name": name, "sku": sku})
                    continue
                qty = int(max(0, odoo_stock[sku]))
                code, _ = ecwid_put(f"/products/{ecwid_id}", {"quantity": qty, "unlimited": False})
                if code == 200:
                    updated.append({"id": ecwid_id, "name": name, "sku": sku, "qty": qty})
                else:
                    errors.append({"id": ecwid_id, "name": name, "sku": sku, "http_status": code})
            else:
                for combo in combinations:
                    csku = combo.get("sku")
                    combo_id = combo.get("id")
                    if not csku or csku not in odoo_stock:
                        skipped.append({"id": ecwid_id, "name": name, "sku": csku, "variant": variant_label(combo)})
                        continue
                    qty = int(max(0, odoo_stock[csku]))
                    code, _ = ecwid_put(f"/products/{ecwid_id}/combinations/{combo_id}", {"quantity": qty})
                    if code == 200:
                        updated.append({"id": ecwid_id, "name": name, "variant": variant_label(combo), "sku": csku, "qty": qty})
                    else:
                        errors.append({"id": ecwid_id, "name": name, "sku": csku, "variant": variant_label(combo), "http_status": code})

        return {
            "status": "ok",
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "error_count": len(errors),
            "updated": updated,
            "skipped": skipped,
            "errors": errors
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


@app.get("/sync-stock/{ecwid_product_id}")
def sync_stock_one(ecwid_product_id: int):
    """Pousse le stock Odoo vers Ecwid pour un seul produit."""
    try:
        product = ecwid_get(f"/products/{ecwid_product_id}")
        if not product:
            return {"status": "ecwid_not_found", "product_id": ecwid_product_id}

        name = product.get("name")
        combinations = product.get("combinations", [])

        all_skus = [product["sku"]] if product.get("sku") else []
        for combo in combinations:
            if combo.get("sku"):
                all_skus.append(combo["sku"])

        odoo_stock = get_odoo_stock_by_skus(all_skus)

        updated, skipped, errors = [], [], []

        if not combinations:
            sku = product.get("sku")
            if not sku or sku not in odoo_stock:
                return {"status": "skipped", "reason": "no_sku_or_not_in_odoo", "sku": sku}
            qty = int(max(0, odoo_stock[sku]))
            code, _ = ecwid_put(f"/products/{ecwid_product_id}", {"quantity": qty, "unlimited": False})
            if code == 200:
                updated.append({"sku": sku, "qty": qty})
            else:
                errors.append({"sku": sku, "http_status": code})
        else:
            for combo in combinations:
                csku = combo.get("sku")
                combo_id = combo.get("id")
                if not csku or csku not in odoo_stock:
                    skipped.append({"sku": csku, "variant": variant_label(combo)})
                    continue
                qty = int(max(0, odoo_stock[csku]))
                code, _ = ecwid_put(f"/products/{ecwid_product_id}/combinations/{combo_id}", {"quantity": qty})
                if code == 200:
                    updated.append({"variant": variant_label(combo), "sku": csku, "qty": qty})
                else:
                    errors.append({"variant": variant_label(combo), "sku": csku, "http_status": code})

        return {
            "status": "ok",
            "product": name,
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "error_count": len(errors),
            "updated": updated,
            "skipped": skipped,
            "errors": errors
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "type": type(e).__name__}


def ecwid_delete(endpoint):
    url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}{endpoint}"
    response = requests.delete(url, headers={"Authorization": f"Bearer {ECWID_TOKEN}"})
    return response.status_code, response.json() if response.content else {}


# Odoo variant ID → nom du produit (via template)
TO_DELETE_ODOO_VARIANT_IDS = [
    27, 31, 48, 55, 85, 90, 129, 137, 148, 166, 171,
    190, 191, 212, 213, 219, 226, 229, 236, 250, 259,
    266, 271, 278, 283, 285, 297, 307, 308, 313, 315,
    318, 322, 327, 329, 343, 344
]

TO_ASSIGN_SKUS = {
    35: "BLPGR1200",
    54: "SHBM1120",
    67: "3295890237019",
    75: "CHIP11XX",
    83: "8856141004672",
    93: "CORDB12XX",
    117: "CHOX1200",
    118: "CHOR2200",
    119: "CHOR2300",
    124: "DRYX1200",
    135: "FOIE11XX",
    145: "MOUFVXXXX",
    156: "SAUC2200",
    157: "CHOF110X",
    174: "JAMB2160",
    179: "SMOKTXXX",
    181: "JAMP12XX",
    206: "COLC111X",
    220: "PANC1280",
    231: "PAVS1200",
    234: "BOCF1263",
    235: "PIEDS1200",
    238: "PINKP1200",
    252: "RILLEXXXX",
    260: "SALPR2200",
    264: "RABB1000",
    267: "RACLT1200",
    280: "SALAM1200",
    288: "SAUCL12XX",
    290: "SAUC12XXX",
    304: "SALM1200",
    309: "SAUST1200",
    319: "SAUT1130",
    324: "SAUVA1200",
    326: "COTV1110",
    331: "VEALN2100",
    341: "BBMA1200",
}


@app.get("/apply-skus-from-file")
def apply_skus_from_file():
    """Assigne les SKUs définis dans TO_ASSIGN_SKUS vers Odoo."""
    try:
        updated, errors = [], []
        for variant_id, sku in TO_ASSIGN_SKUS.items():
            try:
                odoo_execute("product.product", "write", [[variant_id], {"default_code": sku}])
                updated.append({"variant_id": variant_id, "sku": sku})
            except Exception as e:
                errors.append({"variant_id": variant_id, "sku": sku, "error": str(e)})
        return {"status": "ok", "updated_count": len(updated), "error_count": len(errors), "updated": updated, "errors": errors}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/delete-products-preview")
def delete_products_preview():
    """Aperçu des produits qui seront supprimés (sans modifier quoi que ce soit)."""
    try:
        variants = odoo_execute("product.product", "read",
            [TO_DELETE_ODOO_VARIANT_IDS],
            {"fields": ["id", "name", "product_tmpl_id"]}
        )
        ecwid_products = ecwid_get_all_products()
        ecwid_by_name = {p["name"]: p["id"] for p in ecwid_products if p.get("name")}

        preview = []
        for v in variants:
            name = v["name"]
            tmpl_id = v["product_tmpl_id"][0] if v.get("product_tmpl_id") else None
            ecwid_id = ecwid_by_name.get(name)
            preview.append({
                "odoo_variant_id": v["id"],
                "odoo_template_id": tmpl_id,
                "name": name,
                "ecwid_id": ecwid_id,
                "ecwid_found": ecwid_id is not None
            })

        return {
            "status": "ok",
            "count": len(preview),
            "ecwid_found_count": sum(1 for p in preview if p["ecwid_found"]),
            "preview": preview
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/delete-products")
def delete_products():
    """Supprime les produits marqués A SUPPRIMER : archive dans Odoo + supprime dans Ecwid."""
    try:
        variants = odoo_execute("product.product", "read",
            [TO_DELETE_ODOO_VARIANT_IDS],
            {"fields": ["id", "name", "product_tmpl_id"]}
        )

        # Récupérer les template IDs uniques
        template_ids = list(set(
            v["product_tmpl_id"][0] for v in variants if v.get("product_tmpl_id")
        ))

        # Archiver les templates Odoo (désactive aussi toutes leurs variantes)
        odoo_execute("product.template", "write", [template_ids, {"active": False}])

        # Supprimer dans Ecwid
        ecwid_products = ecwid_get_all_products()
        ecwid_by_name = {p["name"]: p["id"] for p in ecwid_products if p.get("name")}

        deleted_ecwid, not_found_ecwid, ecwid_errors = [], [], []
        processed_names = set()

        for v in variants:
            name = v["name"]
            if name in processed_names:
                continue
            processed_names.add(name)
            ecwid_id = ecwid_by_name.get(name)
            if not ecwid_id:
                not_found_ecwid.append(name)
                continue
            code, _ = ecwid_delete(f"/products/{ecwid_id}")
            if code in (200, 204):
                deleted_ecwid.append({"name": name, "ecwid_id": ecwid_id})
            else:
                ecwid_errors.append({"name": name, "ecwid_id": ecwid_id, "http_status": code})

        return {
            "status": "ok",
            "odoo_archived_templates": len(template_ids),
            "ecwid_deleted": len(deleted_ecwid),
            "ecwid_not_found": len(not_found_ecwid),
            "ecwid_errors": len(ecwid_errors),
            "deleted_ecwid": deleted_ecwid,
            "not_found_ecwid": not_found_ecwid,
            "errors_ecwid": ecwid_errors
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/assign-sku/{variant_id}/{sku}")
def assign_sku(variant_id: int, sku: str):
    """Assigne un SKU à une variante Odoo par son ID. Ex: /assign-sku/218/OCBC1420"""
    try:
        odoo_execute("product.product", "write", [[variant_id], {"default_code": sku}])
        return {"status": "ok", "variant_id": variant_id, "sku": sku}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/archive-template/{template_id}")
def archive_template(template_id: int):
    """Archive un template Odoo (et toutes ses variantes). Ex: /archive-template/314"""
    try:
        template = odoo_execute("product.template", "read", [[template_id]], {"fields": ["id", "name"]})
        if not template:
            return {"status": "not_found", "template_id": template_id}
        odoo_execute("product.template", "write", [[template_id], {"active": False}])
        return {"status": "ok", "archived": template[0]["name"], "template_id": template_id}
    except Exception as e:
        return {"status": "error", "error": str(e)}


PRO_STANDARD_PRICES = {
    "Merguez beef & Lamb (diam 22mm - 17cm)": 590,
    "Merguez beef & Lamb (18mm/20cm sandwiches)": 590,
    "Pork Merguez": 490,
    "Chipolata": 480,
    "Herb Chipolatas": 485,
    "Chipolata Tandoori Masala": 485,
    "Chipolata Kebab": 485,
    "Chipolata Ail des ours": 485,
    "Toulouse Sausage": 375,
    "Cooking Chorizo": 490,
    "Morteau Sausage": 510,
    "Montbeliard": 510,
    "Smoked sausages": 450,
    "Creole sausages": 350,
    "Cervelas pistachio": 790,
    "Cervelas pistachio & truffle": 1100,
    "Swiss Vaud sausages": 450,
    "Boudin Noir": 520,
    "Boudin noir vrac": 420,
    "Boudin creole": 590,
    "Boudin blanc Apple": 590,
    "Boudin blanc Mushrooms - Morels": 690,
    "Boudin blanc Truffle": 1100,
    "Breakfast sausages": 250,
    "Breakfast Chicken Sausage": 250,
    "Country Pate": 550,
    "Country Pate green pepper": 570,
    "Country Pate Basque": 490,
    "Duck liver pate": 650,
    "Pork Rillettes": 450,
    "Duck Rillettes": 580,
    "Duck Liver Mousse": 490,
    "Garlic Sausage": 490,
    "Parsley ham": 420,
    "Head cheese pate": 490,
    "Knack": 520,
    "Hotdog sausages": 520,
    "Chorizo": 790,
    "Bresaola sliced": 950,
    "Lonzo": 950,
    "Coppa": 890,
    "Pancetta": 890,
    "Rosette or Jesus": 890,
    "Saucisson Sec (diam 40/50mm)": 795,
    "Saucisson Walnut": 820,
    "Saucisson Provencal": 890,
    "Dry sausage": 720,
    "Dry liver sausages": 695,
    "Dry duck sausage": 820,
    "Cold cut": 120,
    "Pave chorizo": 590,
    "Pave saucisson": 590,
    "Finger saucisson": 890,
    "Finger Chorizo": 990,
    "Paris Ham (Block)": 450,
    "Paris Ham (Sliced)": 470,
    "Parisian heel (Talon)": 250,
    "Ham hock": 400,
    "Chicken Ham": 290,
    "Petit sale": 490,
    "Smoked Bacon": 520,
    "Smoked Bacon (Sliced)": 590,
    "Smoked Bacon (Diced)": 520,
    "Duck breast": 480,
    "Duck breast (magret)": 1750,
    "Smoked Duck Breast": 690,
    "Dry Duck Breast Pepper": 1100,
    "Duck Leg": 295,
    "Duck leg Confit": 495,
    "Gizzard Confit": 320,
    "Drummets": 320,
    "Duck Fat (oil)": 320,
    "Pork Chop": 450,
    "Seasoned minced pork": 350,
    "Harissa": 890,
    "Espelette Piment": 1500,
    "Toulouse sausage Premium": 490,
    "Saucisson premium": 1190,
    "Chorizo Premium": 1090,
    "Bacon smoked": 890,
    "Duck rillette (fat duck)": 950,
    "Duck legs fattened": 1270,
    "Duck Magret": 1790,
    "Duck magret dried": 1850,
    "Duck legs confit": 1590,
    "Andouillette 5A (180gr)": 1495,
    "Andouillette 5A (160gr Standard)": 1295,
    "Foie Gras Lobe Extra Deveined": 2950,
    "Foie gras Ballotine": 3900,
    "Smoked Trout from Himalaya sliced": 1050,
    "Smoked Trout from Himalaya Whole fillet": 890,
    "Smoked Salmon sliced": 1050,
    "Smoked Salmon whole fillet": 890,
}


@app.get("/search-products/{name}")
def search_products(name: str):
    """Cherche des produits par nom (recherche partielle). Ex: /search-products/Chipolata"""
    try:
        templates = odoo_execute("product.template", "search_read",
            [[["name", "ilike", name]]],
            {"fields": ["id", "name", "categ_id", "product_variant_count", "active"], "limit": 50}
        )
        return {"status": "ok", "count": len(templates), "products": templates}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/order-pdf/{order_ref}")
def order_pdf(order_ref: str):
    """Génère le bon de préparation PDF. order_ref = numéro Ecwid ou référence Odoo (ex: S00001)."""
    try:
        # Chercher par référence Odoo (name) ou numéro Ecwid (client_order_ref)
        orders = odoo_execute("sale.order", "search_read",
            [["|", ["name", "=", order_ref], ["client_order_ref", "=", order_ref]]],
            {"fields": ["name", "date_order", "partner_id", "client_order_ref", "order_line", "note"],
             "limit": 1}
        )
        if not orders:
            return {"status": "error", "error": f"Commande '{order_ref}' introuvable"}
        order = orders[0]

        # Récupérer le client
        partner = odoo_execute("res.partner", "read",
            [[order["partner_id"][0]]],
            {"fields": ["name", "street", "street2", "city", "zip", "country_id", "phone"]}
        )[0]

        # Récupérer les lignes de commande
        lines = odoo_execute("sale.order.line", "read",
            [order["order_line"]],
            {"fields": ["product_id", "product_uom_qty", "name"]}
        )

        # Récupérer les SKUs et noms EN + TH des variantes
        variant_ids = [l["product_id"][0] for l in lines if l.get("product_id")]
        variants_en = odoo_execute("product.product", "read", [variant_ids],
            {"fields": ["id", "default_code", "display_name"], "context": {"lang": "en_US"}}
        )
        variants_th = odoo_execute("product.product", "read", [variant_ids],
            {"fields": ["id", "display_name"], "context": {"lang": "th_TH"}}
        )
        sku_map = {v["id"]: v.get("default_code") or "" for v in variants_en}
        name_en_map = {v["id"]: re.sub(r'^\[.*?\]\s*', '', v.get("display_name") or "").strip() for v in variants_en}
        name_th_map = {v["id"]: re.sub(r'^\[.*?\]\s*', '', v.get("display_name") or "").strip() for v in variants_th}

        # La variation entre parenthèses n'est pas traduite par Odoo : si elle apparaît
        # telle quelle à la fin du nom thaï (identique à l'anglais), on la retire pour éviter la redondance.
        for vid, name_th in list(name_th_map.items()):
            name_en = name_en_map.get(vid, "")
            suffix_match = re.search(r'(\s*\([^)]*\))$', name_en)
            if suffix_match and name_th.endswith(suffix_match.group(1)):
                name_th_map[vid] = name_th[:-len(suffix_match.group(1))].strip()

        # Numéro de commande affiché
        ecwid_order_ref = order.get("client_order_ref") or ""
        order_ref = ecwid_order_ref or order["name"]
        order_date = str(order["date_order"])[:10] if order.get("date_order") else ""
        customer_name = partner["name"]
        phone = partner.get("phone") or ""

        # Adresse client — si Odoo n'a pas d'adresse, on essaie Ecwid
        addr_parts = [partner.get("street") or "", partner.get("street2") or "",
                      partner.get("city") or "", partner.get("zip") or ""]
        address = "\n".join(p for p in addr_parts if p)

        if not address and ecwid_order_ref:
            try:
                ecwid_orders = ecwid_get("/orders", {"orderNumber": ecwid_order_ref, "limit": 1})
                if ecwid_orders and ecwid_orders.get("items"):
                    eco = ecwid_orders["items"][0]
                    ship = eco.get("shippingPerson") or eco.get("billingPerson") or {}
                    ecwid_name = ship.get("name") or eco.get("email") or customer_name
                    if ecwid_name and ecwid_name != customer_name:
                        customer_name = ecwid_name
                    if not phone:
                        phone = ship.get("phone") or ""
                    addr_parts = [
                        ship.get("companyName") or "",
                        ship.get("street") or "",
                        ship.get("city") or "",
                        ship.get("stateOrProvinceName") or "",
                        ship.get("postalCode") or "",
                        ship.get("countryName") or "",
                    ]
                    address = "\n".join(p for p in addr_parts if p)
            except Exception:
                pass

        # Générer le PDF
        buf = io.BytesIO()
        w, h = A4
        c = canvas.Canvas(buf, pagesize=A4)

        # ─── LOGO ───
        if os.path.exists(_LOGO_PATH):
            c.drawImage(_LOGO_PATH, 15*mm, h - 40*mm, width=28*mm, height=28*mm,
                        preserveAspectRatio=True, mask='auto')
        else:
            c.setFont("Helvetica-Bold", 14)
            c.setFillColor(colors.HexColor("#1a3a6b"))
            c.drawString(15*mm, h - 25*mm, "FRENCH")
            c.drawString(15*mm, h - 32*mm, "DELICATESSEN")
            c.setFont("Helvetica", 8)
            c.drawString(15*mm, h - 37*mm, "fresh and dry deli")

        # ─── SHIP TO (droite) ───
        c.setStrokeColor(colors.black)
        c.setLineWidth(1)
        c.rect(95*mm, h - 55*mm, 105*mm, 50*mm)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(97*mm, h - 18*mm, "SHIP TO")
        c.setFont("Helvetica-Bold", 12)
        c.drawString(97*mm, h - 27*mm, customer_name)
        c.setFont("Helvetica", 9)
        y_addr = h - 35*mm
        for line_addr in address.split("\n"):
            c.drawString(97*mm, y_addr, line_addr)
            y_addr -= 5*mm

        # ─── Boîte gauche : cases S1..F3 ───
        c.setStrokeColor(colors.black)
        c.setLineWidth(0.8)
        c.rect(15*mm, h - 78*mm, 72*mm, 22*mm)
        box_labels = ["S1", "S2", "A1", "F1", "F2", "F3"]
        for i, lbl in enumerate(box_labels):
            x = 17*mm + i * 11.5*mm
            c.setFont("Helvetica-Bold", 8)
            c.setFillColor(colors.black)
            c.drawString(x + 1*mm, h - 62*mm, lbl)   # label AU-DESSUS de la case
            c.rect(x, h - 75*mm, 8*mm, 8*mm)          # case à cocher

        # ─── Chilled / Frozen ───
        c.rect(15*mm, h - 95*mm, 72*mm, 15*mm)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(17*mm, h - 85*mm, "Chilled")
        c.rect(17*mm, h - 93*mm, 10*mm, 7*mm)
        c.drawString(32*mm, h - 85*mm, "Frozen")
        c.rect(32*mm, h - 93*mm, 10*mm, 7*mm)

        # ─── Infos commande (droite) ───
        c.setFont("Helvetica-Bold", 10)
        c.rect(95*mm, h - 75*mm, 105*mm, 18*mm)
        c.drawString(97*mm, h - 65*mm, order_date)
        c.setFont("Helvetica", 9)
        c.drawString(140*mm, h - 65*mm, "Pro")
        c.setFont("Helvetica-Bold", 10)
        c.drawString(97*mm, h - 73*mm, order_ref)

        c.rect(95*mm, h - 95*mm, 105*mm, 15*mm)
        c.setFont("Helvetica", 8)
        c.drawString(97*mm, h - 84*mm, "Phone")
        c.drawString(140*mm, h - 84*mm, phone)
        c.drawString(97*mm, h - 92*mm, "Customer Order")

        # ─── Bande client + ref ───
        c.setFillColor(colors.HexColor("#1a3a6b"))
        c.rect(15*mm, h - 105*mm, 185*mm, 8*mm, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(17*mm, h - 102*mm, customer_name)
        c.drawString(120*mm, h - 102*mm, order_ref)
        c.drawString(185*mm, h - 102*mm, "1/1")

        # ─── En-têtes tableau ───
        c.setFillColor(colors.HexColor("#1a3a6b"))
        c.rect(15*mm, h - 114*mm, 185*mm, 8*mm, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(17*mm, h - 111*mm, "Qty Ordered")
        c.drawString(45*mm, h - 111*mm, "kg/unit")
        c.drawString(70*mm, h - 111*mm, "Designation and variation")
        c.drawString(160*mm, h - 111*mm, "SKU")
        c.drawString(175*mm, h - 111*mm, "Qty delivered")

        # ─── Lignes produits ───
        y = h - 120*mm
        c.setFillColor(colors.black)
        total_products = 0

        for line in lines:
            if not line.get("product_id"):
                continue
            vid = line["product_id"][0]
            qty = line.get("product_uom_qty", 0)
            uom = "kg"
            sku = sku_map.get(vid, "")
            name_en = name_en_map.get(vid, "")
            name_th = name_th_map.get(vid, "")
            # N'afficher le thaï que s'il est différent de l'anglais
            show_th = name_th and name_th != name_en

            # Nom EN (gras)
            c.setFont("Helvetica-Bold", 9)
            c.setFillColor(colors.black)
            c.drawString(70*mm, y, name_en)

            # Nom TH (en dessous, police Unicode)
            if show_th:
                draw_mixed_text(c, 70*mm, y - 4.5*mm, name_th, 8)

            # Qté dans grande police
            c.setFont("Helvetica-Bold", 16)
            row_mid = y - (5*mm if show_th else 3*mm)
            c.drawString(17*mm, row_mid, str(int(qty) if qty == int(qty) else qty))
            c.setFont("Helvetica", 9)
            c.drawString(38*mm, row_mid + 1*mm, uom)

            # SKU
            c.setFont("Helvetica", 8)
            c.drawString(160*mm, y, sku)

            # Case Qty delivered
            row_h = 14*mm if show_th else 10*mm
            c.rect(175*mm, y - row_h + 4*mm, 22*mm, row_h)

            # Séparateur
            c.setLineWidth(0.3)
            sep_y = y - row_h + 2*mm
            c.line(15*mm, sep_y, 200*mm, sep_y)

            y -= (row_h + 4*mm)
            total_products += 1

            if y < 40*mm:  # nouvelle page si besoin
                c.showPage()
                y = h - 30*mm

        # ─── Total ───
        c.setFont("Helvetica-Bold", 9)
        c.drawString(15*mm, y, f"{total_products}   Total Product")

        # ─── Footer ───
        c.setFont("Helvetica", 7)
        c.setFillColor(colors.grey)
        footer = "French Delicatessen Ltd – 64/21 Moo 2 – Bang Saray – Sattahip – 20250 Chon Buri"
        c.drawCentredString(w/2, 12*mm, footer)
        c.drawCentredString(w/2, 8*mm, "0828.04.04.55 – Contact@french-delicatessen.co.th")

        c.save()
        buf.seek(0)

        filename = f"BonPreparation_{order_ref}.pdf"
        return StreamingResponse(buf, media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"})

    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/set-chipolata-pro-prices")
def set_chipolata_pro_prices():
    """Définit les prix PRO par variante Chipolata : nature=480, autres=485 THB."""
    try:
        # Trouver la liste de prix PRO Standard
        pricelists = odoo_execute("product.pricelist", "search_read",
            [[["name", "=", "Tarif PRO Standard"]]],
            {"fields": ["id"], "limit": 1}
        )
        if not pricelists:
            return {"status": "error", "error": "Liste de prix 'Tarif PRO Standard' introuvable"}
        pricelist_id = pricelists[0]["id"]

        # Supprimer les règles existantes pour template 74
        old_items = odoo_execute("product.pricelist.item", "search",
            [[["pricelist_id", "=", pricelist_id], ["product_tmpl_id", "=", 74]]]
        )
        if old_items:
            odoo_execute("product.pricelist.item", "unlink", [old_items])

        # Nature → 480 THB
        nature_ids = [476, 481, 486]
        # Autres saveurs → 485 THB
        other_ids = [477, 478, 479, 480, 482, 483, 484, 485, 487, 488, 489, 490]

        rules = []
        for vid in nature_ids:
            rules.append({
                "pricelist_id": pricelist_id,
                "applied_on": "0_product_variant",
                "product_id": vid,
                "compute_price": "fixed",
                "fixed_price": 480,
            })
        for vid in other_ids:
            rules.append({
                "pricelist_id": pricelist_id,
                "applied_on": "0_product_variant",
                "product_id": vid,
                "compute_price": "fixed",
                "fixed_price": 485,
            })

        odoo_execute("product.pricelist.item", "create", [rules])

        return {
            "status": "ok",
            "rules_created": len(rules),
            "nature_variants": len(nature_ids),
            "other_variants": len(other_ids),
            "nature_price": 480,
            "other_price": 485
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/assign-pro-category")
def assign_pro_category():
    """Assigne la catégorie 'Pro' à tous les produits PRO créés (templates 450-535)."""
    try:
        # Trouver la catégorie "Pro"
        categories = odoo_execute("product.category", "search_read",
            [[["name", "=", "Pro"]]],
            {"fields": ["id", "name"], "limit": 5}
        )
        if not categories:
            return {"status": "error", "error": "Catégorie 'Pro' introuvable dans Odoo"}
        cat_id = categories[0]["id"]

        # Trouver tous les produits de la liste PRO qui existent
        names = list(PRO_STANDARD_PRICES.keys())
        templates = odoo_execute("product.template", "search_read",
            [[["name", "in", names]]],
            {"fields": ["id", "name"], "limit": 500}
        )
        template_ids = [t["id"] for t in templates]

        odoo_execute("product.template", "write", [template_ids, {"categ_id": cat_id}])

        return {
            "status": "ok",
            "category": "Pro",
            "category_id": cat_id,
            "products_updated": len(template_ids)
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/create-pro-products")
def create_pro_products():
    """Crée dans Odoo les produits PRO manquants (ceux absents du catalogue actuel)."""
    try:
        # Trouver l'UoM kg
        uom_kg = odoo_execute("uom.uom", "search_read",
            [[["name", "=", "kg"]]],
            {"fields": ["id", "name"], "limit": 1}
        )
        uom_id = uom_kg[0]["id"] if uom_kg else False

        # Trouver les produits déjà existants
        names = list(PRO_STANDARD_PRICES.keys())
        existing = odoo_execute("product.template", "search_read",
            [[["name", "in", names], ["active", "=", True]]],
            {"fields": ["name"], "limit": 500}
        )
        existing_names = {t["name"] for t in existing}
        to_create = {n: p for n, p in PRO_STANDARD_PRICES.items() if n not in existing_names}

        if not to_create:
            return {"status": "ok", "created_count": 0, "skipped_existing": len(existing_names), "message": "Tous les produits existent déjà"}

        vals_list = []
        for name, price in to_create.items():
            vals = {"name": name, "type": "consu", "sale_ok": True, "purchase_ok": True, "list_price": price}
            if uom_id:
                vals["uom_id"] = uom_id
            vals_list.append(vals)

        new_ids = odoo_execute("product.template", "create", [vals_list])
        if not isinstance(new_ids, list):
            new_ids = [new_ids]

        return {
            "status": "ok",
            "created_count": len(new_ids),
            "skipped_existing": len(existing_names),
            "error_count": 0,
            "new_template_ids": new_ids
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/pro-pricelist-preview")
def pro_pricelist_preview():
    """Aperçu : quels produits PRO existent dans Odoo (par nom) avant création de la liste de prix."""
    try:
        names = list(PRO_STANDARD_PRICES.keys())
        templates = odoo_execute("product.template", "search_read",
            [[["name", "in", names], ["active", "=", True]]],
            {"fields": ["id", "name"], "limit": 500}
        )
        found_names = {t["name"] for t in templates}
        not_found = [n for n in names if n not in found_names]
        return {
            "status": "ok",
            "total_in_list": len(names),
            "found_in_odoo": len(found_names),
            "not_found_count": len(not_found),
            "found": sorted(found_names),
            "not_found": sorted(not_found)
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/create-pro-pricelist")
def create_pro_pricelist():
    """Crée la liste de prix 'Tarif PRO Standard' dans Odoo avec les prix du catalogue PRO."""
    try:
        # Trouver la devise THB
        currencies = odoo_execute("res.currency", "search_read",
            [[["name", "=", "THB"]]],
            {"fields": ["id", "name"], "limit": 1}
        )
        currency_id = currencies[0]["id"] if currencies else False

        # Vérifier si la liste de prix existe déjà
        existing = odoo_execute("product.pricelist", "search_read",
            [[["name", "=", "Tarif PRO Standard"]]],
            {"fields": ["id", "name"], "limit": 1}
        )

        if existing:
            pricelist_id = existing[0]["id"]
            # Supprimer les anciennes règles
            old_items = odoo_execute("product.pricelist.item", "search",
                [[["pricelist_id", "=", pricelist_id]]]
            )
            if old_items:
                odoo_execute("product.pricelist.item", "unlink", [old_items])
        else:
            pricelist_id = odoo_execute("product.pricelist", "create", [{
                "name": "Tarif PRO Standard",
                "currency_id": currency_id,
            }])

        # Chercher les templates par nom
        names = list(PRO_STANDARD_PRICES.keys())
        templates = odoo_execute("product.template", "search_read",
            [[["name", "in", names], ["active", "=", True]]],
            {"fields": ["id", "name"], "limit": 500}
        )
        template_map = {t["name"]: t["id"] for t in templates}

        created, not_found = [], []
        for name, price in PRO_STANDARD_PRICES.items():
            tmpl_id = template_map.get(name)
            if not tmpl_id:
                not_found.append(name)
                continue
            odoo_execute("product.pricelist.item", "create", [{
                "pricelist_id": pricelist_id,
                "applied_on": "1_product",
                "product_tmpl_id": tmpl_id,
                "compute_price": "fixed",
                "fixed_price": price,
            }])
            created.append({"name": name, "price": price})

        return {
            "status": "ok",
            "pricelist_id": pricelist_id,
            "pricelist_name": "Tarif PRO Standard",
            "rules_created": len(created),
            "not_found_count": len(not_found),
            "not_found": not_found
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/disable-bacon-unused")
def disable_bacon_unused():
    try:

        keep_ids = [464,468,472]

        all_ids = [464,465,466,467,468,469,470,471,472]

        disable_ids = [
            x for x in all_ids
            if x not in keep_ids
        ]

        odoo_execute(
            "product.product",
            "write",
            [
                disable_ids,
                {
                    "active": False
                }
            ]
        )

        return {
            "status": "ok",
            "kept": keep_ids,
            "disabled": disable_ids,
            "disabled_count": len(disable_ids)
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }


# ─── Numérotation des commandes ───────────────────────────────────────────────

SEQ_CODES = {
    "P": "sale.order.pro",
    "D": "fd.order.direct",
    "S": "fd.order.shop",
}


@app.get("/next-order-number/{order_type}")
def next_order_number(order_type: str):
    """Retourne ET consomme le prochain numéro de séquence. order_type: P, D ou S."""
    order_type = order_type.upper()
    if order_type not in SEQ_CODES:
        return {"status": "error", "error": f"Type invalide: {order_type}. Utiliser P, D ou S."}
    try:
        seq_code = SEQ_CODES[order_type]
        number = odoo_execute("ir.sequence", "next_by_code", [[seq_code]])
        if not number:
            return {"status": "error", "error": f"Séquence '{seq_code}' introuvable dans Odoo"}
        return {"status": "ok", "order_type": order_type, "sequence_code": seq_code, "number": number}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/set-order-number/{order_id}/{order_type}")
def set_order_number(order_id: int, order_type: str):
    """
    Assigne un numéro FD à une commande Odoo existante.
    order_type: P (PRO), D (Direct), S (Shop)
    Consomme un numéro de séquence.
    """
    order_type = order_type.upper()
    if order_type not in SEQ_CODES:
        return {"status": "error", "error": f"Type invalide: {order_type}. Utiliser P, D ou S."}
    try:
        orders = odoo_execute("sale.order", "search_read",
            [[["id", "=", order_id]]],
            {"fields": ["id", "name", "state"], "limit": 1}
        )
        if not orders:
            return {"status": "error", "error": f"Commande ID {order_id} introuvable"}

        old_name = orders[0]["name"]
        seq_code = SEQ_CODES[order_type]
        new_name = odoo_execute("ir.sequence", "next_by_code", [[seq_code]])
        if not new_name:
            return {"status": "error", "error": f"Séquence '{seq_code}' introuvable"}

        odoo_execute("sale.order", "write", [[order_id], {"name": new_name}])

        return {
            "status": "ok",
            "order_id": order_id,
            "old_name": old_name,
            "new_name": new_name,
            "order_type": order_type
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/order-number-preview/{order_type}")
def order_number_preview(order_type: str):
    """Aperçu du prochain numéro SANS le consommer. order_type: P, D ou S."""
    order_type = order_type.upper()
    if order_type not in SEQ_CODES:
        return {"status": "error", "error": f"Type invalide: {order_type}. Utiliser P, D ou S."}
    try:
        seq_code = SEQ_CODES[order_type]
        seqs = odoo_execute("ir.sequence", "search_read",
            [[["code", "=", seq_code]]],
            {"fields": ["id", "name", "prefix", "suffix", "number_next_actual", "padding"], "limit": 1}
        )
        if not seqs:
            return {"status": "error", "error": f"Séquence '{seq_code}' introuvable"}
        seq = seqs[0]
        return {
            "status": "ok",
            "sequence": seq["name"],
            "code": seq_code,
            "prefix": seq.get("prefix"),
            "suffix": seq.get("suffix"),
            "next_number": seq.get("number_next_actual"),
            "padding": seq.get("padding"),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─── Traductions thaïes ───────────────────────────────────────────────────────

class ThaiNamesPayload(BaseModel):
    translations: Dict[str, str]  # {nom_anglais: nom_thai}


@app.post("/set-thai-names")
def set_thai_names(payload: ThaiNamesPayload):
    """
    Écrit les traductions thaïes des noms produits dans Odoo.
    Body JSON: {"translations": {"Chipolata": "ไส้กรอกหมู", ...}}
    Cherche les templates par nom anglais, écrit le nom thaï via contexte lang=th_TH.
    """
    try:
        names_en = list(payload.translations.keys())

        # Chercher les templates par nom anglais
        templates = odoo_execute("product.template", "search_read",
            [[["name", "in", names_en]]],
            {"fields": ["id", "name"], "limit": 500, "context": {"lang": "en_US"}}
        )

        updated, not_found = [], []

        for tmpl in templates:
            tmpl_id = tmpl["id"]
            name_en = tmpl["name"]
            name_th = payload.translations.get(name_en)
            if not name_th:
                continue
            # Écrire le nom thaï avec le contexte de langue
            odoo_execute("product.template", "write",
                [[tmpl_id], {"name": name_th}],
                {"context": {"lang": "th_TH"}}
            )
            updated.append({"id": tmpl_id, "en": name_en, "th": name_th})

        found_names = {t["name"] for t in templates}
        not_found = [n for n in names_en if n not in found_names]

        return {
            "status": "ok",
            "updated": len(updated),
            "not_found": not_found,
            "details": updated
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/get-thai-names")
def get_thai_names():
    """Retourne les noms EN et TH de tous les produits actifs."""
    try:
        templates_en = odoo_execute("product.template", "search_read",
            [[["active", "=", True]]],
            {"fields": ["id", "name"], "limit": 1000, "context": {"lang": "en_US"}}
        )
        ids = [t["id"] for t in templates_en]
        templates_th = odoo_execute("product.template", "read",
            [ids],
            {"fields": ["id", "name"], "context": {"lang": "th_TH"}}
        )
        en_map = {t["id"]: t["name"] for t in templates_en}
        th_map = {t["id"]: t["name"] for t in templates_th}

        result = []
        for tid in ids:
            en = en_map.get(tid, "")
            th = th_map.get(tid, "")
            result.append({"id": tid, "en": en, "th": th, "has_thai": th != en})

        return {"status": "ok", "count": len(result), "products": result}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/restore-english-names")
def restore_english_names(payload: ThaiNamesPayload):
    """
    Restaure les noms anglais en cherchant les produits par leur nom thai actuel.
    Body: {"translations": {"nom_anglais": "nom_thai"}} - meme format que set-thai-names.
    Cherche par nom thai et reecrit le nom anglais sans contexte de langue.
    """
    try:
        restored, not_found = [], []
        # Inverser: thai -> anglais
        th_to_en = {v: k for k, v in payload.translations.items()}
        thai_names = list(th_to_en.keys())

        # Chercher par nom thai (sans contexte langue = cherche dans le champ principal)
        templates = odoo_execute("product.template", "search_read",
            [[["name", "in", thai_names]]],
            {"fields": ["id", "name"], "limit": 1000}
        )

        for tmpl in templates:
            thai_found = tmpl["name"]
            english_name = th_to_en.get(thai_found)
            if not english_name:
                continue
            # Ecrire le nom anglais sans contexte de langue
            odoo_execute("product.template", "write",
                [[tmpl["id"]], {"name": english_name}]
            )
            restored.append({"id": tmpl["id"], "was": thai_found, "now": english_name})

        found_thai = {t["name"] for t in templates}
        not_found = [t for t in thai_names if t not in found_thai]

        return {
            "status": "ok",
            "restored": len(restored),
            "not_found": len(not_found),
            "details": restored
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─── Import clients depuis Excel ──────────────────────────────────────────────

@app.post("/import-clients-xlsx")
async def import_clients_xlsx(file: UploadFile = File(...)):
    """
    Importe / met à jour les partenaires Odoo depuis Table Client.xlsx.
    Colonnes attendues : PRO | Nom | Nickname | taxID | Telephone | Email |
                         ok email | telephone thai | Adresse | Sub | District |
                         Postal code | province | contact | tel contact | email contact
    Matching : par Email d'abord, puis par Nom.
    """
    try:
        import pandas as pd

        content = await file.read()
        buf = io.BytesIO(content)
        df = pd.read_excel(buf, dtype=str)
        df = df.where(pd.notna(df), "")

        # Normaliser les noms de colonnes
        df.columns = [str(c).strip() for c in df.columns]

        col_pro   = "PRO"
        col_nom   = "Nom"
        col_nick  = "Nickname"
        col_tax   = "taxID"
        col_tel   = "Telephone"
        col_email = "Email"
        col_tel_th = "telephone thai"
        col_addr  = "Adresse"
        col_sub   = "Sub"
        col_dist  = "District"
        col_zip   = "Postal code"
        col_prov  = "province"
        col_cont  = "contact"
        col_tel_c = "tel contact"
        col_email_c = "email contact"

        created, updated, skipped, errors = [], [], [], []

        for _, row in df.iterrows():
            nom   = str(row.get(col_nom, "")).strip()
            if not nom:
                skipped.append({"reason": "nom vide"})
                continue

            email  = str(row.get(col_email, "")).strip()
            phone  = str(row.get(col_tel, "")).strip() or str(row.get(col_tel_th, "")).strip()
            street = str(row.get(col_addr, "")).strip()
            sub    = str(row.get(col_sub, "")).strip()
            dist   = str(row.get(col_dist, "")).strip()
            zip_c  = str(row.get(col_zip, "")).strip()
            prov   = str(row.get(col_prov, "")).strip()
            nick   = str(row.get(col_nick, "")).strip()
            tax_id = str(row.get(col_tax, "")).strip()
            contact_name = str(row.get(col_cont, "")).strip()
            pro_flag = str(row.get(col_pro, "")).strip()

            # Construire l'adresse : rue + sous-district
            street_full = street
            if sub and sub not in street:
                street_full = (street + " " + sub).strip()

            # Ville = district + province
            city = dist or prov
            if dist and prov and dist != prov:
                city = dist

            vals = {
                "name": nom,
                "phone": phone,
                "street": street_full,
                "street2": dist if dist and dist != city else "",
                "zip": zip_c,
                "city": prov or dist,
            }
            if email:
                vals["email"] = email
            if nick:
                vals["ref"] = nick
            if tax_id:
                vals["vat"] = tax_id
            # Tag PRO
            comment = ""
            if pro_flag == "1":
                comment = "PRO"
            if contact_name:
                comment = (comment + " | " + contact_name).strip(" |")
            if comment:
                vals["comment"] = comment

            # Recherche dans Odoo
            partner_id = None
            if email:
                results = odoo_execute("res.partner", "search_read",
                    [[["email", "=", email], ["active", "=", True]]],
                    {"fields": ["id", "name"], "limit": 1}
                )
                if results:
                    partner_id = results[0]["id"]

            if not partner_id:
                results = odoo_execute("res.partner", "search_read",
                    [[["name", "=", nom], ["active", "=", True]]],
                    {"fields": ["id", "name"], "limit": 1}
                )
                if results:
                    partner_id = results[0]["id"]

            if partner_id:
                odoo_execute("res.partner", "write", [[partner_id], vals])
                updated.append({"id": partner_id, "name": nom})
            else:
                new_id = odoo_execute("res.partner", "create", [vals])
                created.append({"id": new_id, "name": nom})

        return {
            "status": "ok",
            "total_rows": len(df),
            "updated": len(updated),
            "created": len(created),
            "skipped": len(skipped),
            "errors": len(errors),
            "updated_list": updated[:20],
            "created_list": created[:20],
        }

    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─── Import commandes depuis WinDev (2 fichiers CSV/Excel) ────────────────────

@app.post("/import-commandes-xlsx")
async def import_commandes_xlsx(
    orders_file: UploadFile = File(...),
    lines_file: UploadFile = File(...),
    order_type: str = "D",
):
    """
    Importe les commandes WinDev dans Odoo (Option A).
    - orders_file : export COMMANDES (CSV ou Excel)
    - lines_file  : export LIGNCDE  (CSV ou Excel)
    - order_type  : P (PRO), D (Direct boutique), S (Shop Ecwid) — défaut D

    Option A : name Odoo = nouveau numéro FD (séquence P/D/S)
               client_order_ref = numéro WinDev original
    """
    import pandas as pd

    order_type = order_type.upper()
    if order_type not in SEQ_CODES:
        return {"status": "error", "error": f"order_type invalide: {order_type}. Utiliser P, D ou S."}

    seq_code = SEQ_CODES[order_type]

    def read_file(upload: UploadFile) -> pd.DataFrame:
        content = upload.file.read()
        buf = io.BytesIO(content)
        name = upload.filename or ""
        if name.lower().endswith(".csv"):
            try:
                df = pd.read_csv(buf, dtype=str, sep="\t", encoding="utf-8")
            except Exception:
                buf.seek(0)
                df = pd.read_csv(buf, dtype=str, sep=";", encoding="utf-8")
        else:
            df = pd.read_excel(buf, dtype=str)
        df.columns = [str(c).strip() for c in df.columns]
        return df.where(pd.notna(df), "")

    def find_col(df: pd.DataFrame, candidates: list):
        for c in candidates:
            if c in df.columns:
                return c
        return None

    try:
        df_orders = read_file(orders_file)
        df_lines  = read_file(lines_file)
    except Exception as e:
        return {"status": "error", "error": f"Lecture fichiers: {e}"}

    # ── Colonnes commandes ──────────────────────────────────────────────────
    col_num    = find_col(df_orders, ["NumCommande","Numero","RefCommande","Num","N°","Numéro","Number"])
    col_date   = find_col(df_orders, ["DateCommande","Date","date_order","Date commande"])
    col_client = find_col(df_orders, ["NomClient","Client","CodeClient","Nom client","Customer"])
    col_statut = find_col(df_orders, ["StatutCommande","Statut","Etat","Status","Etat commande"])

    if not col_num:
        return {"status": "error", "error": f"Colonne numéro commande introuvable. Colonnes dispo: {list(df_orders.columns)}"}
    if not col_client:
        return {"status": "error", "error": f"Colonne client introuvable. Colonnes dispo: {list(df_orders.columns)}"}

    # ── Colonnes lignes ─────────────────────────────────────────────────────
    ln_num  = find_col(df_lines, ["NumCommande","Numero","RefCommande","Num","N°","Numéro","Number"])
    ln_art  = find_col(df_lines, ["CodeArticle","RefArticle","Article","Code","Ref","SKU"])
    ln_des  = find_col(df_lines, ["Designation","Description","NomArticle","Libelle","Nom article"])
    ln_qty  = find_col(df_lines, ["Quantite","Qte","Qty","Quantité","Quantity"])
    ln_prix = find_col(df_lines, ["PrixUnitaire","PrixVente","Prix","Price","PU","Prix unitaire"])
    ln_rem  = find_col(df_lines, ["Remise","TauxRemise","Discount","Remise%","Remise %"])

    if not ln_num:
        return {"status": "error", "error": f"Colonne numéro commande (lignes) introuvable. Colonnes dispo: {list(df_lines.columns)}"}

    # ── Pré-charger les produits Odoo ───────────────────────────────────────
    odoo_products = odoo_execute("product.product", "search_read",
        [[["active", "=", True]]],
        {"fields": ["id", "name", "default_code", "uom_id"], "limit": 2000, "context": {"lang": "en_US"}}
    )
    prod_by_ref  = {str(p["default_code"]).strip().upper(): p for p in odoo_products if p.get("default_code")}
    prod_by_name = {}
    for p in odoo_products:
        key = str(p["name"]).strip().lower()
        if key not in prod_by_name:
            prod_by_name[key] = p

    def find_product(code: str, name: str):
        if code:
            p = prod_by_ref.get(str(code).strip().upper())
            if p:
                return p
        if name:
            p = prod_by_name.get(str(name).strip().lower())
            if p:
                return p
        return None

    # ── Pré-charger les partenaires Odoo ───────────────────────────────────
    odoo_partners = odoo_execute("res.partner", "search_read",
        [[["active", "=", True]]],
        {"fields": ["id", "name", "email"], "limit": 5000}
    )
    partner_by_name  = {str(p["name"]).strip().lower(): p["id"] for p in odoo_partners}
    partner_by_email = {str(p["email"]).strip().lower(): p["id"] for p in odoo_partners if p.get("email")}

    def find_partner(client_str: str):
        key = str(client_str).strip().lower()
        pid = partner_by_name.get(key)
        if pid:
            return pid
        return partner_by_email.get(key)

    # ── Grouper les lignes par numéro commande ──────────────────────────────
    lines_by_order = {}
    for _, row in df_lines.iterrows():
        num = str(row.get(ln_num, "")).strip()
        if num:
            lines_by_order.setdefault(num, []).append(row)

    # ── Importer les commandes ──────────────────────────────────────────────
    created, skipped, errors_list = [], [], []

    for _, row in df_orders.iterrows():
        windev_num = str(row.get(col_num, "")).strip()
        client_str = str(row.get(col_client, "")).strip()
        date_str   = str(row.get(col_date, "")).strip() if col_date else ""

        if not windev_num:
            skipped.append({"reason": "numéro vide"})
            continue

        partner_id = find_partner(client_str)
        if not partner_id:
            errors_list.append({"windev": windev_num, "reason": f"Client introuvable: '{client_str}'"})
            continue

        fd_number = odoo_execute("ir.sequence", "next_by_code", [[seq_code]])
        if not fd_number:
            errors_list.append({"windev": windev_num, "reason": f"Séquence '{seq_code}' introuvable"})
            continue

        order_vals = {
            "partner_id": partner_id,
            "client_order_ref": windev_num,
            "name": fd_number,
        }

        if date_str:
            try:
                m = re.match(r"(\d{2})[/\-](\d{2})[/\-](\d{4})", date_str)
                if m:
                    order_vals["date_order"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)} 00:00:00"
                else:
                    m2 = re.match(r"(\d{4})[/\-](\d{2})[/\-](\d{2})", date_str)
                    if m2:
                        order_vals["date_order"] = f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)} 00:00:00"
            except Exception:
                pass

        try:
            order_id = odoo_execute("sale.order", "create", [order_vals])
        except Exception as e:
            errors_list.append({"windev": windev_num, "reason": f"Création: {e}"})
            continue

        # ── Lignes de commande ──────────────────────────────────────────────
        order_lines = lines_by_order.get(windev_num, [])
        lines_created, lines_missing = 0, []

        for line_row in order_lines:
            art_code = str(line_row.get(ln_art, "")).strip() if ln_art else ""
            art_name = str(line_row.get(ln_des, "")).strip() if ln_des else ""
            qty_str  = str(line_row.get(ln_qty, "1")).strip() if ln_qty else "1"
            prix_str = str(line_row.get(ln_prix, "0")).strip() if ln_prix else "0"
            rem_str  = str(line_row.get(ln_rem, "0")).strip() if ln_rem else "0"

            try:
                qty    = float(qty_str.replace(",", ".")) if qty_str else 1.0
                prix   = float(prix_str.replace(",", ".")) if prix_str else 0.0
                remise = float(rem_str.replace(",", ".")) if rem_str else 0.0
            except ValueError:
                qty, prix, remise = 1.0, 0.0, 0.0

            product = find_product(art_code, art_name)
            if not product:
                lines_missing.append(art_code or art_name)
                line_vals = {
                    "order_id": order_id,
                    "name": art_name or art_code or "Article inconnu",
                    "product_uom_qty": qty,
                    "price_unit": prix,
                    "discount": remise,
                }
            else:
                uom_id = product["uom_id"][0] if isinstance(product.get("uom_id"), list) else False
                line_vals = {
                    "order_id": order_id,
                    "product_id": product["id"],
                    "name": art_name or product["name"],
                    "product_uom_qty": qty,
                    "price_unit": prix,
                    "discount": remise,
                }
                if uom_id:
                    line_vals["product_uom"] = uom_id

            try:
                odoo_execute("sale.order.line", "create", [line_vals])
                lines_created += 1
            except Exception as e:
                lines_missing.append(f"erreur ligne: {e}")

        try:
            odoo_execute("sale.order", "action_confirm", [[order_id]])
        except Exception:
            pass

        created.append({
            "windev": windev_num,
            "fd": fd_number,
            "partner": client_str,
            "lines_created": lines_created,
            "lines_missing": lines_missing[:5] if lines_missing else [],
        })

    return {
        "status": "ok",
        "order_type": order_type,
        "seq_code": seq_code,
        "total_orders": len(df_orders),
        "created": len(created),
        "skipped": len(skipped),
        "errors": len(errors_list),
        "created_sample": created[:10],
        "errors_detail": errors_list[:20],
    }


# ─── Sync commandes Ecwid → Odoo ─────────────────────────────────────────────

@app.get("/sync-ecwid-orders")
def sync_ecwid_orders(
    since_order_id: int = 0,
    since_days: int = 30,
):
    """
    Importe TOUTES les commandes Ecwid dans Odoo (tous statuts, toutes pages).
    - since_order_id : si fourni, importe seulement les commandes avec orderNumber > valeur

    Chaque commande Ecwid crée un sale.order Odoo numéroté FD-S-XXXX.
    Le client est trouvé par email ; créé si absent.
    Les produits sont trouvés par SKU puis par nom.
    Les commandes déjà importées sont ignorées (vérification par client_order_ref).
    """
    seq_code = SEQ_CODES["S"]

    # ── Pré-charger produits Odoo ────────────────────────────────────────────
    odoo_products = odoo_execute("product.product", "search_read",
        [[["active", "=", True]]],
        {"fields": ["id", "name", "default_code", "uom_id", "lst_price"],
         "limit": 2000, "context": {"lang": "en_US"}}
    )
    prod_by_sku  = {str(p["default_code"]).strip().upper(): p
                   for p in odoo_products if p.get("default_code")}
    prod_by_name = {}
    for p in odoo_products:
        key = str(p["name"]).strip().lower()
        if key not in prod_by_name:
            prod_by_name[key] = p

    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.dirname(__file__)))
        from sku_mapping import SKU_MAP as _SKU_MAP
    except Exception:
        _SKU_MAP = {}

    def find_product(sku: str, name: str):
        if sku:
            p = prod_by_sku.get(str(sku).strip().upper())
            if p:
                return p
            mapped = _SKU_MAP.get(str(sku).strip())
            if mapped:
                p = prod_by_sku.get(str(mapped).strip().upper())
                if p:
                    return p
        if name:
            p = prod_by_name.get(str(name).strip().lower())
            if p:
                return p
        return None

    # ── Pré-charger partenaires Odoo (email) ─────────────────────────────────
    odoo_partners = odoo_execute("res.partner", "search_read",
        [[["active", "=", True]]],
        {"fields": ["id", "name", "email"], "limit": 5000}
    )
    partner_by_email = {str(p["email"]).strip().lower(): p["id"]
                        for p in odoo_partners if p.get("email")}

    # ── Commandes déjà importées (toutes formes : ECWID-xxx ou xxx) ──────────
    existing = odoo_execute("sale.order", "search_read",
        [[["client_order_ref", "!=", False]]],
        {"fields": ["client_order_ref"], "limit": 5000}
    )
    already_imported = set()
    for e in existing:
        ref_val = str(e["client_order_ref"])
        already_imported.add(ref_val)
        # Ajouter aussi la forme numérique si c'est ECWID-xxx
        if ref_val.startswith("ECWID-"):
            already_imported.add(ref_val[6:])
        else:
            already_imported.add(f"ECWID-{ref_val}")

    # ── Récupérer les commandes Ecwid récentes (since_days, défaut 30j) ─────────
    from datetime import datetime, timezone, timedelta
    since_ts = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())
    all_items = []
    offset = 0
    batch = 100
    total_ecwid = 0
    while True:
        params = {"limit": batch, "offset": offset, "sortBy": "ORDER_DATE_DESC",
                  "createdFrom": since_ts}
        ecwid_data = ecwid_get("/orders", params)
        if not ecwid_data:
            return {"status": "error", "error": "Impossible de joindre l'API Ecwid"}
        items_page = ecwid_data.get("items", [])
        total_ecwid = ecwid_data.get("total", total_ecwid)
        all_items.extend(items_page)
        if len(items_page) < batch:
            break
        offset += batch

    created, skipped, errors_list = [], [], []

    for eco in all_items:
        order_num = str(eco.get("orderNumber", ""))
        if not order_num:
            skipped.append({"reason": "pas de numéro"})
            continue

        if order_num in already_imported:
            skipped.append({"ecwid": order_num, "reason": "déjà importé"})
            continue

        if since_order_id and int(order_num) <= since_order_id:
            skipped.append({"ecwid": order_num, "reason": "trop ancien"})
            continue

        # ── Client ───────────────────────────────────────────────────────────
        email = str(eco.get("email") or "").strip().lower()
        ship  = eco.get("shippingPerson") or eco.get("billingPerson") or {}
        cname = ship.get("name") or eco.get("email") or f"Ecwid #{order_num}"
        phone = ship.get("phone") or ""
        street = ship.get("street") or ""
        city   = ship.get("city") or ""
        zipcode = ship.get("postalCode") or ""
        subdistrict = _get_ecwid_subdistrict(eco)

        partner_id = partner_by_email.get(email) if email else None
        if not partner_id:
            # Créer le partenaire
            vals_p = {"name": cname}
            if email:
                vals_p["email"] = email
            if phone:
                vals_p["phone"] = phone
            if street:
                vals_p["street"] = street
            if city:
                vals_p["city"] = city
            if zipcode:
                vals_p["zip"] = zipcode
            if subdistrict:
                vals_p["x_sub_district"] = subdistrict
            try:
                partner_id = odoo_execute("res.partner", "create", [vals_p])
                if email:
                    partner_by_email[email] = partner_id
            except Exception as e:
                errors_list.append({"ecwid": order_num, "reason": f"Création client: {e}"})
                continue

        # Ignorer les commandes Ecwid sans aucun article (ex: paniers vides/annules)
        if not eco.get("items"):
            skipped.append({"ecwid": order_num, "reason": "aucun article"})
            continue

        # ── Obtenir numéro FD ─────────────────────────────────────────────────
        fd_number = odoo_execute("ir.sequence", "next_by_code", [[seq_code]])
        if not fd_number:
            errors_list.append({"ecwid": order_num, "reason": "Séquence FD-S introuvable"})
            continue

        # ── Créer commande ────────────────────────────────────────────────────
        order_date = eco.get("createDate") or eco.get("updateDate")
        order_vals = {
            "partner_id": partner_id,
            "client_order_ref": f"ECWID-{order_num}",
            "name": fd_number,
        }
        if order_date:
            try:
                from datetime import datetime
                dt = datetime.utcfromtimestamp(order_date / 1000 if order_date > 1e10 else order_date)
                order_vals["date_order"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        try:
            order_id = odoo_execute("sale.order", "create", [order_vals])
        except Exception as e:
            errors_list.append({"ecwid": order_num, "reason": f"Création commande: {e}"})
            continue

        # ── Lignes ────────────────────────────────────────────────────────────
        lines_created, lines_missing = 0, []
        for item in eco.get("items", []):
            sku      = str(item.get("sku") or "").strip()
            name_i   = str(item.get("name") or "").strip()
            qty      = float(item.get("quantity") or 1)
            price    = float(item.get("price") or 0)

            product = find_product(sku, name_i)
            if not product:
                lines_missing.append(sku or name_i)
                line_vals = {
                    "order_id": order_id,
                    "name": name_i or sku or "Article Ecwid",
                    "product_uom_qty": qty,
                    "price_unit": price,
                }
            else:
                uom_id = product["uom_id"][0] if isinstance(product.get("uom_id"), list) else False
                line_vals = {
                    "order_id": order_id,
                    "product_id": product["id"],
                    "name": name_i or product["name"],
                    "product_uom_qty": qty,
                    "price_unit": price,
                }
                if uom_id:
                    line_vals["product_uom"] = uom_id

            try:
                odoo_execute("sale.order.line", "create", [line_vals])
                lines_created += 1
            except Exception as e:
                lines_missing.append(f"erreur: {e}")

        # Supplements (ex: "Delivery Frozen or Chill")
        for surcharge in (eco.get("customSurcharges") or []):
            amount = surcharge.get("total") or surcharge.get("value") or 0
            if amount:
                try:
                    odoo_execute("sale.order.line", "create", [{
                        "order_id": order_id,
                        "product_id": 1917,
                        "name": surcharge.get("description") or "Supplement",
                        "product_uom_qty": 1,
                        "price_unit": amount,
                    }])
                    lines_created += 1
                except Exception as e:
                    lines_missing.append(f"erreur supplement: {e}")

        # Confirmer la commande
        try:
            odoo_execute("sale.order", "action_confirm", [[order_id]])
        except Exception:
            pass

        created.append({
            "ecwid": order_num,
            "fd": fd_number,
            "client": cname,
            "lines_created": lines_created,
            "lines_missing": lines_missing[:3] if lines_missing else [],
        })

    return {
        "status": "ok",
        "ecwid_total": total_ecwid,
        "processed": len(all_items),
        "created": len(created),
        "skipped": len(skipped),
        "errors": len(errors_list),
        "created_sample": created[:20],
        "errors_detail": errors_list[:10],
    }


# ─── Webhook Ecwid → Odoo (temps réel) ───────────────────────────────────────

def _parse_ecwid_date(order_date) -> str:
    """Parse date Ecwid (unix seconds, unix ms, ou string ISO) → 'YYYY-MM-DD HH:MM:SS'"""
    if not order_date:
        return ""
    try:
        from datetime import datetime
        if isinstance(order_date, (int, float)):
            ts = order_date / 1000 if order_date > 1e10 else order_date
            return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(order_date, str):
            for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
                        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(order_date[:19], fmt[:len(fmt)])
                    return dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
    except Exception:
        pass
    return ""


def _import_one_ecwid_order(order_num: str, eco: dict) -> dict:
    """Importe une commande Ecwid dans Odoo. Retourne {"fd": ..., "status": ...}"""
    seq_code = SEQ_CODES["S"]

    # Vérifier si déjà importée
    existing = odoo_execute("sale.order", "search_read",
        [[["client_order_ref", "=", order_num]]],
        {"fields": ["id", "name"], "limit": 1}
    )
    if existing:
        return {"status": "already_exists", "fd": existing[0]["name"]}

    if not eco.get("items"):
        return {"status": "skipped", "reason": "aucun article"}

    # Client
    email  = str(eco.get("email") or "").strip().lower()
    ship   = eco.get("shippingPerson") or eco.get("billingPerson") or {}
    cname  = ship.get("name") or eco.get("email") or f"Ecwid #{order_num}"
    phone  = ship.get("phone") or ""
    street = ship.get("street") or ""
    city   = ship.get("city") or ""
    zipcode = ship.get("postalCode") or ""
    subdistrict = _get_ecwid_subdistrict(eco)

    partner_id = None
    if email:
        results = odoo_execute("res.partner", "search_read",
            [[["email", "=", email], ["active", "=", True]]],
            {"fields": ["id"], "limit": 1}
        )
        if results:
            partner_id = results[0]["id"]

    if not partner_id:
        vals_p = {"name": cname}
        if email:
            vals_p["email"] = email
        if phone:
            vals_p["phone"] = phone
        if street:
            vals_p["street"] = street
        if city:
            vals_p["city"] = city
        if zipcode:
            vals_p["zip"] = zipcode
        if subdistrict:
            vals_p["x_sub_district"] = subdistrict
        partner_id = odoo_execute("res.partner", "create", [vals_p])

    # Numéro FD
    fd_number = odoo_execute("ir.sequence", "next_by_code", [[seq_code]])
    if not fd_number:
        return {"status": "error", "reason": "Séquence FD-S introuvable"}

    # Créer commande
    order_vals = {
        "partner_id": partner_id,
        "client_order_ref": order_num,
        "name": fd_number,
    }
    order_date_str = _parse_ecwid_date(eco.get("createDate") or eco.get("updateDate"))
    if order_date_str:
        order_vals["date_order"] = order_date_str

    order_id = odoo_execute("sale.order", "create", [order_vals])

    # Pré-charger produits (en_US pour matching correct)
    odoo_products = odoo_execute("product.product", "search_read",
        [[["active", "=", True]]],
        {"fields": ["id", "name", "default_code", "uom_id"], "limit": 5000,
         "context": {"lang": "en_US"}}
    )
    prod_by_sku  = {str(p["default_code"]).strip().upper(): p
                   for p in odoo_products if p.get("default_code")}
    prod_by_name = {}
    for p in odoo_products:
        key = str(p["name"]).strip().lower()
        if key not in prod_by_name:
            prod_by_name[key] = p

    lines_created = 0
    for item in eco.get("items", []):
        sku    = str(item.get("sku") or item.get("productSku") or "").strip().upper()
        name_i = str(item.get("name") or item.get("productName") or "").strip()
        qty    = float(item.get("quantity") or 1)
        price  = float(item.get("price") or item.get("unitPrice") or 0)

        # Matching : SKU exact → nom exact → nom partiel
        product = prod_by_sku.get(sku)
        if not product and name_i:
            product = prod_by_name.get(name_i.lower())
        if not product and name_i:
            # Recherche partielle : trouver produit dont le nom Odoo est contenu dans le nom Ecwid
            for k, p in prod_by_name.items():
                if k and (k in name_i.lower() or name_i.lower() in k):
                    product = p
                    break
        if not product:
            line_vals = {"order_id": order_id, "name": name_i or sku or "Article Ecwid",
                         "product_uom_qty": qty, "price_unit": price}
        else:
            uom_id = product["uom_id"][0] if isinstance(product.get("uom_id"), list) else False
            line_vals = {"order_id": order_id, "product_id": product["id"],
                         "name": name_i or product["name"],
                         "product_uom_qty": qty, "price_unit": price}
            if uom_id:
                line_vals["product_uom"] = uom_id
        try:
            odoo_execute("sale.order.line", "create", [line_vals])
            lines_created += 1
        except Exception:
            pass

    for surcharge in (eco.get("customSurcharges") or []):
        amount = surcharge.get("total") or surcharge.get("value") or 0
        if amount:
            try:
                odoo_execute("sale.order.line", "create", [{
                    "order_id": order_id, "product_id": 1917,
                    "name": surcharge.get("description") or "Supplement",
                    "product_uom_qty": 1, "price_unit": amount,
                }])
                lines_created += 1
            except Exception:
                pass

    try:
        odoo_execute("sale.order", "action_confirm", [[order_id]])
    except Exception:
        pass

    return {"status": "created", "fd": fd_number, "lines": lines_created}


@app.post("/webhook/ecwid")
async def webhook_ecwid(request: Request):
    """
    Webhook Ecwid — appelé automatiquement par Ecwid à chaque événement.
    Enregistrer l'URL dans Ecwid : Paramètres → API → Webhooks
    URL : https://mistercochon-backend.onrender.com/webhook/ecwid

    Traite : order.created, order.updated (si payé)
    """
    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored", "reason": "payload non JSON"}

    event_type = payload.get("eventType", "")
    entity_id  = str(payload.get("entityId", ""))

    if event_type not in ("order.created", "order.updated"):
        return {"status": "ignored", "event": event_type}

    if not entity_id:
        return {"status": "ignored", "reason": "pas d'entityId"}

    # Récupérer la commande depuis Ecwid
    ecwid_data = ecwid_get("/orders", {"orderNumber": entity_id, "limit": 1})
    if not ecwid_data or not ecwid_data.get("items"):
        return {"status": "error", "reason": f"Commande {entity_id} non trouvée dans Ecwid"}

    eco = ecwid_data["items"][0]
    result = _import_one_ecwid_order(entity_id, eco)
    return {"status": "ok", "order": entity_id, "result": result}


# ─── Webhook Stripe → Odoo (paiement carte) ──────────────────────────────────

def _mark_order_paid_odoo(ecwid_order_ref: str, stripe_payment_id: str):
    """Cherche la commande Odoo par ref Ecwid et enregistre le paiement."""
    log = []
    orders = odoo_execute("sale.order", "search_read",
        [["|", ["client_order_ref", "=", ecwid_order_ref], ["name", "=", ecwid_order_ref]]],
        {"fields": ["id", "name", "amount_total", "invoice_ids", "partner_id", "state"], "limit": 1}
    )
    if not orders:
        return {"found": False, "ref": ecwid_order_ref}

    order = orders[0]
    order_id = order["id"]
    log.append(f"order found: {order['name']} state={order['state']}")

    # Confirmer la commande si encore en brouillon
    if order["state"] in ("draft", "sent"):
        try:
            odoo_execute("sale.order", "action_confirm", [[order_id]])
            log.append("order confirmed")
        except Exception as e:
            log.append(f"confirm error: {e}")

    # Chercher une facture existante ou en créer une directement
    invoice_ids = order.get("invoice_ids") or []
    if not invoice_ids:
        try:
            lines = odoo_execute("sale.order.line", "search_read",
                [[["order_id", "=", order_id], ["product_id", "!=", False]]],
                {"fields": ["product_id", "product_uom_qty", "price_unit", "name", "discount"]}
            )
            inv_lines = []
            for l in lines:
                pid = l["product_id"]
                if isinstance(pid, list):
                    pid = pid[0]
                inv_lines.append((0, 0, {
                    "product_id": pid,
                    "quantity": l["product_uom_qty"],
                    "price_unit": l["price_unit"],
                    "name": l["name"],
                    "discount": l.get("discount", 0),
                }))
            partner_id = order["partner_id"]
            if isinstance(partner_id, list):
                partner_id = partner_id[0]
            inv_id = odoo_execute("account.move", "create", [{
                "move_type": "out_invoice",
                "partner_id": partner_id,
                "invoice_origin": order["name"],
                "invoice_line_ids": inv_lines,
            }])
            if isinstance(inv_id, list):
                inv_id = inv_id[0]
            invoice_ids = [inv_id]
            log.append(f"invoice created directly: {inv_id}")
        except Exception as e:
            log.append(f"invoice direct create error: {e}")

    # Re-fetch au cas où
    if not invoice_ids:
        try:
            fresh = odoo_execute("sale.order", "read", [[order_id]], {"fields": ["invoice_ids"]})[0]
            invoice_ids = fresh.get("invoice_ids") or []
            log.append(f"invoice_ids re-fetched: {invoice_ids}")
        except Exception as e:
            log.append(f"re-fetch error: {e}")

    if invoice_ids:
        inv_id = invoice_ids[0]
        # Confirmer la facture (passer de draft à posted)
        try:
            inv_state = odoo_execute("account.move", "read", [[inv_id]], {"fields": ["state"]})[0]["state"]
            if inv_state == "draft":
                odoo_execute("account.move", "action_post", [[inv_id]])
                log.append("invoice posted")
            else:
                log.append(f"invoice already {inv_state}")
        except Exception as e:
            log.append(f"post error: {e}")

        # Enregistrer le paiement via le wizard
        try:
            journals = odoo_execute("account.journal", "search_read",
                [[["type", "=", "bank"]]],
                {"fields": ["id", "name"], "limit": 1}
            )
            if journals:
                journal_id = journals[0]["id"]
                ctx = {"active_model": "account.move", "active_ids": [inv_id]}
                wizard_id = odoo_execute("account.payment.register", "create",
                    [{"journal_id": journal_id}],
                    {"context": ctx})
                if isinstance(wizard_id, list):
                    wizard_id = wizard_id[0]
                odoo_execute("account.payment.register", "action_create_payments",
                    [[wizard_id]],
                    {"context": ctx})
                log.append(f"payment registered via wizard {wizard_id}")
        except Exception as e:
            log.append(f"payment error: {e}")
    else:
        log.append("no invoice found — cannot register payment")

    # Note interne
    try:
        odoo_execute("sale.order", "message_post", [[order_id]], {
            "body": f"Paiement PromptPay Stripe confirmé : {stripe_payment_id}",
            "message_type": "comment",
            "subtype_xmlid": "mail.mt_note",
        })
    except Exception as e:
        log.append(f"note error: {e}")

    return {"found": True, "order": order["name"], "invoiced": bool(invoice_ids), "log": log}


@app.post("/webhook/stripe")
async def webhook_stripe(request: Request):
    """Unified Stripe webhook — handles Ecwid orders AND LINE retail payments."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Signature verification
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = _stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except Exception:
            return {"status": "error", "reason": "signature invalide"}
    else:
        try:
            event = json.loads(payload)
        except Exception:
            return {"status": "error", "reason": "JSON invalide"}

    event_type = event.get("type", "")
    data_obj   = event.get("data", {}).get("object", {})
    metadata   = data_obj.get("metadata") or {}
    stripe_id  = data_obj.get("id", "")

    if event_type not in ("payment_intent.succeeded", "checkout.session.completed",
                          "charge.succeeded"):
        return {"status": "ignored", "event": event_type}

    # ── LINE Retail payment ────────────────────────────────────────────────────
    line_user_id = metadata.get("line_user_id", "")
    if line_user_id:
        def _confirm_retail(meta: dict, cart: list):
            user_id   = meta.get("line_user_id", "")
            partner_id = int(meta.get("partner_id", 0) or 0)
            if not cart:
                try:
                    cart = json.loads(meta.get("cart", "[]"))
                except Exception:
                    cart = []
            if not cart:
                cart = _retail_sessions.get(user_id, {}).get("stripe_cart", [])
            if not cart or not partner_id:
                return
            lines_vals = [(0, 0, {"product_id": i["pid"],
                "product_uom_qty": i["qty"], "price_unit": i["price"]}) for i in cart]
            try:
                intent_id = meta.get("stripe_intent_id", "") or obj.get("id", "")
                note = f"LINE Retail Stripe — {intent_id or user_id}"
                # Anti-doublon: ne pas créer si cet intent existe déjà
                existing = odoo_execute("sale.order", "search_read",
                    [[["note", "=", note]]],
                    {"fields": ["name"], "limit": 1})
                if existing:
                    order_name = existing[0]["name"]
                else:
                    order_id = odoo_execute("sale.order", "create", [{
                        "partner_id": partner_id, "order_line": lines_vals,
                        "note": note}])
                    odoo_execute("sale.order", "action_confirm", [[order_id]])
                    order_name = odoo_execute("sale.order", "read",
                        [[order_id]], {"fields": ["name"]})[0]["name"]
            except Exception:
                order_name = "—"
            sess = _retail_sessions.get(user_id, {})
            _retail_sessions[user_id] = {k: v for k, v in sess.items()
                if k not in ("stripe_cart", "stripe_session_id", "stripe_intent_id")}
            if user_id:
                retail_push(user_id, [line_text(
                    f"✅ ชำระเงินสำเร็จ!\nPayment confirmed!\n\nคำสั่งซื้อ: {order_name}\nขอบคุณครับ 🐷"
                )])

        if event_type == "checkout.session.completed":
            sess_cart = _retail_sessions.get(line_user_id, {}).get("stripe_cart", [])
            _confirm_retail(metadata, sess_cart)
        else:
            _confirm_retail(metadata, [])
        return {"status": "ok", "source": "line_retail"}

    # ── Ecwid / B2B order ─────────────────────────────────────────────────────
    ecwid_ref = (
        metadata.get("ecwid_order_id") or
        metadata.get("order_id") or
        metadata.get("orderNumber") or
        data_obj.get("description") or ""
    )
    if not ecwid_ref:
        return {"status": "ignored", "reason": "no order ref", "stripe_id": stripe_id}

    # Use the Odoo-specific ref if stored (e.g. "ECWID-FDFHNY6")
    odoo_ref = metadata.get("ecwid_odoo_ref") or str(ecwid_ref)
    result = _mark_order_paid_odoo(odoo_ref, stripe_id)
    ecwid_result = {}
    try:
        ecwid_put(f"/orders/{ecwid_ref}", {"paymentStatus": "PAID"})
        ecwid_result = {"ecwid_updated": True}
    except Exception as e:
        ecwid_result = {"ecwid_updated": False, "error": str(e)}

    return {"status": "ok", "source": "ecwid", "event": event_type,
            "ecwid_ref": ecwid_ref, "result": result, "ecwid": ecwid_result}


# ─── Ecwid PromptPay payment page ────────────────────────────────────────────

@app.get("/pay/promptpay")
async def pay_ecwid_promptpay(request: Request, order_id: str = "", confirmed: str = ""):
    """Render a PromptPay QR payment page for an Ecwid order.
    Without order_id: show confirmation step, then phone lookup form.
    With order_id: fetch order from Ecwid and show QR code."""
    from fastapi.responses import HTMLResponse as _HR

    # No order_id → step 1: confirm order placed, step 2: phone lookup
    if not order_id:
        # Step 1: confirmation page
        if not confirmed:
            return _HR("""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PromptPay — Mister Cochon</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #1a1a1a; color: #fff;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 24px; }
  .card { background: #fff; color: #222; border-radius: 16px; padding: 40px 28px;
          max-width: 400px; width: 100%; text-align: center; box-shadow: 0 8px 32px rgba(0,0,0,.4); }
  .brand { color: #6B0000; font-size: 22px; font-weight: 900; letter-spacing: 3px;
           text-transform: uppercase; margin-bottom: 28px; margin-top: 8px; }
  h2 { font-size: 20px; margin-bottom: 12px; color: #111; }
  p  { color: #666; font-size: 14px; line-height: 1.7; margin-bottom: 28px; }
  .btn-yes { display:block; width:100%; padding:14px; background:#6B0000; color:#fff; border:none;
             border-radius:10px; font-size:16px; font-weight:700; cursor:pointer; text-decoration:none;
             margin-bottom:12px; }
  .btn-yes:hover { background:#8B0000; }
  .btn-no  { display:block; width:100%; padding:14px; background:#f0f0f0; color:#555; border:none;
             border-radius:10px; font-size:15px; font-weight:600; cursor:pointer; text-decoration:none; }
  .btn-no:hover { background:#e0e0e0; }
</style>
</head>
<body>
<div class="card">
  <div class="brand">Mister Cochon</div>
  <h2>Payer par PromptPay</h2>
  <p>Avez-vous bien cliqué sur<br>
     <strong>"Place Order"</strong> sur la page précédente ?<br><br>
     กดปุ่ม <strong>"Place Order"</strong> แล้วหรือยัง?<br><br>
     Did you click <strong>"Place Order"</strong> first?</p>
  <a class="btn-yes" href="/pay/promptpay?confirmed=1">✅ Oui, ma commande est passée →</a>
  <a class="btn-no"  href="javascript:history.back()">← Non, retour</a>
</div>
</body>
</html>""")

        # Step 2: phone number lookup form
        return _HR("""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PromptPay — Mister Cochon</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #1a1a1a; color: #fff;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 24px; }
  .card { background: #fff; color: #222; border-radius: 16px; padding: 40px 28px;
          max-width: 400px; width: 100%; text-align: center; box-shadow: 0 8px 32px rgba(0,0,0,.4); }
  .brand { color: #6B0000; font-size: 22px; font-weight: 900; letter-spacing: 3px;
           text-transform: uppercase; margin-bottom: 28px; margin-top: 8px; }
  h2 { font-size: 20px; margin-bottom: 8px; color: #111; }
  p  { color: #666; font-size: 14px; line-height: 1.6; margin-bottom: 20px; }
  input { width: 100%; padding: 14px 16px; border: 2px solid #ddd; border-radius: 10px;
          font-size: 20px; text-align: center; letter-spacing: 2px; margin-bottom: 16px; outline: none; }
  input:focus { border-color: #6B0000; }
  button { width: 100%; padding: 14px; background: #6B0000; color: #fff; border: none;
           border-radius: 10px; font-size: 16px; font-weight: 700; cursor: pointer; }
  button:hover { background: #8B0000; }
  #msg { margin-top: 14px; font-size: 13px; color: #c00; min-height: 20px; }
</style>
</head>
<body>
<div class="card">
  <div class="brand">Mister Cochon</div>
  <h2>Payer par PromptPay</h2>
  <p>Entrez votre numéro de téléphone<br>
     กรอกหมายเลขโทรศัพท์ของคุณ<br>
     Enter your phone number</p>
  <form onsubmit="go(event)">
    <input type="tel" id="ph" placeholder="0812345678" autofocus inputmode="tel">
    <button type="submit" id="btn">Trouver ma commande →</button>
  </form>
  <div id="msg"></div>
</div>
<script>
async function go(e) {
  e.preventDefault();
  const phone = document.getElementById('ph').value.trim();
  if (!phone) return;
  const btn = document.getElementById('btn');
  const msg = document.getElementById('msg');
  btn.textContent = '⏳ Recherche…';
  btn.disabled = true;
  msg.textContent = '';
  try {
    const r = await fetch('/pay/lookup?phone=' + encodeURIComponent(phone));
    const d = await r.json();
    if (d.order_id) {
      window.location.href = '/pay/promptpay?order_id=' + d.order_id;
    } else {
      const isNotFound = !d.order_id;
      msg.innerHTML = isNotFound
        ? '<strong>Aucune commande en attente trouvée.</strong><br>'
          + 'Avez-vous bien cliqué sur <em>"Place Order"</em> avant ?<br>'
          + 'Please place your order first, then try again.<br>'
          + '<a href="javascript:history.back()" style="display:inline-block;margin-top:10px;padding:8px 20px;background:#6B0000;color:#fff;border-radius:6px;text-decoration:none;font-weight:700;">← Retour / Go back</a>'
        : (d.error || 'Erreur inconnue');
      btn.textContent = 'Réessayer →';
      btn.disabled = false;
    }
  } catch(err) {
    msg.textContent = 'Erreur réseau, réessayez.';
    btn.textContent = 'Trouver ma commande →';
    btn.disabled = false;
  }
}
</script>
</body>
</html>""")

    if not _stripe:
        return _HR("<h2 style='font-family:sans-serif;padding:40px;color:red'>Stripe non configuré</h2>", status_code=500)

    order = ecwid_get(f"/orders/{order_id}")
    if not order:
        return _HR(f"""<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Commande introuvable</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #1a1a1a;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
  .card {{ background: #fff; border-radius: 16px; padding: 40px 28px; max-width: 380px;
           text-align: center; box-shadow: 0 8px 32px rgba(0,0,0,.4); }}
  h2 {{ color: #c00; margin-bottom: 12px; }}
  p  {{ color: #555; margin-bottom: 24px; }}
  a  {{ display: inline-block; padding: 12px 24px; background: #6B0000; color: #fff;
        border-radius: 8px; text-decoration: none; font-weight: 700; }}
</style></head>
<body><div class="card">
  <div style="font-size:48px;margin-bottom:12px">❌</div>
  <h2>Commande introuvable</h2>
  <p>Numéro «{order_id}» introuvable.<br>Vérifiez et réessayez.</p>
  <a href="/pay/promptpay">← Réessayer</a>
</div></body></html>""", status_code=404)

    total     = float(order.get("total", 0))
    customer_email = order.get("email") or f"order{order_id}@mistercochon.com"
    order_num = order.get("orderNumber") or order_id
    # Build Odoo reference: Ecwid stores vendorOrderNumber as "ECWID-XXXXX" in client_order_ref
    vendor_num = order.get("vendorOrderNumber") or order.get("referenceTransactionId") or ""
    odoo_ref = f"ECWID-{vendor_num}" if vendor_num else str(order_id)

    try:
        intent = _stripe.PaymentIntent.create(
            amount=int(round(total * 100)),
            currency="thb",
            payment_method_types=["promptpay"],
            metadata={
                "ecwid_order_id": str(order_id),
                "ecwid_odoo_ref": odoo_ref,
            },
        )
        intent = _stripe.PaymentIntent.confirm(
            intent["id"],
            payment_method_data={
                "type": "promptpay",
                "billing_details": {"email": customer_email},
            },
            return_url=f"{RENDER_URL}/pay/success?order_id={order_id}",
        )
    except Exception as e:
        return _HR(f"<h2 style='font-family:sans-serif;padding:40px;color:red'>Erreur: {e}</h2>", status_code=500)

    qr_data  = (intent.get("next_action") or {}).get("promptpay_display_qr_code") or {}
    qr_png   = qr_data.get("image_url_png", "")
    intent_id = intent.get("id", "")

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PromptPay — Mister Cochon</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #1a1a1a; color: #fff;
         display: flex; flex-direction: column; align-items: center;
         justify-content: center; min-height: 100vh; padding: 24px; }}
  .card {{ background: #fff; color: #222; border-radius: 16px; padding: 32px 24px;
           max-width: 380px; width: 100%; text-align: center; box-shadow: 0 8px 32px rgba(0,0,0,.4); }}
  .logo {{ font-size: 42px; margin-bottom: 4px; }}
  .brand {{ color: #6B0000; font-size: 13px; font-weight: 700; letter-spacing: 2px;
            text-transform: uppercase; margin-bottom: 20px; }}
  .amount {{ font-size: 36px; font-weight: 700; color: #111; margin-bottom: 4px; }}
  .order  {{ font-size: 13px; color: #888; margin-bottom: 20px; }}
  .qr {{ width: 240px; height: 240px; border: 2px solid #eee; border-radius: 12px;
         object-fit: contain; margin-bottom: 16px; }}
  .instr {{ font-size: 13px; color: #555; line-height: 1.6; margin-bottom: 20px; }}
  .status {{ padding: 10px 20px; border-radius: 20px; font-size: 13px; font-weight: 600;
             background: #FFF3CD; color: #856404; display: inline-block; }}
  .status.ok {{ background: #D1FAE5; color: #065F46; }}
  .timer {{ font-size: 11px; color: #aaa; margin-top: 16px; }}
</style>
</head>
<body>
<div class="card">
  <div class="logo">🐷</div>
  <div class="brand">Mister Cochon</div>
  <div class="amount">฿{total:,.2f}</div>
  <div class="order">คำสั่งซื้อ #{order_num} / Order #{order_num}</div>
  {"<img class='qr' src='" + qr_png + "' alt='PromptPay QR'>" if qr_png else "<p style='color:red'>QR code unavailable</p>"}
  <div class="instr">
    เปิดแอปธนาคารของคุณ → สแกน QR → ชำระเงิน<br>
    Open your bank app → Scan QR → Pay
  </div>
  <div class="status" id="st">⏳ รอการชำระเงิน / Waiting for payment…</div>
  <div class="timer" id="timer"></div>
</div>
<script>
const INTENT = "{intent_id}";
const ORDER  = "{order_id}";
let elapsed = 0;
const iv = setInterval(async () => {{
  elapsed += 5;
  const m = String(Math.floor(elapsed/60)).padStart(2,'0');
  const s = String(elapsed%60).padStart(2,'0');
  document.getElementById('timer').textContent = 'Temps écoulé: ' + m + ':' + s;
  try {{
    const r = await fetch('/pay/promptpay/status?intent_id=' + INTENT);
    const d = await r.json();
    if (d.status === 'succeeded') {{
      clearInterval(iv);
      const el = document.getElementById('st');
      el.textContent = '✅ ชำระเงินสำเร็จ! / Payment successful!';
      el.className = 'status ok';
      setTimeout(() => window.location.href = '/pay/success?order_id=' + ORDER, 2500);
    }}
  }} catch(e) {{}}
}}, 5000);
</script>
</body>
</html>"""
    return _HR(html)


@app.get("/pay/lookup")
async def pay_lookup(phone: str = "", email: str = ""):
    """Find the most recent AWAITING_PAYMENT PromptPay order by phone or email."""
    if not phone and not email:
        return {"error": "phone or email required"}

    # Normalize phone: keep digits only for comparison
    def norm_phone(p: str) -> str:
        return re.sub(r"\D", "", p or "")

    phone_digits = norm_phone(phone)

    import time as _time
    # Only look at orders placed in the last 24 hours to avoid matching old pending orders
    since_ts = int(_time.time()) - 86400

    # Fetch recent pending orders from Ecwid
    params: dict = {"paymentStatus": "AWAITING_PAYMENT", "limit": 50,
                    "sortBy": "DATE_PLACED_DESC", "createdFrom": since_ts}
    if email:
        params["email"] = email

    orders = ecwid_get("/orders", params=params) or {}
    items  = orders.get("items") or []

    for o in items:
        if phone_digits:
            # Check phone in billing/shipping address
            bill_phone = norm_phone(o.get("billingPerson", {}).get("phone", ""))
            ship_phone = norm_phone(o.get("shippingPerson", {}).get("phone", ""))
            if phone_digits not in (bill_phone, ship_phone):
                # Also try last-digit match (e.g. 0812345678 vs +66812345678)
                if not (bill_phone.endswith(phone_digits[-9:]) or
                        ship_phone.endswith(phone_digits[-9:])):
                    continue
        order_id = o.get("orderNumber") or o.get("id")
        if order_id:
            return {"order_id": str(order_id), "total": o.get("total"), "currency": o.get("currency")}

    return {"error": "Aucune commande en attente trouvée. Vérifiez votre numéro."}


@app.get("/pay/promptpay/status")
async def pay_promptpay_status(intent_id: str):
    """Poll Stripe PaymentIntent status (for AJAX polling on payment page)."""
    if not _stripe:
        return {"status": "error"}
    try:
        intent = _stripe.PaymentIntent.retrieve(intent_id)
        return {"status": intent.get("status", "unknown")}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/pay/success")
async def pay_success_page(order_id: str = ""):
    from fastapi.responses import HTMLResponse as _HR
    return _HR(f"""<!DOCTYPE html>
<html lang="th">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paiement confirmé</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #1a1a1a; color: #fff;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
  .card {{ background: #fff; color: #222; border-radius: 16px; padding: 40px 32px;
           max-width: 360px; text-align: center; box-shadow: 0 8px 32px rgba(0,0,0,.4); }}
  h1 {{ color: #065F46; font-size: 28px; margin-bottom: 12px; }}
  p  {{ color: #555; line-height: 1.7; }}
  .redirect {{ font-size: 12px; color: #aaa; margin-top: 16px; }}
  .btn-store {{ display:inline-block;margin-top:16px;padding:10px 24px;background:#6B0000;
               color:#fff;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px; }}
</style>
</head>
<body>
<div class="card">
  <div style="font-size:64px;margin-bottom:16px">✅</div>
  <h1>ชำระเงินสำเร็จ!</h1>
  <p>Payment Successful!<br>
  {"คำสั่งซื้อ #" + str(order_id) + "<br>" if order_id else ""}
  ขอบคุณที่ใช้บริการ Mister Cochon<br>
  Thank you for your order!</p>
  <a class="btn-store" href="https://mistercochon.com">Retour à la boutique →</a>
  <p class="redirect" id="cd">Redirection dans 20 secondes…</p>
</div>
<script>
var s=20,iv=setInterval(function(){{
  s--;document.getElementById('cd').textContent='Redirection dans '+s+' secondes…';
  if(s<=0){{clearInterval(iv);window.location.href='https://mistercochon.com';}}
}},1000);
</script>
</body>
</html>""")


# ─── Auto-cancel unpaid PromptPay orders ─────────────────────────────────────

@app.get("/admin/cancel-old-orders")
def cancel_old_orders(secret: str = ""):
    """Cancel AWAITING_PAYMENT PromptPay orders older than 2 hours in Ecwid + Odoo.
    Call via cron every 30 min: /admin/cancel-old-orders?secret=XXX"""
    import time as _time
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or secret != admin_secret:
        return {"status": "error", "error": "Invalid secret"}

    now = int(_time.time())
    two_hours_ago = now - 7200

    # Fetch AWAITING_PAYMENT orders created before 2h ago
    orders_data = ecwid_get("/orders", params={
        "paymentStatus": "AWAITING_PAYMENT",
        "limit": 100,
        "sortBy": "DATE_PLACED_DESC",
        "createdTo": two_hours_ago,
    }) or {}

    items = orders_data.get("items") or []

    # Filter PromptPay orders only
    promptpay_items = [
        o for o in items
        if "promptpay" in (o.get("paymentMethod") or "").lower()
        or "promptpay" in (o.get("paymentMethodTitle") or "").lower()
    ]

    cancelled = []
    errors = []

    for order in promptpay_items:
        ecwid_num = order.get("orderNumber") or order.get("id")
        vendor_num = order.get("vendorOrderNumber") or order.get("referenceTransactionId") or ""
        odoo_ref = f"ECWID-{vendor_num}" if vendor_num else str(ecwid_num)

        # 1. Cancel in Ecwid
        try:
            ecwid_put(f"/orders/{ecwid_num}", {"paymentStatus": "CANCELLED"})
            ecwid_ok = True
        except Exception as e:
            ecwid_ok = False
            errors.append({"order": ecwid_num, "step": "ecwid", "error": str(e)})

        # 2. Cancel in Odoo
        odoo_ok = False
        try:
            odoo_orders = odoo_execute("sale.order", "search_read",
                [[["client_order_ref", "=", odoo_ref], ["state", "not in", ["cancel", "done"]]]],
                {"fields": ["id", "name", "state"], "limit": 1}
            )
            if odoo_orders:
                odoo_id = odoo_orders[0]["id"]
                odoo_execute("sale.order", "action_cancel", [[odoo_id]])
                odoo_ok = True
                try:
                    odoo_execute("sale.order", "message_post", [[odoo_id]], {
                        "body": "Commande annulée automatiquement : PromptPay non payé dans les 2h.",
                        "message_type": "comment",
                        "subtype_xmlid": "mail.mt_note",
                    })
                except Exception:
                    pass
        except Exception as e:
            errors.append({"order": ecwid_num, "step": "odoo", "error": str(e)})

        cancelled.append({
            "ecwid_order": ecwid_num,
            "odoo_ref": odoo_ref,
            "ecwid_cancelled": ecwid_ok,
            "odoo_cancelled": odoo_ok,
        })

    return {
        "status": "ok",
        "checked": len(promptpay_items),
        "cancelled": cancelled,
        "errors": errors,
    }


# ─── LINE Rich Menu setup ────────────────────────────────────────────────────

@app.get("/debug/mark-paid")
def debug_mark_paid(secret: str = "", odoo_ref: str = ""):
    """Test Odoo payment marking without real Stripe payment.
    Usage: /debug/mark-paid?secret=XXX&odoo_ref=ECWID-FDFHNY6"""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or secret != admin_secret:
        return {"status": "error", "error": "Invalid secret"}
    if not odoo_ref:
        return {"status": "error", "error": "odoo_ref required (ex: ECWID-FDFHNY6)"}
    result = _mark_order_paid_odoo(odoo_ref, "TEST-STRIPE-ID")
    return {"status": "ok", "result": result}


@app.get("/setup-richmenu")
def setup_richmenu(secret: str = ""):
    """Create/replace the LINE Rich Menu. Call once: /setup-richmenu?secret=XXX"""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or secret != admin_secret:
        return {"status": "error", "error": "Invalid secret"}
    try:
        from setup_richmenu import make_image, delete_existing, create_and_activate
        img = make_image()
        delete_existing(LINE_CHANNEL_ACCESS_TOKEN)
        mid = create_and_activate(LINE_CHANNEL_ACCESS_TOKEN, img)
        return {"status": "ok", "richMenuId": mid}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/setup-richmenu-retail")
def setup_richmenu_retail(secret: str = ""):
    """Create/replace the Mister Cochon retail Rich Menu. Call once: /setup-richmenu-retail?secret=XXX"""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or secret != admin_secret:
        return {"status": "error", "error": "Invalid secret"}
    try:
        from setup_richmenu_retail import make_image, delete_existing, create_and_activate
        retail_token = os.getenv("LINE_RETAIL_ACCESS_TOKEN", "")
        img = make_image()
        delete_existing(retail_token)
        mid = create_and_activate(retail_token, img)
        return {"status": "ok", "richMenuId": mid}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/debug-ecwid-order")
def debug_ecwid_order(order_id: str = "", secret: str = ""):
    """Debug: show raw Ecwid order fields to check vendorOrderNumber etc."""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or secret != admin_secret:
        return {"status": "error", "error": "Invalid secret"}
    order = ecwid_get(f"/orders/{order_id}")
    if not order:
        # Try searching by orderNumber
        res = ecwid_get("/orders", {"orderNumber": order_id, "limit": 1})
        if res and res.get("items"):
            order = res["items"][0]
    if not order:
        return {"error": "Not found"}
    return {
        "id": order.get("id"),
        "orderNumber": order.get("orderNumber"),
        "vendorOrderNumber": order.get("vendorOrderNumber"),
        "referenceTransactionId": order.get("referenceTransactionId"),
        "paymentStatus": order.get("paymentStatus"),
        "total": order.get("total"),
    }


@app.get("/admin/sync-gammes")
def admin_sync_gammes(secret: str = ""):
    """Resync les tags Gamme Tradition / Gamme Premium selon les SKUs actuels.
    Rejouer après toute correction de SKU dans Odoo.
    - SKU xxxx23xx → LINE-Gamme Premium (retire Tradition)
    - Autres SKUs PRO → LINE-Gamme Tradition (retire Premium)
    Usage: /admin/sync-gammes?secret=XXX"""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or secret != admin_secret:
        return {"status": "error", "error": "Invalid secret"}

    tag_rows = odoo_execute("product.tag", "search_read",
        [[["name", "in", ["LINE-Gamme Tradition", "LINE-Gamme Premium"]]]],
        {"fields": ["id", "name"]})
    tag_map = {t["name"]: t["id"] for t in (tag_rows or [])}
    tid = tag_map.get("LINE-Gamme Tradition")
    pid = tag_map.get("LINE-Gamme Premium")
    if not tid or not pid:
        return {"status": "error", "error": f"Tags manquants: {tag_map}"}

    cat_ids = odoo_execute("product.category", "search", [[["name", "=", "PRO"]]])
    all_pro = odoo_execute("product.template", "search_read",
        [[["categ_id", "in", cat_ids], ["default_code", "!=", False]]],
        {"fields": ["id", "default_code"]})

    # Exclure les produits BELGO (SKU commençant par BELGO) — tag LINE-BELGO géré séparément
    premium_ids  = [p["id"] for p in (all_pro or [])
                    if re.match(r'^.{4}23.{2}$', (p.get("default_code") or "").strip())
                    and not (p.get("default_code") or "").upper().startswith("BELGO")]
    tradition_ids = [p["id"] for p in (all_pro or [])
                     if p["id"] not in premium_ids
                     and not (p.get("default_code") or "").upper().startswith("BELGO")]

    if tradition_ids:
        odoo_execute("product.template", "write", [tradition_ids,
            {"product_tag_ids": [(3, pid), (4, tid)]}])
    if premium_ids:
        odoo_execute("product.template", "write", [premium_ids,
            {"product_tag_ids": [(3, tid), (4, pid)]}])

    return {
        "status": "ok",
        "tradition": len(tradition_ids),
        "premium": len(premium_ids),
        "skus_premium": sorted(p["default_code"] for p in (all_pro or []) if p["id"] in premium_ids)
    }


@app.get("/debug-reorder")
def debug_reorder(secret: str = "", code: str = ""):
    """Debug reorder: show what the bot finds for a client. /debug-reorder?secret=fd2026&code=62101"""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or secret != admin_secret:
        return {"status": "error", "error": "Invalid secret"}
    # Find partner by code (last 5 digits of VAT)
    partners = odoo_execute("res.partner", "search_read",
        [[["vat", "!=", False], ["customer_rank", ">", 0]]],
        {"fields": ["id", "name", "vat"], "limit": 2000}
    )
    partner = None
    for p in (partners or []):
        vat = re.sub(r"\D", "", p.get("vat") or "")
        if vat.endswith(code[-5:] if len(code) >= 5 else code):
            partner = p
            break
    if not partner:
        return {"status": "error", "error": f"Partner not found for code {code}"}
    orders = odoo_execute("sale.order", "search_read",
        [[["partner_id", "child_of", partner["id"]], ["state", "in", ["sale", "done"]]]],
        {"fields": ["id", "name", "state", "date_order"], "limit": 5, "order": "date_order desc"}
    )
    if not orders:
        return {"partner": partner["name"], "orders": [], "note": "No confirmed orders found"}
    oid_list = [o["id"] for o in orders]
    lines = odoo_execute("sale.order.line", "search_read",
        [[["order_id", "in", oid_list]]],
        {"fields": ["product_id", "product_uom_qty", "order_id"], "limit": 200}
    )
    prod_ids = list({ln["product_id"][0] for ln in (lines or []) if ln.get("product_id")})
    domain = [["id", "in", prod_ids], ["active", "=", True],
              ["name", "not ilike", "frozen"],
              ["name", "not ilike", "livraison"],
              ["name", "not ilike", "delivery"]]
    prods = odoo_execute("product.product", "search_read",
        [domain],
        {"fields": ["id", "name", "default_code", "list_price", "active"],
         "limit": 100, "context": {"lang": "en_US"}}
    )
    # Also check without filters to compare
    prods_raw = odoo_execute("product.product", "search_read",
        [[["id", "in", prod_ids]]],
        {"fields": ["id", "name", "default_code", "list_price", "active"],
         "limit": 100, "context": {"lang": "en_US"}}
    )
    return {
        "partner": partner["name"],
        "partner_id": partner["id"],
        "orders": [{"id": o["id"], "name": o["name"], "state": o["state"]} for o in orders],
        "order_line_count": len(lines or []),
        "unique_product_ids": prod_ids,
        "products_after_filter": [{"id": p["id"], "name": p["name"], "active": p["active"]} for p in (prods or [])],
        "products_raw": [{"id": p["id"], "name": p["name"], "active": p["active"], "list_price": p["list_price"]} for p in (prods_raw or [])],
    }


# ─── Utilitaires import Ecwid ────────────────────────────────────────────────

@app.get("/debug-ecwid-order/{order_num}")
def debug_ecwid_order(order_num: str):
    """Affiche la structure brute d'une commande Ecwid (pour debug)."""
    data = ecwid_get("/orders", {"orderNumber": order_num, "limit": 1})
    if not data or not data.get("items"):
        return {"status": "not_found"}
    eco = data["items"][0]
    items = eco.get("items", [])
    return {
        "orderNumber": eco.get("orderNumber"),
        "createDate": eco.get("createDate"),
        "createDate_parsed": _parse_ecwid_date(eco.get("createDate")),
        "paymentStatus": eco.get("paymentStatus"),
        "email": eco.get("email"),
        "total": eco.get("total"),
        "items_count": len(items),
        "items_sample": [
            {"sku": i.get("sku"), "productSku": i.get("productSku"),
             "name": i.get("name"), "quantity": i.get("quantity"),
             "price": i.get("price")}
            for i in items[:3]
        ],
    }


@app.post("/create-ecwid-products")
async def create_ecwid_products(request: Request):
    """
    Create new products in Ecwid.
    Body: list of {name, sku, price, category}
    """
    items = await request.json()
    if not isinstance(items, list):
        return {"error": "expected list"}

    cat_data = ecwid_get("/categories").get("items", [])
    cat_map  = {c["name"].lower(): c["id"] for c in cat_data}

    created, skipped, errors = 0, 0, []
    for row in items:
        sku  = (row.get("sku") or "").strip()
        name = (row.get("name") or "").strip()
        if not sku or not name:
            skipped += 1
            continue

        payload = {
            "name":    name,
            "sku":     sku,
            "price":   float(row.get("price") or 0),
            "enabled": True,
            "weight":  0,
        }
        cat_name = (row.get("category") or "").lower()
        if cat_name:
            cid = cat_map.get(cat_name)
            if not cid:
                for k, v in cat_map.items():
                    if cat_name in k or k in cat_name:
                        cid = v
                        break
            if cid:
                payload["categoryIds"]       = [cid]
                payload["defaultCategoryId"] = cid

        r = requests.post(
            f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/products",
            json=payload,
            headers={"Authorization": f"Bearer {ECWID_TOKEN}", "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code in (200, 201):
            created += 1
        else:
            errors.append({"sku": sku, "status": r.status_code, "body": r.text[:200]})

    return {"status": "ok", "created": created, "skipped": skipped, "errors": errors}


@app.post("/sync-ecwid-products")
async def sync_ecwid_products(request: Request):
    """
    Sync price, category, enabled status for Ecwid products.
    Body: list of {sku, prix, cat_ecwid, delete}
    """
    items = await request.json()
    if not isinstance(items, list):
        return {"error": "expected list"}

    # Load all Ecwid products indexed by SKU
    all_prods, offset = [], 0
    while True:
        data = ecwid_get("/products", {"limit": 100, "offset": offset})
        batch = data.get("items", [])
        all_prods.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    by_sku = {(p.get("sku") or "").strip().upper(): p for p in all_prods if p.get("sku")}

    # Load Ecwid categories
    cat_data = ecwid_get("/categories").get("items", [])
    cat_map  = {c["name"].lower(): c["id"] for c in cat_data}

    updated, disabled, skipped = 0, 0, 0
    for row in items:
        sku = (row.get("sku") or "").strip().upper()
        if not sku or sku not in by_sku:
            skipped += 1
            continue
        p   = by_sku[sku]
        eid = p["id"]

        if row.get("delete"):
            ecwid_put(f"/products/{eid}", {"enabled": False})
            disabled += 1
            continue

        payload = {}
        prix = row.get("prix")
        if prix and abs(float(prix) - (p.get("price") or 0)) > 0.01:
            payload["price"] = float(prix)

        cat_name = (row.get("cat_ecwid") or "").lower()
        if cat_name:
            cid = cat_map.get(cat_name)
            if cid and cid not in (p.get("categoryIds") or []):
                payload["categoryIds"]      = [cid]
                payload["defaultCategoryId"] = cid

        if payload:
            ecwid_put(f"/products/{eid}", payload)
            updated += 1

    return {"status": "ok", "updated": updated, "disabled": disabled, "skipped": skipped}


@app.get("/delete-ecwid-imports")
def delete_ecwid_imports(confirm: str = ""):
    """
    Supprime toutes les commandes Odoo importées depuis Ecwid (client_order_ref numérique).
    Passer ?confirm=yes pour exécuter.
    """
    if confirm != "yes":
        orders = odoo_execute("sale.order", "search_read",
            [[["name", "like", "S"]]],
            {"fields": ["id", "name", "state"], "limit": 500}
        )
        ecwid_orders = [o for o in orders if o["name"].endswith("S") and "FD" in o["name"]]
        return {
            "status": "preview",
            "to_delete": len(ecwid_orders),
            "sample": [{"id": o["id"], "name": o["name"], "state": o["state"]}
                       for o in ecwid_orders[:10]],
            "action": "Ajouter ?confirm=yes pour supprimer"
        }

    # Chercher toutes les commandes Ecwid par nom (se terminent par 'S')
    orders = odoo_execute("sale.order", "search_read",
        [[["name", "like", "S"]]],
        {"fields": ["id", "name", "state"], "limit": 2000}
    )
    all_ids = [o for o in (orders or []) if o["name"].endswith("S") and "FD" in o["name"]]

    deleted, errors = [], []
    for o in all_ids:
        oid = o["id"]
        name = o["name"]
        try:
            if o["state"] in ("sale", "done"):
                odoo_execute("sale.order", "action_cancel", [[oid]])
            try:
                odoo_execute("sale.order", "action_draft", [[oid]])
            except Exception:
                pass
            odoo_execute("sale.order", "unlink", [[oid]])
            deleted.append(name)
        except Exception as e:
            errors.append({"name": name, "error": str(e)})

    return {
        "status": "ok",
        "found": len(all_ids),
        "deleted": len(deleted),
        "errors": len(errors),
        "errors_detail": errors[:20],
    }


# ─── LINE Messaging API — Bot commandes B2B French Delicatessen ───────────────

LINE_API = "https://api.line.me/v2/bot"
LINE_FALLBACK_IMG = "https://upload.wikimedia.org/wikipedia/commons/thumb/6/65/No-Image-Placeholder.svg/1024px-No-Image-Placeholder.svg.png"


def line_reply(reply_token: str, messages: list):
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        requests.post(f"{LINE_API}/message/reply",
            json={"replyToken": reply_token, "messages": messages},
            headers=headers, timeout=10)
    except Exception:
        pass


def line_text(text: str) -> dict:
    return {"type": "text", "text": text}


def line_quick_reply(text: str, items: list) -> dict:
    """Text message with quick reply buttons. items = [(label, text_or_data), ...]
    If label starts with '__' or text starts with '__', uses postback (hidden from chat).
    """
    def _action(lbl, txt):
        if txt.startswith("__"):
            return {"type": "postback", "label": lbl[:20], "data": txt}
        return {"type": "message", "label": lbl[:20], "text": txt}

    return {
        "type": "text",
        "text": text,
        "quickReply": {
            "items": [
                {"type": "action", "action": _action(lbl, txt)}
                for lbl, txt in items[:13]
            ]
        }
    }


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _line_get_display_name(user_id: str) -> str:
    try:
        r = requests.get(f"{LINE_API}/profile/{user_id}",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            timeout=5)
        return r.json().get("displayName", "")
    except Exception:
        return ""


def _line_get_partner(user_id: str) -> dict | None:
    """Return Odoo partner linked to this LINE user_id, or None."""
    results = odoo_execute("res.partner", "search_read",
        [[["comment", "like", f"line:{user_id}"]]],
        {"fields": ["id", "name", "property_product_pricelist"], "limit": 1}
    )
    return results[0] if results else None


def _dbd_check_digit(digits: str) -> bool:
    """Validate Thai 13-digit company registration number using official check digit formula."""
    if len(digits) != 13 or not digits.isdigit():
        return False
    weights = [13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2]
    total = sum(int(digits[i]) * weights[i] for i in range(12))
    check = (11 - (total % 11)) % 11
    if check >= 10:
        return False
    return str(check) == digits[12]


def _dbd_verify(reg_number: str, company_name: str) -> tuple:
    """
    Validate Thai company registration number (format + check digit).
    DBD has no public real-time API — we validate format only.
    Returns (ok, reason) where reason is "valid"/"invalid_format"/"invalid_check".
    """
    digits = re.sub(r"\D", "", reg_number)
    if len(digits) != 13:
        return False, "invalid_format"
    if not _dbd_check_digit(digits):
        return False, "invalid_check"
    return True, "valid"


def _line_quick_login(code: str, user_id: str) -> tuple:
    """Quick login: last 5 digits of VAT/DBD number (accepts full number too). Returns (ok, partner_name)."""
    digits = re.sub(r"\D", "", code.strip())
    if len(digits) < 5:
        return False, ""
    code = digits[-5:]  # always use last 5
    partners = odoo_execute("res.partner", "search_read",
        [[["vat", "!=", False], ["customer_rank", ">", 0]]],
        {"fields": ["id", "name", "vat", "comment"], "limit": 2000}
    )
    for p in (partners or []):
        vat = re.sub(r"\D", "", p.get("vat") or "")
        if vat.endswith(code):
            comment = p.get("comment") or ""
            marker = f"line:{user_id}"
            if marker not in comment:
                odoo_execute("res.partner", "write",
                    [[p["id"]], {"comment": (comment + "\n" + marker).strip()}])
            return True, p["name"]
    return False, ""


# ── Product helpers ───────────────────────────────────────────────────────────

def _line_get_line_tag_id() -> int | None:
    """Return the id of the 'LINE' tag, or None if it doesn't exist."""
    tags = odoo_execute("product.tag", "search_read",
        [[["name", "=", "LINE"]]],
        {"fields": ["id"], "limit": 1}
    )
    return tags[0]["id"] if tags else None


def _line_extra_tags_for_partner(partner: dict) -> list:
    """Retourne les tag IDs des catalogues privés auxquels ce partenaire a accès.
    Logique : si le nom du tag LINE-XXXXX est contenu dans le nom du partenaire
    (ou vice-versa), le partenaire voit ce catalogue.
    Les tags publics (Gamme Tradition/Premium) sont exclus ici."""
    PUBLIC_TAGS = {"LINE-Gamme Tradition", "LINE-Gamme Premium"}
    name_low = (partner.get("name") or "").lower()
    all_tags = odoo_execute("product.tag", "search_read",
        [[["name", "=ilike", "LINE-%"]]],
        {"fields": ["id", "name"], "limit": 50})
    extra = []
    for t in (all_tags or []):
        if t["name"] in PUBLIC_TAGS:
            continue
        suffix = t["name"][5:].lower()  # retire "LINE-"
        if suffix and suffix in name_low:
            extra.append(t["id"])
    return extra


def _line_get_pro_categories(extra_tags: list | None = None) -> list:
    """Return (id, label) pairs for the LINE B2B bot menu.

    Positive id  → product tag (LINE-* tags in Odoo).
    Negative id  → product category (-categ_id), used as fallback when no
                   LINE-* tags are configured in Odoo.
    extra_tags allows injecting hidden tag ids (e.g. BELGO/Dofann).
    """
    PUBLIC_ORDER = [
        "LINE-Gamme Tradition",
        "LINE-Gamme Premium",
    ]

    sub_tags = odoo_execute("product.tag", "search_read",
        [[["name", "=ilike", "LINE-%"]]],
        {"fields": ["id", "name"], "limit": 50}
    )
    tag_by_name = {t["name"]: t["id"] for t in (sub_tags or [])}

    # Si le client a des tags privés → il voit SEULEMENT ses produits, pas les gammes publiques
    result = []
    if extra_tags:
        for tid in extra_tags:
            t = next((t for t in (sub_tags or []) if t["id"] == tid), None)
            if t:
                result.append((tid, t["name"][5:].strip()))
    for name in PUBLIC_ORDER:
        tid = tag_by_name.get(name)
        if not tid:
            continue
        count = odoo_execute("product.product", "search_count",
            [[["active", "=", True], ["sale_ok", "=", True],
              ["product_tag_ids", "in", [tid]]]]
        )
        if count:
            result.append((tid, name[5:].strip()))

    # Fallback: no LINE-* tags configured → use Odoo product categories
    if not result:
        EXCLUDE = {"all", "tous", "all products", "deliveries", "livraisons",
                   "matieres premieres", "matières premières",
                   "expense", "expenses", "default", "internal"}
        cats = odoo_execute("product.category", "search_read",
            [[]],
            {"fields": ["id", "name"], "limit": 100}
        )
        for c in (cats or []):
            if c["name"].lower() in EXCLUDE:
                continue
            count = odoo_execute("product.product", "search_count",
                [[["active", "=", True], ["sale_ok", "=", True],
                  ["categ_id", "=", c["id"]]]]
            )
            if count:
                result.append((-c["id"], c["name"]))
        result = result[:10]

    return result


def _line_build_cat_flex(cats: list) -> dict:
    """Build a Flex bubble showing category buttons."""
    cat_buttons = []
    for cid, label in cats[:10]:
        cat_buttons.append({
            "type": "button", "style": "secondary", "height": "sm",
            "action": {"type": "postback", "label": label[:40], "data": f"__cat_{cid}"}
        })
    return {
        "type": "flex", "altText": "Select a category",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical",
                "backgroundColor": "#1A3A6B", "paddingAll": "14px",
                "contents": [{"type": "text", "text": "French Delicatessen PRO",
                              "weight": "bold", "color": "#FFFFFF", "size": "md"}]
            },
            "body": {
                "type": "box", "layout": "vertical",
                "paddingAll": "10px", "spacing": "sm",
                "contents": cat_buttons
            }
        }
    }


def _line_get_subcategories_for_tag(tag_id: int) -> list:
    """Return [(categ_id, categ_name)] for Odoo categories that have products with this tag."""
    EXCLUDE = {"all", "tous", "all products", "deliveries", "livraisons",
               "matieres premieres", "matières premières", "expense", "expenses",
               "default", "internal", "pro"}
    prods = odoo_execute("product.product", "search_read",
        [[["active", "=", True], ["sale_ok", "=", True],
          ["product_tag_ids", "in", [tag_id]]]],
        {"fields": ["categ_id"], "limit": 500})
    seen = {}
    for p in (prods or []):
        cid, cname = p["categ_id"]
        if cname.lower() not in EXCLUDE:
            seen[cid] = cname
    return sorted(seen.items(), key=lambda x: x[1])


def _line_build_subcat_flex(tag_id: int, subcats: list, gamme_label: str) -> dict:
    """Build a Flex bubble showing sub-family buttons for a given gamme tag."""
    buttons = []
    for categ_id, label in subcats[:10]:
        buttons.append({
            "type": "button", "style": "secondary", "height": "sm",
            "action": {"type": "postback", "label": label[:40],
                       "data": f"__subcat_{tag_id}_{categ_id}"}
        })
    return {
        "type": "flex", "altText": f"{gamme_label} — Familles",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical",
                "backgroundColor": "#1A3A6B", "paddingAll": "14px",
                "contents": [{"type": "text", "text": gamme_label,
                              "weight": "bold", "color": "#FFFFFF", "size": "md"}]
            },
            "body": {
                "type": "box", "layout": "vertical",
                "paddingAll": "10px", "spacing": "sm",
                "contents": buttons
            }
        }
    }


_ecwid_image_cache: dict = {}
_ecwid_image_cache_ts: float = 0.0

def _line_get_ecwid_images() -> dict:
    """Return {sku_upper: image_url} for the WHOLE Ecwid catalog (paginated).
    Cached for 1 hour on success. On failure, keeps serving the previous
    (stale) cache instead of going empty, and retries again within ~60s
    instead of being stuck without images for the full hour."""
    import time
    global _ecwid_image_cache, _ecwid_image_cache_ts
    now = time.time()
    has_cache = bool(_ecwid_image_cache)
    cache_age = now - _ecwid_image_cache_ts

    if has_cache and cache_age < 3600:
        return _ecwid_image_cache

    img_map = {}
    try:
        for item in ecwid_get_all_products():
            img = item.get("imageUrl") or item.get("thumbnailUrl") or ""
            sku = str(item.get("sku") or "").strip().upper()
            if sku and img:
                img_map[sku] = img
            for combo in item.get("combinations", []):
                vsku = str(combo.get("sku") or "").strip().upper()
                if vsku and img:
                    img_map[vsku] = img
    except Exception:
        img_map = {}

    if img_map:
        _ecwid_image_cache = img_map
        _ecwid_image_cache_ts = now
        return img_map

    # Fetch failed or returned nothing usable this round.
    if has_cache:
        # Serve the stale cache but retry again in ~60s instead of an hour.
        _ecwid_image_cache_ts = now - 3600 + 60
        return _ecwid_image_cache
    _ecwid_image_cache_ts = now - 3600 + 60
    return {}


async def _poll_ecwid_images():
    """Keep the product-image cache warm in the background so LINE
    requests never pay the full Ecwid fetch cost synchronously."""
    while True:
        try:
            _line_get_ecwid_images()
        except Exception as e:
            print(f"[IMG] Erreur: {e}")
        await asyncio.sleep(1800)  # refresh every 30 min, well under the 1h cache TTL


def _line_get_client_price(product_id: int, list_price: float, pricelist) -> float:
    """Get price for a product given partner's pricelist."""
    if not pricelist or pricelist is False:
        return list_price
    pricelist_id = pricelist[0] if isinstance(pricelist, list) else pricelist
    items = odoo_execute("product.pricelist.item", "search_read",
        [[["pricelist_id", "=", pricelist_id],
          ["product_id", "=", product_id],
          ["compute_price", "=", "fixed"]]],
        {"fields": ["fixed_price"], "limit": 1}
    )
    if items:
        return items[0]["fixed_price"]
    return list_price


def _truncate(s: str, n: int) -> str:
    """Truncate to at most n chars, adding an ellipsis when actually cut.
    Never cuts a word in half if a space is reasonably close to the limit."""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    sp = cut.rfind(" ")
    if sp > n * 0.6:  # avoid chopping mid-word when a space is nearby
        cut = cut[:sp]
    return cut.rstrip() + "…"


def _parse_product_variant(name: str):
    """Parse a raw Odoo product name into (base, weight_gr:int|None, variant:str|None).
    Handles 'Taste', 'Flavour' (UK) and 'Flavor' (US) labels."""
    import re as _re
    name = name or ""
    m = _re.match(r"^(.+?)\s*\(weight:\s*(\d+)\s*gr\s*/\s*(?:Taste|Flavour|Flavor):\s*(.+?)\)\s*$", name, _re.I)
    if m:
        return m.group(1).strip(), int(m.group(2)), m.group(3).strip()
    m2 = _re.match(r"^(.+?)\s*\(weight:\s*(\d+)\s*gr\)\s*$", name, _re.I)
    if m2:
        return m2.group(1).strip(), int(m2.group(2)), None
    m3 = _re.match(r"^(.+?)\s*\((?:Taste|Flavour|Flavor):\s*(.+?)\)\s*$", name, _re.I)
    if m3:
        return m3.group(1).strip(), None, m3.group(2).strip()
    return name, None, None


def _shorten_product_name(name: str) -> str:
    """Normalize Odoo product name variants into a compact display form.
    Does NOT hard-truncate: callers should truncate to fit their own UI
    element via _truncate(), since the safe length differs per bubble/field."""
    base, weight, variant = _parse_product_variant(name)
    if weight and variant:
        kg = f"{weight//1000}kg" if weight >= 1000 else f"{weight}g"
        return f"{base} {kg} · {variant}"
    if weight:
        return f"{base} {weight}g"
    if variant:
        return f"{base} · {variant}"
    return base


def _line_build_carousel(products: list, pricelist, page: int = 0,
                          category_name: str = "") -> list:
    """Compact scrollable list: one row per product, tap to open detail."""
    PAGE = 12
    start = page * PAGE
    page_prods = products[start:start + PAGE]
    if not page_prods:
        return [line_text("No products found in this category.")]

    total_pages = ((len(products) - 1) // PAGE) + 1
    header_text = (category_name or "Products") + (f"  {page+1}/{total_pages}" if total_pages > 1 else "")

    rows = []
    for p in page_prods:
        sku   = str(p.get("default_code") or "").strip().upper()
        name  = _shorten_product_name(p.get("name") or "")
        price = _line_get_client_price(p["id"], p.get("list_price", 0), pricelist)
        img_url = _line_get_ecwid_images().get(sku, "")
        img_box = {"type": "image", "url": img_url, "size": "xs", "aspectMode": "cover", "aspectRatio": "1:1", "flex": 2} if img_url else {"type": "filler", "flex": 2}
        rows.append({
                "type": "box", "layout": "horizontal",
                "paddingTop": "8px", "paddingBottom": "8px",
                "paddingStart": "10px", "paddingEnd": "14px",
                "spacing": "md",
                "action": {"type": "postback", "label": _truncate(name, 40),
                            "data": f"__view_{p['id']}_{sku}"},
                "contents": [
                    img_box,
                    {"type": "box", "layout": "vertical", "flex": 7, "justifyContent": "center",
                     "contents": [
                         {"type": "text", "text": _truncate(name, 60), "size": "sm", "color": "#222222", "wrap": True, "maxLines": 2},
                         {"type": "text", "text": f"{price:.0f}฿", "size": "sm", "weight": "bold", "color": "#C8102E"}
                     ]},
                    {"type": "text", "text": ">", "flex": 1, "size": "xl", "color": "#1A3A6B", "align": "end"}
                ]
            })

    nav_buttons = []
    if page > 0:
        nav_buttons.append({
            "type": "button", "style": "secondary", "height": "sm", "flex": 1,
            "action": {"type": "postback", "label": "◀ Prev", "data": f"__page_{page-1}"}
        })
    if start + PAGE < len(products):
        nav_buttons.append({
            "type": "button", "style": "secondary", "height": "sm", "flex": 1,
            "action": {"type": "postback", "label": "Next ▶", "data": f"__page_{page+1}"}
        })

    footer_contents = []
    if nav_buttons:
        footer_contents.append({"type": "box", "layout": "horizontal",
                                 "spacing": "sm", "contents": nav_buttons})
    footer_contents += [
        {"type": "button", "style": "primary", "height": "sm", "color": "#1A3A6B",
         "action": {"type": "message", "label": "My cart", "text": "cart"}},
        {"type": "button", "style": "secondary", "height": "sm",
         "action": {"type": "message", "label": "Categories", "text": "menu"}}
    ]

    bubble = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1A3A6B", "paddingAll": "12px",
            "contents": [{"type": "text", "text": header_text, "weight": "bold",
                          "color": "#FFFFFF", "size": "md"}]
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "0px", "spacing": "none",
            "contents": rows
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "paddingAll": "10px", "spacing": "xs",
            "contents": footer_contents
        }
    }
    return [{"type": "flex", "altText": header_text, "contents": bubble}]


def _line_product_detail(p: dict, pricelist, back_page: int = 0, is_retail: bool = False) -> list:
    """Detail bubble for a single product with qty buttons.
    is_retail=True (Mister Cochon / B2C) adds photo, stock, and weight —
    is_retail=False (French Delicatessen / B2B) keeps the original lean layout."""
    sku   = str(p.get("default_code") or "").strip().upper()
    name  = p.get("name") or sku
    short = _shorten_product_name(name)
    price = _line_get_client_price(p["id"], p.get("list_price", 0), pricelist)
    desc  = str(p.get("description_sale") or "").strip()
    price_int = int(round(price))
    pid   = p["id"]

    body_contents = []

    if is_retail:
        img_url = _line_get_ecwid_images().get(sku, "")
        if img_url:
            body_contents.append({"type": "image", "url": img_url, "size": "full",
                                   "aspectMode": "cover", "aspectRatio": "4:3",
                                   "margin": "none"})

    body_contents += [
        {"type": "text", "text": f"{price:,.0f} ฿",
         "weight": "bold", "size": "xxl", "color": "#C8102E", "margin": "md"},
        {"type": "text", "text": f"Ref: {sku}", "size": "xs",
         "color": "#888888", "margin": "xs"},
    ]

    if is_retail:
        _, weight_gr, _variant = _parse_product_variant(name)
        if weight_gr:
            w_text = f"{weight_gr/1000:.2f}".rstrip("0").rstrip(".") + " kg" if weight_gr >= 1000 else f"{weight_gr} g"
            body_contents.append({"type": "text", "text": f"⚖️ {w_text}", "size": "sm",
                                   "color": "#444444", "margin": "xs"})
        qty_available = p.get("qty_available", 0) or 0
        if qty_available > 0:
            stock_text = f"✅ มีสินค้า {int(qty_available)} ชิ้น / In stock"
            stock_color = "#1E8E3E"
        else:
            stock_text = "❌ สินค้าหมด / Out of stock"
            stock_color = "#C8102E"
        body_contents.append({"type": "text", "text": stock_text, "size": "sm",
                               "color": stock_color, "weight": "bold", "margin": "xs"})

    if desc:
        body_contents.append({"type": "text", "text": desc[:120], "size": "sm",
                               "color": "#444444", "wrap": True, "margin": "sm"})

    def qty_btn(q):
        return {"type": "button", "style": "secondary", "height": "sm", "flex": 1,
                "action": {"type": "postback", "label": f"×{q}",
                           "data": f"__aq_{pid}_{sku}_{price_int}_{q}"}}

    footer_contents = [
       {"type": "button", "style": "primary", "height": "sm", "color": "#1A3A6B",
         "action": {"type": "postback", "label": "🛒 Add to cart",
                    "data": f"__add_{pid}_{sku}"}},
        {"type": "button", "style": "secondary", "height": "sm",
         "action": {"type": "postback", "label": "← Back to list",
                    "data": f"__page_{back_page}"}}
    ]
    bubble = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1A3A6B", "paddingAll": "14px",
            "contents": [{"type": "text", "text": short, "weight": "bold",
                          "color": "#FFFFFF", "size": "md", "wrap": True, "maxLines": 3}]
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "14px", "spacing": "none",
            "contents": body_contents
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "10px", "spacing": "sm",
            "contents": footer_contents
        }
    }
    return [{"type": "flex", "altText": short, "contents": bubble}]


# ── Order helpers ─────────────────────────────────────────────────────────────

def _line_parse_order(text: str) -> list:
    items = []
    for part in re.split(r"[,;\n]+", text):
        part = part.strip()
        m = re.match(r"([A-Za-z0-9_\-]+)\s*[xX×]\s*(\d+)", part)
        if m:
            items.append((m.group(1).upper(), int(m.group(2))))
    return items


def _line_create_order(partner: dict, items: list) -> str:
    pricelist = partner.get("property_product_pricelist")

    fd_number = odoo_execute("ir.sequence", "next_by_code", [[SEQ_CODES["P"]]])
    if not fd_number:
        return "❌ Order creation failed (sequence error). Please contact us."

    order_vals = {
        "partner_id": partner["id"],
        "name": fd_number,
        "note": "Order via LINE Bot — French Delicatessen",
    }
    if pricelist and pricelist is not False:
        pl_id = pricelist[0] if isinstance(pricelist, list) else pricelist
        order_vals["pricelist_id"] = pl_id

    order_id = odoo_execute("sale.order", "create", [order_vals])

    lines_ok, lines_nok = [], []
    for item in items:
        sku        = item["sku"]
        qty        = item["qty"]
        product_id = item.get("product_id", 0)
        price      = item.get("price", 0)
        name       = item.get("name", sku)

        # Resolve product_id if not stored (fallback)
        if not product_id:
            hits = odoo_execute("product.product", "search_read",
                [[["default_code", "=", sku], ["active", "=", True]]],
                {"fields": ["id", "list_price"], "limit": 1, "context": {"lang": "en_US"}}
            )
            if not hits:
                lines_nok.append(sku)
                continue
            product_id = hits[0]["id"]
            price = hits[0]["list_price"]

        client_price = _line_get_client_price(product_id, price, pricelist)
        line_vals = {
            "order_id":        order_id,
            "product_id":      product_id,
            "name":            name,
            "product_uom_qty": qty,
            "price_unit":      client_price,
        }
        try:
            odoo_execute("sale.order.line", "create", [line_vals])
            lines_ok.append(f"{sku} x{qty}")
        except Exception:
            lines_nok.append(sku)

    if lines_ok:
        try:
            odoo_execute("sale.order", "action_confirm", [[order_id]])
        except Exception:
            pass

    msg = [f"✅ Order *{fd_number}* confirmed!"]
    if lines_ok:
        msg.append("Items: " + ", ".join(lines_ok))
    if lines_nok:
        msg.append("⚠️ SKUs not found: " + ", ".join(lines_nok))
    msg.append("Our team will contact you to confirm delivery. Thank you!")
    return "\n".join(msg)


def _line_cart_messages(cart: list, added_name: str = None, added_qty: int = None) -> list:
    """Return LINE messages showing the cart as a clean Flex bubble."""
    total = sum(i["price"] * i["qty"] for i in cart)
    n = len(cart)

    header_text = f"Your Order  ({n} item{'s' if n > 1 else ''})"
    if added_name and added_qty:
        header_text = f"Added: {_truncate(added_name, 28)} ×{added_qty}"

    body_rows = []
    for i in cart:
        short = _truncate(_shorten_product_name(i["name"]), 28)
        row_total = i["price"] * i["qty"]
        label = f"{short} ×{i['qty']}" + (" 🕐" if i.get("preorder") else "")
        body_rows.append({
            "type": "box", "layout": "horizontal", "margin": "sm",
            "contents": [
                {"type": "text", "text": label, "size": "sm",
                 "color": "#333333", "wrap": True, "flex": 4},
                {"type": "text", "text": f"{row_total:,.0f} ฿", "size": "sm",
                 "color": "#333333", "align": "end", "flex": 2}
            ]
        })

    if any(i.get("preorder") for i in cart):
        body_rows.append({"type": "text", "text": "🕐 Pre-order — out of stock, longer delivery time",
                           "size": "xs", "color": "#888888", "wrap": True, "margin": "sm"})

    body_rows += [
        {"type": "separator", "margin": "md"},
        {
            "type": "box", "layout": "horizontal", "margin": "md",
            "contents": [
                {"type": "text", "text": "TOTAL", "weight": "bold", "size": "md",
                 "color": "#1A3A6B", "flex": 4},
                {"type": "text", "text": f"{total:,.0f} ฿", "weight": "bold",
                 "size": "md", "color": "#C8102E", "align": "end", "flex": 2}
            ]
        }
    ]

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1A3A6B", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": header_text, "weight": "bold",
                 "size": "md", "color": "#FFFFFF", "wrap": True}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "14px",
            "contents": body_rows
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "paddingAll": "10px", "spacing": "xs",
            "contents": [
                {
                    "type": "button", "style": "primary", "height": "sm",
                    "color": "#1A3A6B",
                    "action": {"type": "message", "label": "Place order", "text": "checkout"}
                },
                {
                    "type": "button", "style": "secondary", "height": "sm",
                    "action": {"type": "message", "label": "Continue shopping", "text": "menu"}
                },
                {
                    "type": "button", "style": "secondary", "height": "sm",
                    "action": {"type": "message", "label": "Clear cart", "text": "cancel"}
                }
            ]
        }
    }
    return [{"type": "flex", "altText": f"Your order — {total:,.0f} ฿",
             "contents": bubble}]


# ── Webhook ───────────────────────────────────────────────────────────────────

# In-memory session: {user_id: {"category_products": [...], "page": 0, "state": ...}}
_line_sessions: dict = {}


@app.post("/webhook/line")
async def webhook_line(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"status": "ignored"}

    for event in body.get("events", []):
        event_type = event.get("type")

        # Accept both text messages and postback events
        if event_type == "postback":
            text = event.get("postback", {}).get("data", "").strip()
        elif event_type == "message" and event.get("message", {}).get("type") == "text":
            text = event["message"]["text"].strip()
        else:
            continue

        reply_token = event.get("replyToken", "")
        user_id = event.get("source", {}).get("userId", "")
        text_low = text.lower()

        partner = _line_get_partner(user_id)

        # ── Logout ────────────────────────────────────────────────────────
        if text_low in ("logout", "log out", "déconnexion", "ออกจากระบบ"):
            if partner:
                p = odoo_execute("res.partner", "read",
                    [[partner["id"]], ["comment"]], {})[0]
                comment = (p.get("comment") or "").replace(f"line:{user_id}", "").strip()
                odoo_execute("res.partner", "write",
                    [[partner["id"]], {"comment": comment}])
            _line_sessions.pop(user_id, None)
            line_reply(reply_token, [line_quick_reply(
                "👋 You have been logged out.\n\nEnter your 5-digit code to log back in:",
                [("🆕 New client", "NEW"), ("💬 Help", "HELP")]
            )])
            continue

        # ── Not authenticated ──────────────────────────────────────────────
        if not partner:
            sess = _line_sessions.get(user_id, {})

            # Registration step 2: waiting for company name
            if sess.get("state") == "awaiting_name":
                _line_sessions[user_id] = {"state": "awaiting_vat", "company": text.strip()}
                line_reply(reply_token, [line_text(
                    f"Company: *{text.strip()}*\n\n"
                    "Now please enter your VAT / DBD registration number:"
                )])
                continue

            # Registration step 3: waiting for DBD registration number
            if sess.get("state") == "awaiting_vat":
                company = sess.get("company", "")
                reg_input = text.strip()
                digits = re.sub(r"\D", "", reg_input)
                _line_sessions.pop(user_id, None)

                ok_fmt, reason = _dbd_verify(reg_input, company)

                if ok_fmt:
                    # Check if this VAT already exists in Odoo
                    existing = odoo_execute("res.partner", "search_read",
                        [[["vat", "=", digits], ["customer_rank", ">", 0]]],
                        {"fields": ["id", "name", "comment"], "limit": 1}
                    )
                    display = _line_get_display_name(user_id)
                    marker = f"line:{user_id}"

                    if existing:
                        p = existing[0]
                        comment = p.get("comment") or ""
                        if marker not in comment:
                            odoo_execute("res.partner", "write",
                                [[p["id"]], {"comment": (comment + "\n" + marker).strip()}])
                        partner = _line_get_partner(user_id)
                        line_reply(reply_token, [line_text(
                            f"✅ Welcome, {p['name']}!\n\n"
                            "Your account has been linked.\n"
                            "Type *menu* to browse our PRO catalog."
                        )])
                    else:
                        # Create pending partner — admin must verify on dbd.go.th and assign pricelist
                        odoo_execute("res.partner", "create", [{
                            "name": company,
                            "vat": digits,
                            "customer_rank": 1,
                            "is_company": True,
                            "country_id": 216,  # Thailand
                            "comment": (
                                f"line:{user_id}\n"
                                f"Registered via LINE bot — {display}\n"
                                f"DBD reg: {digits}"
                            ),
                        }])
                        partner = _line_get_partner(user_id)
                        line_reply(reply_token, [line_text(
                            f"✅ Welcome, {company}!\n\n"
                            "Your PRO account is now active.\n"
                            "Type *menu* to browse our catalog and place orders."
                        )])
                else:
                    if reason == "invalid_format":
                        msg = "❌ Invalid number format.\nThai DBD registration numbers have 13 digits."
                    else:
                        msg = "❌ Invalid registration number.\nPlease check and try again."
                    line_reply(reply_token, [line_quick_reply(
                        msg + "\n\nNeed help?",
                        [("🔄 Try again", "NEW"), ("💬 Contact us", "HELP")]
                    )])
                continue

            # New user: try code or offer NEW registration
            if text.upper() == "NEW":
                _line_sessions[user_id] = {"state": "awaiting_name"}
                line_reply(reply_token, [line_text(
                    "📝 *New client registration*\n\n"
                    "Please enter your company name:"
                )])
                continue

            # HELP → direct to human support
            if text.upper() == "HELP":
                line_reply(reply_token, [line_text(
                    "💬 *Need help?*\n\n"
                    "Please contact us on LINE:\n"
                    "👤 @jfbuc\n\n"
                    "We will set up your PRO account manually."
                )])
                continue

            # Quick login: 5-digit code (last 5 of DBD/VAT or phone)
            ok, name = _line_quick_login(text, user_id)
            if ok:
                partner = _line_get_partner(user_id)
                cats = _line_get_pro_categories(_line_extra_tags_for_partner(partner))
                welcome = line_text(f"✅ Welcome back, {name}!")
                if cats:
                    line_reply(reply_token, [welcome, _line_build_cat_flex(cats)])
                else:
                    line_reply(reply_token, [welcome, line_text("Type *menu* to browse our PRO catalog.")])
            else:
                line_reply(reply_token, [line_quick_reply(
                    "🇫🇷 *French Delicatessen — Professional Portal*\n\n"
                    "Enter your 5-digit code (last 5 digits of your DBD/VAT number),\n"
                    "or tap below:",
                    [("🆕 New client", "NEW"), ("💬 Help", "HELP")]
                )])
            continue

        pricelist = partner.get("property_product_pricelist")

        # ── Pagination (internal __page_N commands) ────────────────────────
        if text.startswith("__page_"):
            try:
                page = int(text.split("_")[-1])
            except ValueError:
                page = 0
            sess = _line_sessions.get(user_id, {})
            prods = sess.get("category_products", [])
            if prods:
                _line_sessions[user_id] = {**sess, "page": page}
                line_reply(reply_token, _line_build_carousel(prods, pricelist, page))
            else:
                line_reply(reply_token, [line_text("Type *menu* to browse products.")])
            continue

        # ── Product detail view: __view_{pid}_{sku} ───────────────────────
        if text.startswith("__view_"):
            raw = text[7:]
            first, _, sku = raw.partition("_")
            product_id = int(first) if first.isdigit() else 0
            sku = sku.upper()
            sess = _line_sessions.get(user_id, {})
            prods = sess.get("category_products", [])
            prod = next((p for p in prods if p.get("id") == product_id), None)
            if not prod and product_id:
                rows = odoo_execute("product.product", "search_read",
                    [[["id", "=", product_id]]],
                    {"fields": ["id", "name", "default_code", "list_price", "description_sale", "qty_available"],
                     "limit": 1, "context": {"lang": "en_US"}})
                prod = rows[0] if rows else None
            if not prod:
                line_reply(reply_token, [line_text("Product not found.")])
                continue
            back_page = sess.get("page", 0)
            line_reply(reply_token, _line_product_detail(prod, pricelist, back_page))
            continue

        # ── Add product to cart: show qty buttons ─────────────────────────
        # data format: __add_{product_id}_{sku}
        if text.startswith("__add_"):
            raw = text[6:]
            # Parse product_id embedded at front: __add_1234_DUCB2100
            first, _, rest = raw.partition("_")
            if first.isdigit() and rest:
                product_id = int(first)
                sku = rest.upper()
            else:
                product_id = 0
                sku = raw.upper()

            # Get price and name from session cache or Odoo
            sess = _line_sessions.get(user_id, {})
            prods = sess.get("category_products", [])
            prod = next((p for p in prods if p.get("id") == product_id
                         or str(p.get("default_code") or "").upper() == sku), None)
            if prod:
                product_id = product_id or prod["id"]
                price = _line_get_client_price(prod["id"], prod.get("list_price", 0), pricelist)
                name  = prod.get("name", sku)
            else:
                price = 0.0
                name  = sku

            short     = _shorten_product_name(name)
            price_int = int(round(price))
            sess = _line_sessions.get(user_id, {})
            _line_sessions[user_id] = {**sess,
                "pending_sku": sku, "pending_price": float(price_int),
                "pending_pid": product_id, "pending_name": name}
            line_reply(reply_token, [line_text(
                f"*{short}*\nUnit price: {price:.0f} ฿\n\nCombien d'unités ?"
            )])
            continue

        # ── Custom qty: __cq_{pid}_{sku}_{price} ──────────────────────────
        if text.startswith("__cq_"):
            parts = text.split("_")
            try:
                price_int  = int(parts[-1])
                sku        = parts[-2].upper()
                product_id = int(parts[-3]) if parts[-3].isdigit() else 0
                name       = sku
            except (ValueError, IndexError):
                line_reply(reply_token, [line_text("Invalid selection. Please try again.")])
                continue
            sess = _line_sessions.get(user_id, {})
            _line_sessions[user_id] = {**sess,
                "pending_sku": sku, "pending_price": float(price_int),
                "pending_pid": product_id, "pending_name": name}
            line_reply(reply_token, [line_text(f"Type the quantity for {sku}:")])
            continue

        # ── Free-text qty for custom quantity ──────────────────────────────
        sess = _line_sessions.get(user_id, {})
        if sess.get("pending_sku") and re.match(r"^\d+$", text) and 1 <= int(text) <= 9999:
            sku        = sess["pending_sku"]
            price      = sess["pending_price"]
            product_id = sess.get("pending_pid", 0)
            name       = sess.get("pending_name", sku)
            qty        = int(text)
            cart = list(sess.get("cart", []))
            for item in cart:
                if item["sku"] == sku:
                    item["qty"] += qty
                    break
            else:
                cart.append({"sku": sku, "name": name,
                              "price": price, "product_id": product_id, "qty": qty})
            _line_sessions[user_id] = {**sess, "cart": cart,
                                        "pending_sku": None, "pending_price": None}
            short = _shorten_product_name(name)
            total = sum(i["price"] * i["qty"] for i in cart)
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
                {"fields": ["id", "name", "default_code", "list_price", "description_sale", "qty_available"], "limit": 200,
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
                {"fields": ["id", "name", "default_code", "list_price", "description_sale", "qty_available"], "limit": 200,
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
                    {"fields": ["id", "name", "default_code", "list_price", "description_sale", "qty_available"], "limit": 200,
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
                        {"fields": ["id", "name", "default_code", "list_price", "description_sale", "qty_available"],
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

        # ── Free-text order: "SKU x2" or "SKU1 x2, SKU2 x3" ────────────────
        elif _line_parse_order(text):
            parsed = _line_parse_order(text)
            sess = _line_sessions.get(user_id, {})
            cart = list(sess.get("cart", []))
            added, not_found = [], []
            for sku, qty in parsed:
                hits = odoo_execute("product.product", "search_read",
                    [[["default_code", "=ilike", sku], ["active", "=", True]]],
                    {"fields": ["id", "name", "default_code", "list_price"],
                     "limit": 1, "context": {"lang": "en_US"}}
                )
                if not hits:
                    not_found.append(sku)
                    continue
                prod = hits[0]
                price = _line_get_client_price(prod["id"], prod.get("list_price", 0), pricelist)
                for item in cart:
                    if item["sku"] == sku:
                        item["qty"] += qty
                        break
                else:
                    cart.append({"sku": sku, "name": prod["name"],
                                  "price": price, "product_id": prod["id"], "qty": qty})
                added.append(f"{_shorten_product_name(prod['name'])} ×{qty}")
            _line_sessions[user_id] = {**sess, "cart": cart}
            lines = []
            if added:
                total = sum(i["price"] * i["qty"] for i in cart)
                lines.append("✅ Added:\n" + "\n".join(added))
                lines.append(f"\nCart: {len(cart)} item{'s' if len(cart)>1 else ''} — {total:,.0f} ฿")
            if not_found:
                lines.append("❌ Not found: " + ", ".join(not_found))
            line_reply(reply_token, [line_text("\n".join(lines))])
        
        # ── SKU lookup: type a SKU to open product detail ─────────────────
        else:
            sess = _line_sessions.get(user_id, {})
            awaiting_sku = sess.get("state") == "awaiting_sku"
            sku_match = re.match(r"^([A-Za-z0-9_\-]{2,20})(?:\s+(\d+))?$", text.strip()) if awaiting_sku else None
            if sku_match:
                raw_sku = sku_match.group(1).upper()
                prods_found = odoo_execute("product.product", "search_read",
                    [[["default_code", "=ilike", raw_sku]]],
                    {"fields": ["id", "name", "default_code", "list_price", "description_sale", "qty_available"],
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
        tag = " 🕐(Pre-order)" if i.get("preorder") else ""
        lines.append(f"• {i['name'][:30]} ×{i['qty']} = {i['qty']*i['price']:,.0f}฿{tag}")
    order_text = "\n".join(lines)
    if any(i.get("preorder") for i in cart):
        order_text += "\n\n🕐 มีสินค้าพรีออเดอร์ อาจใช้เวลาจัดส่งนานกว่าปกติ\nIncludes pre-order item(s) — may take longer to deliver."
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

            # ── Registration: email not found → offer to sign up ──────────
            if sess.get("state") == "awaiting_register_choice":
                if text_low in ("ใช่", "yes", "สมัคร", "register", "ลงทะเบียน", "✅"):
                    _retail_sessions[user_id] = {"state": "awaiting_reg_name",
                                                  "reg_email": sess.get("reg_email", "")}
                    retail_reply(reply_token, [line_text(
                        "📝 ลงทะเบียนสมาชิกใหม่ / New registration\n\n"
                        "กรุณากรอกชื่อ-นามสกุลของคุณ\nPlease enter your full name:"
                    )])
                else:
                    _retail_sessions.pop(user_id, None)
                    retail_reply(reply_token, [line_text(
                        "กรุณากรอกอีเมลของคุณอีกครั้ง\nPlease enter your email again."
                    )])
                continue

            # Registration step 2: waiting for full name
            if sess.get("state") == "awaiting_reg_name":
                name = text.strip()
                if len(name) < 2:
                    retail_reply(reply_token, [line_text(
                        "กรุณากรอกชื่อ-นามสกุลที่ถูกต้อง\nPlease enter a valid full name:"
                    )])
                    continue
                _retail_sessions[user_id] = {"state": "awaiting_reg_phone",
                                              "reg_name": name,
                                              "reg_email": sess.get("reg_email", "")}
                retail_reply(reply_token, [line_text(
                    f"ชื่อ: *{name}*\n\n"
                    "กรุณากรอกเบอร์โทรศัพท์ของคุณ\nNow please enter your phone number:"
                )])
                continue

            # Registration step 3: waiting for phone → create the account
            if sess.get("state") == "awaiting_reg_phone":
                phone = re.sub(r"[^\d+]", "", text.strip())
                if len(re.sub(r"\D", "", phone)) < 8:
                    retail_reply(reply_token, [line_text(
                        "เบอร์โทรศัพท์ไม่ถูกต้อง กรุณากรอกใหม่\n"
                        "Invalid phone number, please try again:"
                    )])
                    continue

                name  = sess.get("reg_name", "").strip()
                email = sess.get("reg_email", "").strip()
                _retail_sessions.pop(user_id, None)

                display = _line_get_display_name(user_id)
                marker  = f"line_retail:{user_id}"

                # Guard: someone may have registered with this email in the
                # gap since the failed lookup — link to that account instead
                # of creating a duplicate.
                existing = None
                if email:
                    existing = odoo_execute("res.partner", "search_read",
                        [[["email", "=ilike", email], ["customer_rank", ">", 0]]],
                        {"fields": ["id", "name", "comment"], "limit": 1}
                    )
                    existing = existing[0] if existing else None

                if existing:
                    comment = existing.get("comment") or ""
                    if marker not in comment:
                        odoo_execute("res.partner", "write",
                            [[existing["id"]], {"comment": (comment + "\n" + marker).strip()}])
                    pname = existing["name"]
                else:
                    odoo_execute("res.partner", "create", [{
                        "name": name or display or "Mister Cochon Customer",
                        "email": email,
                        "phone": phone,
                        "customer_rank": 1,
                        "is_company": False,
                        "country_id": 216,  # Thailand
                        "comment": (
                            f"{marker}\n"
                            f"Registered via LINE bot — {display}"
                        ),
                    }])
                    pname = name

                partner = _retail_get_partner(user_id)
                cats = _retail_get_categories()
                retail_reply(reply_token, [
                    line_text(
                        f"✅ ยินดีต้อนรับ คุณ{pname}!\nWelcome, {pname}!\n\n"
                        "บัญชีของคุณถูกสร้างแล้ว\nYour account has been created."
                    ),
                    _retail_build_cat_flex(cats) if cats else line_text("พิมพ์ เมนู เพื่อดูสินค้า")
                ])
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
            elif re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", text.strip()):
                # Looks like an email but no match in Odoo → offer registration
                _retail_sessions[user_id] = {"state": "awaiting_register_choice",
                                              "reg_email": text.strip()}
                retail_reply(reply_token, [line_quick_reply(
                    "❌ ไม่พบอีเมลนี้ในระบบ\nEmail not found.\n\n"
                    "ต้องการสมัครสมาชิกใหม่หรือไม่?\nWould you like to register as a new customer?",
                    [("✅ สมัคร / Register", "ใช่"), ("🔄 ลองอีกครั้ง / Try again", "ไม่ใช่")]
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
                {"fields": ["id", "name", "default_code", "list_price", "description_sale", "qty_available"],
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
                    {"fields": ["id", "name", "default_code", "list_price", "description_sale", "qty_available"],
                     "limit": 1, "context": {"lang": "en_US"}}
                )
                prod = found[0] if found else None
            if prod:
                retail_reply(reply_token, _line_product_detail(prod, pricelist, sess.get("page", 0), is_retail=True))
            continue

        # ── Postback: page navigation ──────────────────────────────────────
        if text.startswith("__page_"):
            page = int(text.split("__page_")[1])
            prods = sess.get("category_products", [])
            _retail_sessions[user_id] = {**sess, "page": page}
            retail_reply(reply_token, _line_build_carousel(prods, pricelist, page))
            continue

        # ── Postback: add to cart — button from shared product detail footer ──
        # data format: __add_{product_id}_{sku}  (same format as B2B)
        if text.startswith("__add_"):
            raw = text[6:]
            first, _, rest = raw.partition("_")
            if first.isdigit() and rest:
                product_id = int(first)
                sku = rest.upper()
            else:
                product_id = 0
                sku = raw.upper()

            prods = sess.get("category_products", [])
            prod = next((p for p in prods if p.get("id") == product_id
                         or str(p.get("default_code") or "").upper() == sku), None)
            if not prod and product_id:
                found = odoo_execute("product.product", "search_read",
                    [[["id", "=", product_id]]],
                    {"fields": ["id", "name", "default_code", "list_price", "qty_available"],
                     "limit": 1, "context": {"lang": "en_US"}}
                )
                prod = found[0] if found else None

            if prod:
                product_id = product_id or prod["id"]
                price = _line_get_client_price(prod["id"], prod.get("list_price", 0), pricelist)
                name  = prod.get("name", sku)
                is_preorder = (prod.get("qty_available", 0) or 0) <= 0
            else:
                price = 0.0
                name  = sku
                is_preorder = False

            short = _truncate(_shorten_product_name(name), 40)
            _retail_sessions[user_id] = {**sess, "state": "awaiting_qty",
                "pending_pid": product_id, "pending_sku": sku,
                "pending_price": int(round(price)), "pending_preorder": is_preorder}
            prompt = f"*{short}*\nUnit price: {price:.0f} ฿\n\nกรุณากรอกจำนวน:\nEnter quantity:"
            if is_preorder:
                prompt = (f"*{short}*\nUnit price: {price:.0f} ฿\n\n"
                           "🕐 สินค้าหมด — คุณสามารถสั่งพรีออเดอร์ได้\n"
                           "Out of stock — you can still place a pre-order,\n"
                           "delivery time may be longer than usual.\n\n"
                           "กรุณากรอกจำนวน:\nEnter quantity:")
            retail_reply(reply_token, [line_text(prompt)])
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
                short = _truncate(_shorten_product_name(name), 25)
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
            is_preorder = bool(sess.get("pending_preorder", False))
            prods = sess.get("category_products", [])
            prod = next((p for p in prods if p.get("id") == pid), None)
            name = prod["name"] if prod else sku
            existing = next((i for i in cart if i["pid"] == pid), None)
            if existing:
                existing["qty"] += qty
                existing["preorder"] = existing.get("preorder", False) or is_preorder
            else:
                cart.append({"pid": pid, "sku": sku, "name": name, "qty": qty,
                              "price": price, "preorder": is_preorder})
            total = sum(i["qty"] * i["price"] for i in cart)
            _retail_sessions[user_id] = {**{k: v for k, v in sess.items()
                                            if k not in ("state", "pending_pid", "pending_sku",
                                                          "pending_price", "pending_preorder")},
                                          "cart": cart}
            short = _truncate(_shorten_product_name(name), 25)
            preorder_note = "\n🕐 พรีออเดอร์ — Pre-order (out of stock)" if is_preorder else ""
            retail_reply(reply_token, [line_text(
                f"✅ {short} ×{qty} เพิ่มแล้ว{preorder_note}\nตะกร้า: {len(cart)} รายการ — {total:,.0f}฿"
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
                preorder_note = ""
                if any(i.get("preorder") for i in cart):
                    preorder_note = ("\n\n🕐 มีสินค้าพรีออเดอร์ในตะกร้า อาจใช้เวลาจัดส่งนานกว่าปกติ\n"
                                      "Includes pre-order item(s) — may take longer to deliver.")
                retail_reply(reply_token, [line_quick_reply(
                    f"ยอดรวม {total:,.0f}฿ — เลือกวิธีชำระเงิน\nTotal {total:,.0f}฿ — Choose payment:{preorder_note}",
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
