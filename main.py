import os
import csv
import io
import requests
import xmlrpc.client
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import StreamingResponse
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

app = FastAPI()

VERSION = "2026-06-12-v26-order-numbering"

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_LOGIN = os.getenv("ODOO_LOGIN")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

ECWID_STORE_ID = os.getenv("ECWID_STORE_ID")
ECWID_TOKEN = os.getenv("ECWID_TOKEN")


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
            {"fields": ["product_id", "product_uom_qty", "product_uom", "name", "default_code"]}
        )

        # Récupérer les SKUs des variantes
        variant_ids = [l["product_id"][0] for l in lines if l.get("product_id")]
        variants = odoo_execute("product.product", "read", [variant_ids],
            {"fields": ["id", "default_code", "display_name"]}
        )
        sku_map = {v["id"]: v.get("default_code") or "" for v in variants}

        # Numéro de commande affiché
        order_ref = order.get("client_order_ref") or order["name"]
        order_date = str(order["date_order"])[:10] if order.get("date_order") else ""
        customer_name = partner["name"]

        # Adresse client
        addr_parts = [partner.get("street") or "", partner.get("street2") or "",
                      partner.get("city") or "", partner.get("zip") or ""]
        address = "\n".join(p for p in addr_parts if p)
        phone = partner.get("phone") or ""

        # Générer le PDF
        buf = io.BytesIO()
        w, h = A4
        c = canvas.Canvas(buf, pagesize=A4)

        # ─── LOGO placeholder ───
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
        c.rect(15*mm, h - 75*mm, 72*mm, 18*mm)
        box_labels = ["S1", "S2", "A1", "F1", "F2", "F3"]
        for i, lbl in enumerate(box_labels):
            x = 17*mm + i * 11.5*mm
            c.rect(x, h - 70*mm, 8*mm, 8*mm)
            c.setFont("Helvetica", 8)
            c.drawString(x + 1*mm, h - 63*mm, lbl)

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
            prod_name = line["product_id"][1] if line.get("product_id") else ""
            qty = line.get("product_uom_qty", 0)
            uom = line.get("product_uom", ["", ""])[1] if line.get("product_uom") else ""
            sku = sku_map.get(vid, "")
            description = line.get("name") or prod_name

            # Nom produit (gras)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(70*mm, y, prod_name)

            # Variation (si différente du nom)
            variation = description.replace(prod_name, "").strip(" \n-[]")
            if variation:
                c.setFont("Helvetica", 8)
                c.drawString(70*mm, y - 4*mm, variation)

            # Qté dans grande police
            c.setFont("Helvetica-Bold", 16)
            c.drawString(17*mm, y - 4*mm, str(int(qty) if qty == int(qty) else qty))
            c.setFont("Helvetica", 9)
            c.drawString(38*mm, y - 3*mm, uom)

            # SKU
            c.setFont("Helvetica", 8)
            c.drawString(160*mm, y, sku)

            # Case Qty delivered
            c.rect(175*mm, y - 6*mm, 22*mm, 10*mm)

            # Séparateur
            c.setLineWidth(0.3)
            c.line(15*mm, y - 9*mm, 200*mm, y - 9*mm)

            y -= 18*mm
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


@app.post("/set-order-number/{order_id}/{order_type}")
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
