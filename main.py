import os
import csv
import io
import requests
import xmlrpc.client
from fastapi import FastAPI, UploadFile, File

app = FastAPI()

VERSION = "2026-06-11-v14-delete-assign"

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
