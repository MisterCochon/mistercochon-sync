import os
import requests
import xmlrpc.client
from fastapi import FastAPI

app = FastAPI()

VERSION = "2026-05-23-v7-bacon-sku-apply"

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_LOGIN = os.getenv("ODOO_LOGIN")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

ECWID_STORE_ID = os.getenv("ECWID_STORE_ID")
ECWID_TOKEN = os.getenv("ECWID_TOKEN")


def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_LOGIN, ODOO_PASSWORD, {})

    if not uid:
        raise Exception("Connexion Odoo échouée")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def odoo_execute(model, method, args=None, kwargs=None):
    uid, models = odoo_connect()
    return models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        model,
        method,
        args or [],
        kwargs or {}
    )


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
