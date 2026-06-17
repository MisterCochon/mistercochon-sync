import os
import csv
import io
import re
import requests
import xmlrpc.client
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

app = FastAPI()

VERSION = "2026-06-16-v47-fix-order-lines"

# Enregistrer la police Thai au démarrage
_THAI_FONT = "NotoSansThai"
try:
    _font_path = os.path.join(os.path.dirname(__file__), "NotoSansThai-Regular.ttf")
    pdfmetrics.registerFont(TTFont(_THAI_FONT, _font_path))
except Exception:
    _THAI_FONT = "Helvetica"  # fallback

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

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_LOGIN = os.getenv("ODOO_LOGIN")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

ECWID_STORE_ID = os.getenv("ECWID_STORE_ID")
ECWID_TOKEN = os.getenv("ECWID_TOKEN")

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


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


def ecwid_get(endpoint, params=None):
    url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}{endpoint}"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {ECWID_TOKEN}"},
        params=params or {}
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

    def find_product(sku: str, name: str):
        if sku:
            p = prod_by_sku.get(str(sku).strip().upper())
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

    # ── Commandes déjà importées (client_order_ref = numéro Ecwid) ───────────
    existing = odoo_execute("sale.order", "search_read",
        [[["client_order_ref", "!=", False]]],
        {"fields": ["client_order_ref"], "limit": 5000}
    )
    already_imported = {str(e["client_order_ref"]) for e in existing}

    # ── Récupérer TOUTES les commandes Ecwid (pagination) ────────────────────
    all_items = []
    offset = 0
    batch = 100
    total_ecwid = 0
    while True:
        ecwid_data = ecwid_get("/orders", {"limit": batch, "offset": offset,
                                           "sortBy": "ORDER_DATE_DESC"})
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
            try:
                partner_id = odoo_execute("res.partner", "create", [vals_p])
                if email:
                    partner_by_email[email] = partner_id
            except Exception as e:
                errors_list.append({"ecwid": order_num, "reason": f"Création client: {e}"})
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
            "client_order_ref": order_num,
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

    # Client
    email  = str(eco.get("email") or "").strip().lower()
    ship   = eco.get("shippingPerson") or eco.get("billingPerson") or {}
    cname  = ship.get("name") or eco.get("email") or f"Ecwid #{order_num}"
    phone  = ship.get("phone") or ""
    street = ship.get("street") or ""
    city   = ship.get("city") or ""
    zipcode = ship.get("postalCode") or ""

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
    orders = odoo_execute("sale.order", "search_read",
        [[["client_order_ref", "=", ecwid_order_ref]]],
        {"fields": ["id", "name", "amount_total", "invoice_ids", "partner_id"], "limit": 1}
    )
    if not orders:
        return {"found": False}

    order = orders[0]
    order_id = order["id"]

    # Créer la facture si pas encore créée
    invoice_ids = order.get("invoice_ids") or []
    if not invoice_ids:
        try:
            odoo_execute("sale.order", "action_lock", [[order_id]])
        except Exception:
            pass
        try:
            invoice_ids = odoo_execute("sale.order", "action_invoice_create", [[order_id]])
            if not isinstance(invoice_ids, list):
                invoice_ids = [invoice_ids]
        except Exception:
            pass

    if invoice_ids:
        inv_id = invoice_ids[0]
        # Confirmer la facture
        try:
            odoo_execute("account.move", "action_post", [[inv_id]])
        except Exception:
            pass
        # Enregistrer le paiement
        try:
            journals = odoo_execute("account.journal", "search_read",
                [[["type", "=", "bank"]]],
                {"fields": ["id", "name"], "limit": 1}
            )
            if journals:
                payment_vals = {
                    "move_id": inv_id,
                    "journal_id": journals[0]["id"],
                    "payment_method_line_id": False,
                    "amount": order["amount_total"],
                    "currency_id": False,
                }
                odoo_execute("account.payment.register", "create", [payment_vals])
        except Exception:
            pass

    # Ajouter note Stripe en internal note
    try:
        odoo_execute("sale.order", "message_post", [[order_id]], {
            "body": f"Paiement Stripe confirmé : {stripe_payment_id}",
            "message_type": "comment",
            "subtype_xmlid": "mail.mt_note",
        })
    except Exception:
        pass

    return {"found": True, "order": order["name"], "invoiced": bool(invoice_ids)}


@app.post("/webhook/stripe")
async def webhook_stripe(request: Request):
    """
    Webhook Stripe — appelé automatiquement par Stripe après un paiement réussi.
    Enregistrer l'URL dans Stripe Dashboard → Developers → Webhooks
    URL : https://mistercochon-backend.onrender.com/webhook/stripe
    Événements : payment_intent.succeeded, checkout.session.completed

    Cherche la commande Ecwid/Odoo via les metadata Stripe (order_id ou ecwid_order_id).
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Vérification signature Stripe (si secret configuré)
    if STRIPE_WEBHOOK_SECRET:
        try:
            import hmac, hashlib, time
            parts = {p.split("=")[0]: p.split("=")[1]
                     for p in sig_header.split(",") if "=" in p}
            ts = parts.get("t", "0")
            v1 = parts.get("v1", "")
            signed = f"{ts}.{payload.decode()}"
            expected = hmac.new(
                STRIPE_WEBHOOK_SECRET.encode(),
                signed.encode(),
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, v1):
                return {"status": "error", "reason": "signature invalide"}
        except Exception:
            pass

    try:
        event = await request.json() if not payload else __import__("json").loads(payload)
    except Exception:
        return {"status": "error", "reason": "JSON invalide"}

    event_type = event.get("type", "")
    data_obj   = event.get("data", {}).get("object", {})

    # Extraire référence commande Ecwid depuis metadata Stripe
    metadata = data_obj.get("metadata") or {}
    ecwid_ref = (
        metadata.get("ecwid_order_id") or
        metadata.get("order_id") or
        metadata.get("orderNumber") or
        data_obj.get("description") or ""
    )
    stripe_id = data_obj.get("id", "")

    if event_type not in ("payment_intent.succeeded", "checkout.session.completed",
                          "charge.succeeded"):
        return {"status": "ignored", "event": event_type}

    if not ecwid_ref:
        return {
            "status": "ignored",
            "reason": "Pas de référence commande dans les metadata Stripe",
            "stripe_id": stripe_id,
            "metadata": metadata,
        }

    result = _mark_order_paid_odoo(str(ecwid_ref), stripe_id)
    return {"status": "ok", "event": event_type, "ecwid_ref": ecwid_ref, "result": result}


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
    all_ids = []
    offset = 0
    while True:
        batch = odoo_execute("sale.order", "search_read",
            [[["name", "like", "S"]]],
            {"fields": ["id", "name", "state"], "limit": 200, "offset": offset}
        )
        if not batch:
            break
        # Garder seulement les FD-S (ex: FD0626265S)
        ecwid_batch = [o for o in batch if o["name"].endswith("S") and "FD" in o["name"]]
        all_ids.extend(ecwid_batch)
        if len(batch) < 200:
            break
        offset += 200

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

