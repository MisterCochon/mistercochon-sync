from fastapi import FastAPI, HTTPException
import requests
import os
import xmlrpc.client

app = FastAPI()

ECWID_STORE_ID = os.getenv("ECWID_STORE_ID")
ECWID_TOKEN = os.getenv("ECWID_TOKEN")

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_LOGIN = os.getenv("ODOO_LOGIN")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")


def get_odoo():
    if not all([ODOO_URL, ODOO_DB, ODOO_LOGIN, ODOO_PASSWORD]):
        raise Exception("Variables Odoo manquantes")

    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(ODOO_DB, ODOO_LOGIN, ODOO_PASSWORD, {})

    if not uid:
        raise Exception("Connexion Odoo échouée")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
    return ODOO_DB, uid, ODOO_PASSWORD, models


def ecwid_get(endpoint):
    if not ECWID_STORE_ID or not ECWID_TOKEN:
        raise HTTPException(status_code=500, detail="Variables Ecwid manquantes")

    url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/{endpoint}"
    headers = {"Authorization": f"Bearer {ECWID_TOKEN}"}
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    return response.json()


def clean_sku(sku):
    if not sku:
        return ""
    return str(sku).strip().upper()


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "mistercochon-backend",
        "version": "2026-05-20-v3-variants"
    }


@app.get("/odoo-test")
async def odoo_test():
    try:
        db, uid, password, models = get_odoo()

        user = models.execute_kw(
            db,
            uid,
            password,
            "res.users",
            "read",
            [[uid]],
            {"fields": ["id", "name", "login"]}
        )

        return {
            "status": "connected",
            "uid": uid,
            "user": user
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }


@app.get("/check-sku")
async def check_sku():
    try:
        db, uid, password, models = get_odoo()

        products = models.execute_kw(
            db,
            uid,
            password,
            "product.product",
            "search_read",
            [[["default_code", "=", False]]],
            {
                "fields": ["id", "name", "default_code", "product_tmpl_id"],
                "limit": 200
            }
        )

        return {
            "status": "ok",
            "missing_sku_count": len(products),
            "missing_sku": products
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }


@app.get("/find-product/{sku}")
async def find_product(sku: str):
    try:
        db, uid, password, models = get_odoo()
        sku_clean = clean_sku(sku)

        product = models.execute_kw(
            db,
            uid,
            password,
            "product.product",
            "search_read",
            [[["default_code", "=", sku_clean]]],
            {
                "fields": ["id", "name", "default_code", "product_tmpl_id"],
                "limit": 20
            }
        )

        return {
            "status": "ok",
            "sku": sku_clean,
            "count": len(product),
            "products": product
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }


@app.get("/product-variants/{template_id}")
async def product_variants(template_id: int):
    try:
        db, uid, password, models = get_odoo()

        template = models.execute_kw(
            db,
            uid,
            password,
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
            return {
                "status": "not_found",
                "template_id": template_id
            }

        variant_ids = template[0].get("product_variant_ids", [])

        variants = []
        if variant_ids:
            variants = models.execute_kw(
                db,
                uid,
                password,
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
                        "product_tmpl_id"
                    ]
                }
            )

        return {
            "status": "ok",
            "template": template[0],
            "variant_count": len(variants),
            "variants": variants
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }


@app.get("/ecwid-test")
async def ecwid_test():
    try:
        data = ecwid_get("profile")

        return {
            "status": "connected",
            "store_id": ECWID_STORE_ID,
            "store": data.get("generalInfo", {}).get("storeName")
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }
@app.get("/ecwid-product/{product_id}")
async def ecwid_product(product_id: int):

    try:

        data = ecwid_get(f"products/{product_id}")

        variants = data.get("combinations", [])

        result = []

        for v in variants:

            result.append({
                "sku": v.get("sku"),
                "price": v.get("price"),
                "quantity": v.get("quantity"),
                "options": v.get("options")
            })

        return {
            "status": "ok",
            "id": data.get("id"),
            "name": data.get("name"),
            "base_sku": data.get("sku"),
            "variant_count": len(result),
            "variants": result
        }

    except Exception as e:

        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }
@app.get("/ecwid-sku/{sku}")
async def ecwid_sku(sku: str):

    try:
        data = ecwid_get("products")

        products = data.get("items", [])

        matches = []

        for p in products:

            if p.get("sku") == sku:
                matches.append({
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "sku": p.get("sku")
                })

            for c in p.get("combinations", []):
                if c.get("sku") == sku:
                    matches.append({
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "variant_sku": c.get("sku"),
                        "options": c.get("options")
                    })

        return {
            "status": "ok",
            "count": len(matches),
            "results": matches
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }
@app.get("/ecwid-raw/{product_id}")
async def ecwid_raw(product_id: int):
    try:
        data = ecwid_get(f"products/{product_id}")

        return {
            "status": "ok",
            "id": data.get("id"),
            "name": data.get("name"),
            "sku": data.get("sku"),
            "keys": list(data.keys()),
            "options": data.get("options"),
            "combinations": data.get("combinations"),
            "variations": data.get("variations")
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }
@app.get("/sync-product/{product_id}")
async def sync_product(product_id: int):

    try:
        db, uid, password, models = get_odoo()

        ecwid = ecwid_get(f"products/{product_id}")

        product_name = ecwid["name"]
        combinations = ecwid.get("combinations", [])

        # Recherche produit modèle Odoo
        template = models.execute_kw(
            db,
            uid,
            password,
            "product.template",
            "search_read",
            [[["name", "=", product_name]]],
            {
                "fields": ["id", "name"],
                "limit": 1
            }
        )

        if not template:
            return {
                "status": "error",
                "error": f"Produit {product_name} non trouvé dans Odoo"
            }

        template_id = template[0]["id"]

        results = []

        for combo in combinations:

            variant_name = combo["options"][0]["value"]
            sku = combo["sku"]

            results.append({
                "variant": variant_name,
                "sku": sku
            })

        return {
            "status": "ok",
            "product": product_name,
            "template_id": template_id,
            "variants_found": len(results),
            "variants": results
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }
@app.get("/apply-sync-product/{product_id}")
async def apply_sync_product(product_id: int):
    try:
        db, uid, password, models = get_odoo()

        ecwid = ecwid_get(f"products/{product_id}")
        product_name = ecwid["name"]
        combinations = ecwid.get("combinations", [])

        template = models.execute_kw(
            db, uid, password,
            "product.template", "search_read",
            [[["name", "=", product_name]]],
            {"fields": ["id", "name"], "limit": 1}
        )

        if not template:
            return {"status": "error", "error": f"Produit Odoo non trouvé: {product_name}"}

        template_id = template[0]["id"]

        # 1. Trouver ou créer l'attribut Format
        attr = models.execute_kw(
            db, uid, password,
            "product.attribute", "search_read",
            [[["name", "=", "Format"]]],
            {"fields": ["id"], "limit": 1}
        )

        if attr:
            attr_id = attr[0]["id"]
        else:
            attr_id = models.execute_kw(
                db, uid, password,
                "product.attribute", "create",
                [{"name": "Format"}]
            )

        value_ids = []
        sku_by_value = {}

        # 2. Créer les valeurs d'attribut
        for combo in combinations:
            variant_value = combo["options"][0]["value"]
            sku = combo.get("sku")

            val = models.execute_kw(
                db, uid, password,
                "product.attribute.value", "search_read",
                [[
                    ["name", "=", variant_value],
                    ["attribute_id", "=", attr_id]
                ]],
                {"fields": ["id"], "limit": 1}
            )

            if val:
                value_id = val[0]["id"]
            else:
                value_id = models.execute_kw(
                    db, uid, password,
                    "product.attribute.value", "create",
                    [{
                        "name": variant_value,
                        "attribute_id": attr_id
                    }]
                )

            value_ids.append(value_id)
            sku_by_value[variant_value] = sku

        # 3. Ajouter l'attribut au produit
        existing_lines = models.execute_kw(
            db, uid, password,
            "product.template.attribute.line", "search_read",
            [[
                ["product_tmpl_id", "=", template_id],
                ["attribute_id", "=", attr_id]
            ]],
            {"fields": ["id"], "limit": 1}
        )

        if existing_lines:
            line_id = existing_lines[0]["id"]
            models.execute_kw(
                db, uid, password,
                "product.template.attribute.line", "write",
                [[line_id], {
                    "value_ids": [(6, 0, value_ids)]
                }]
            )
        else:
            models.execute_kw(
                db, uid, password,
                "product.template.attribute.line", "create",
                [{
                    "product_tmpl_id": template_id,
                    "attribute_id": attr_id,
                    "value_ids": [(6, 0, value_ids)]
                }]
            )

        # 4. Relire les variantes générées
        template_after = models.execute_kw(
            db, uid, password,
            "product.template", "read",
            [[template_id]],
            {"fields": ["product_variant_ids"]}
        )

        variant_ids = template_after[0]["product_variant_ids"]

        variants = models.execute_kw(
            db, uid, password,
            "product.product", "read",
            [variant_ids],
            {"fields": ["id", "name", "default_code"]}
        )

        updated = []

        # 5. Mettre les SKU sur les variantes
        for variant in variants:
            name = variant["name"]

            for value_name, sku in sku_by_value.items():
                if value_name.lower() in name.lower():
                    models.execute_kw(
                        db, uid, password,
                        "product.product", "write",
                        [[variant["id"]], {
                            "default_code": sku
                        }]
                    )

                    updated.append({
                        "variant_id": variant["id"],
                        "variant": name,
                        "sku": sku
                    })

        return {
            "status": "ok",
            "product": product_name,
            "template_id": template_id,
            "updated_count": len(updated),
            "updated": updated
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }
