from fastapi import FastAPI, HTTPException
import requests
import os
import xmlrpc.client

app = FastAPI()

ECWID_STORE_ID = os.getenv("ECWID_STORE_ID")
ECWID_TOKEN = os.getenv("ECWID_TOKEN")


def get_odoo():
    url = os.getenv("ODOO_URL")
    db = os.getenv("ODOO_DB")
    username = os.getenv("ODOO_LOGIN")
    password = os.getenv("ODOO_PASSWORD")

    if not url or not db or not username or not password:
        raise Exception("Variables Odoo manquantes")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(db, username, password, {})

    if not uid:
        raise Exception("Connexion Odoo échouée")

    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    return db, uid, password, models


def ecwid_get(endpoint):
    if not ECWID_STORE_ID or not ECWID_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="ECWID_STORE_ID ou ECWID_TOKEN manquant dans Render",
        )

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


def find_product_by_sku(models, db, uid, password, sku):
    sku = clean_sku(sku)

    if not sku:
        return []

    return models.execute_kw(
        db,
        uid,
        password,
        "product.product",
        "search",
        [[["default_code", "=", sku]]],
        {"limit": 1},
    )


def resolve_ecwid_item(models, db, uid, password, item):
    sku = clean_sku(item.get("sku"))

    if not sku:
        return None, {
            "reason": "missing_sku",
            "sku": "",
            "name": item.get("name", ""),
            "ecwid_product_id": item.get("productId"),
            "ecwid_variation_id": item.get("variationId"),
            "quantity": item.get("quantity", 1),
        }

    product_ids = find_product_by_sku(models, db, uid, password, sku)

    if product_ids:
        return product_ids[0], "sku"

    return None, {
        "reason": "sku_not_found_in_odoo",
        "sku": sku,
        "name": item.get("name", ""),
        "ecwid_product_id": item.get("productId"),
        "ecwid_variation_id": item.get("variationId"),
        "quantity": item.get("quantity", 1),
    }


@app.get("/")
async def root():
    return {"message": "MisterCochon API running"}


@app.get("/odoo/test")
async def test_odoo():
    try:
        db, uid, password, models = get_odoo()
        return {
            "status": "connected",
            "uid": uid,
            "db": db,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__,
        }


@app.get("/ecwid/products")
async def get_ecwid_products():
    return ecwid_get("products")


@app.get("/ecwid/import-products-to-odoo")
async def import_products_to_odoo():
    """
    Import simple des produits Ecwid vers Odoo.

    IMPORTANT :
    - Les produits simples Ecwid sont créés/mis à jour dans Odoo.
    - Le SKU est écrit sur product.product, pas seulement product.template.
    - Les produits sans SKU sont ignorés.
    """

    try:
        ecwid_products = ecwid_get("products").get("items", [])
        db, uid, password, models = get_odoo()

        imported = []
        skipped = []

        for product in ecwid_products:
            name = product.get("name")
            sku = clean_sku(product.get("sku"))
            price = product.get("price", 0)
            ecwid_product_id = product.get("id")

            if not sku:
                skipped.append({
                    "ecwid_product_id": ecwid_product_id,
                    "name": name,
                    "reason": "missing_sku",
                })
                continue

            product_ids = find_product_by_sku(models, db, uid, password, sku)

            values_template = {
                "name": name,
                "list_price": price,
                "sale_ok": True,
                "purchase_ok": True,
            }

            if product_ids:
                product_product_id = product_ids[0]

                product_data = models.execute_kw(
                    db,
                    uid,
                    password,
                    "product.product",
                    "read",
                    [product_product_id],
                    {"fields": ["product_tmpl_id"]},
                )[0]

                template_id = product_data["product_tmpl_id"][0]

                models.execute_kw(
                    db,
                    uid,
                    password,
                    "product.template",
                    "write",
                    [[template_id], values_template],
                )

                models.execute_kw(
                    db,
                    uid,
                    password,
                    "product.product",
                    "write",
                    [[product_product_id], {
                        "default_code": sku,
                    }],
                )

                imported.append({
                    "sku": sku,
                    "name": name,
                    "status": "updated",
                    "product_product_id": product_product_id,
                    "product_template_id": template_id,
                })

            else:
                template_id = models.execute_kw(
                    db,
                    uid,
                    password,
                    "product.template",
                    "create",
                    [{
                        **values_template,
                        "default_code": sku,
                    }],
                )

                product_product_ids = models.execute_kw(
                    db,
                    uid,
                    password,
                    "product.product",
                    "search",
                    [[["product_tmpl_id", "=", template_id]]],
                    {"limit": 1},
                )

                if product_product_ids:
                    product_product_id = product_product_ids[0]

                    models.execute_kw(
                        db,
                        uid,
                        password,
                        "product.product",
                        "write",
                        [[product_product_id], {
                            "default_code": sku,
                        }],
                    )
                else:
                    product_product_id = None

                imported.append({
                    "sku": sku,
                    "name": name,
                    "status": "created",
                    "product_template_id": template_id,
                    "product_product_id": product_product_id,
                })

        return {
            "status": "success",
            "imported_count": len(imported),
            "skipped_count": len(skipped),
            "products": imported,
            "skipped": skipped,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__,
        }


@app.get("/ecwid/import-orders-to-odoo")
async def import_orders_to_odoo():
    try:
        orders = ecwid_get("orders").get("items", [])

        db, uid, password, models = get_odoo()

        imported_orders = []
        unresolved_items = []

        for order in orders:
            ecwid_order_id = str(order.get("id"))

            billing = order.get("billingPerson") or {}
            customer_name = billing.get("name") or "Ecwid Customer"
            email = order.get("email") or billing.get("email") or ""

            partner_ids = []

            if email:
                partner_ids = models.execute_kw(
                    db,
                    uid,
                    password,
                    "res.partner",
                    "search",
                    [[["email", "=", email]]],
                    {"limit": 1},
                )

            if partner_ids:
                partner_id = partner_ids[0]
            else:
                partner_id = models.execute_kw(
                    db,
                    uid,
                    password,
                    "res.partner",
                    "create",
                    [{
                        "name": customer_name,
                        "email": email,
                    }],
                )

            existing_order = models.execute_kw(
                db,
                uid,
                password,
                "sale.order",
                "search",
                [[["client_order_ref", "=", ecwid_order_id]]],
                {"limit": 1},
            )

            if existing_order:
                imported_orders.append({
                    "order": ecwid_order_id,
                    "status": "already_exists",
                })
                continue

            order_lines = []
            order_unresolved = []

            for item in order.get("items", []):
                quantity = item.get("quantity", 1)
                price = item.get("price", 0)

                product_id, method_or_error = resolve_ecwid_item(
                    models,
                    db,
                    uid,
                    password,
                    item,
                )

                if not product_id:
                    error = method_or_error
                    error["order"] = ecwid_order_id
                    order_unresolved.append(error)
                    unresolved_items.append(error)
                    continue

                order_lines.append(
                    (0, 0, {
                        "product_id": product_id,
                        "product_uom_qty": quantity,
                        "price_unit": price,
                    })
                )

            if not order_lines:
                imported_orders.append({
                    "order": ecwid_order_id,
                    "status": "skipped_no_matching_products",
                    "unresolved": order_unresolved,
                })
                continue

            sale_order_id = models.execute_kw(
                db,
                uid,
                password,
                "sale.order",
                "create",
                [{
                    "partner_id": partner_id,
                    "client_order_ref": ecwid_order_id,
                    "order_line": order_lines,
                }],
            )

            imported_orders.append({
                "order": ecwid_order_id,
                "status": "created",
                "id": sale_order_id,
                "unresolved_count": len(order_unresolved),
                "unresolved": order_unresolved,
            })

        return {
            "status": "success",
            "orders": imported_orders,
            "unresolved_count": len(unresolved_items),
            "unresolved_items": unresolved_items,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__,
        }


@app.get("/ecwid/check-unresolved-skus")
async def check_unresolved_skus():
    try:
        orders = ecwid_get("orders").get("items", [])

        db, uid, password, models = get_odoo()

        unresolved = []
        resolved = []

        for order in orders:
            ecwid_order_id = str(order.get("id"))

            for item in order.get("items", []):
                product_id, method_or_error = resolve_ecwid_item(
                    models,
                    db,
                    uid,
                    password,
                    item,
                )

                if product_id:
                    resolved.append({
                        "order": ecwid_order_id,
                        "sku": clean_sku(item.get("sku")),
                        "name": item.get("name"),
                        "odoo_product_id": product_id,
                        "match": method_or_error,
                    })
                else:
                    error = method_or_error
                    error["order"] = ecwid_order_id
                    unresolved.append(error)

        return {
            "status": "success",
            "resolved_count": len(resolved),
            "unresolved_count": len(unresolved),
            "unresolved": unresolved,
            "resolved": resolved,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__,
        }
