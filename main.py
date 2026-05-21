import os
import requests
import xmlrpc.client
from fastapi import FastAPI

app = FastAPI()

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_LOGIN = os.getenv("ODOO_LOGIN")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

ECWID_STORE_ID = os.getenv("ECWID_STORE_ID")
ECWID_TOKEN = os.getenv("ECWID_TOKEN")


def get_odoo():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_LOGIN, ODOO_PASSWORD, {})

    if not uid:
        raise Exception("Connexion Odoo échouée")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "mistercochon-backend",
        "version": "2026-05-21-v4-check-sku"
    }


@app.get("/odoo-test")
def odoo_test():
    try:
        uid, models = get_odoo()

        user = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
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
def check_sku():
    """
    Vérifie les vrais produits Odoo sans SKU.
    On exclut les produits archivés.
    """

    try:
        uid, models = get_odoo()

        products = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "product.product",
            "search_read",
            [[
                ["active", "=", True],
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
                "limit": 500
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
def find_product(sku: str):
    try:
        uid, models = get_odoo()

        products = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "product.product",
            "search_read",
            [[
                ["default_code", "=", sku]
            ]],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "product_tmpl_id",
                    "lst_price"
                ],
                "limit": 10
            }
        )

        return {
            "status": "ok",
            "sku": sku,
            "count": len(products),
            "products": products
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }


@app.get("/product-variants/{template_id}")
def product_variants(template_id: int):
    try:
        uid, models = get_odoo()

        template = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
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

        variants = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
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
def ecwid_test():
    try:
        url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/profile"
        headers = {
            "Authorization": f"Bearer {ECWID_TOKEN}"
        }

        response = requests.get(url, headers=headers)
        data = response.json()

        return {
            "status": "ok",
            "ecwid_status_code": response.status_code,
            "store": data
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }


@app.get("/ecwid-product/{product_id}")
def ecwid_product(product_id: int):
    try:
        url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/products/{product_id}"
        headers = {
            "Authorization": f"Bearer {ECWID_TOKEN}"
        }

        response = requests.get(url, headers=headers)
        product = response.json()

        variants = product.get("combinations", [])

        return {
            "status": "ok",
            "id": product.get("id"),
            "name": product.get("name"),
            "base_sku": product.get("sku"),
            "variant_count": len(variants),
            "variants": [
                {
                    "sku": v.get("sku"),
                    "price": v.get("price"),
                    "quantity": v.get("quantity"),
                    "options": v.get("options")
                }
                for v in variants
            ]
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }


@app.get("/ecwid-raw/{product_id}")
def ecwid_raw(product_id: int):
    try:
        url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/products/{product_id}"
        headers = {
            "Authorization": f"Bearer {ECWID_TOKEN}"
        }

        response = requests.get(url, headers=headers)

        return response.json()

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }


@app.get("/ecwid-sku/{sku}")
def ecwid_sku(sku: str):
    """
    Recherche un SKU dans Ecwid :
    - SKU parent
    - SKU variante
    """

    try:
        url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/products"
        headers = {
            "Authorization": f"Bearer {ECWID_TOKEN}"
        }

        response = requests.get(
            url,
            headers=headers,
            params={
                "keyword": sku,
                "limit": 100
            }
        )

        data = response.json()
        items = data.get("items", [])

        results = []

        for product in items:
            if product.get("sku") == sku:
                results.append({
                    "type": "parent",
                    "product_id": product.get("id"),
                    "name": product.get("name"),
                    "sku": product.get("sku")
                })

            for variant in product.get("combinations", []):
                if variant.get("sku") == sku:
                    results.append({
                        "type": "variant",
                        "product_id": product.get("id"),
                        "name": product.get("name"),
                        "sku": variant.get("sku"),
                        "options": variant.get("options")
                    })

        return {
            "status": "ok",
            "sku": sku,
            "count": len(results),
            "results": results
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }


@app.get("/sync-product/{product_id}")
def sync_product(product_id: int):
    """
    Test synchro Ecwid -> Odoo.
    Ne modifie rien.
    """

    try:
        uid, models = get_odoo()

        url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/products/{product_id}"
        headers = {
            "Authorization": f"Bearer {ECWID_TOKEN}"
        }

        response = requests.get(url, headers=headers)
        product = response.json()

        product_name = product.get("name")
        variants = product.get("combinations", [])

        templates = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "product.template",
            "search_read",
            [[
                ["name", "=", product_name]
            ]],
            {
                "fields": ["id", "name", "product_variant_ids"],
                "limit": 1
            }
        )

        if not templates:
            return {
                "status": "not_found",
                "product": product_name,
                "message": "Produit non trouvé dans Odoo"
            }

        template = templates[0]

        return {
            "status": "ok",
            "product": product_name,
            "template_id": template["id"],
            "variants_found": len(variants),
            "variants": [
                {
                    "variant": v.get("options", [{}])[0].get("value"),
                    "sku": v.get("sku")
                }
                for v in variants
            ]
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }


@app.get("/apply-sync-product/{product_id}")
def apply_sync_product(product_id: int):
    """
    Applique la synchro Ecwid -> Odoo.
    Met à jour les SKU des variantes si nécessaire.
    """

    try:
        uid, models = get_odoo()

        url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/products/{product_id}"
        headers = {
            "Authorization": f"Bearer {ECWID_TOKEN}"
        }

        response = requests.get(url, headers=headers)
        product = response.json()

        product_name = product.get("name")
        ecwid_variants = product.get("combinations", [])

        templates = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "product.template",
            "search_read",
            [[
                ["name", "=", product_name]
            ]],
            {
                "fields": ["id", "name", "product_variant_ids"],
                "limit": 1
            }
        )

        if not templates:
            return {
                "status": "not_found",
                "product": product_name,
                "message": "Produit non trouvé dans Odoo"
            }

        template = templates[0]
        variant_ids = template.get("product_variant_ids", [])

        odoo_variants = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "product.product",
            "read",
            [variant_ids],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "product_template_attribute_value_ids"
                ]
            }
        )

        updated = []

        for ecwid_variant in ecwid_variants:
            ecwid_sku = ecwid_variant.get("sku")
            options = ecwid_variant.get("options", [])

            if not ecwid_sku or not options:
                continue

            ecwid_option_value = options[0].get("value")

            for odoo_variant in odoo_variants:
                if odoo_variant.get("default_code") == ecwid_sku:
                    continue

                # Sécurité : pour l’instant on ne force pas de matching complexe.
                # On évite d’écrire au mauvais variant.
                pass

        return {
            "status": "ok",
            "product": product_name,
            "template_id": template["id"],
            "updated_count": len(updated),
            "updated": updated
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }
