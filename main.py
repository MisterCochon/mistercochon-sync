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


def find_product_by_name(models, db, uid, password, name):
    if not name:
        return []

    return models.execute_kw(
        db,
        uid,
        password,
        "product.product",
        "search",
        [[["name", "ilike", name]]],
        {"limit": 1},
    )


def resolve_ecwid_item(models, db, uid, password, item):
    sku = clean_sku(item.get("sku"))
    name = item.get("name", "")
    product_id = item.get("productId")
    variation_id = item.get("variationId")

    # 1. Match SKU exact
    product_ids = find_product_by_sku(models, db, uid, password, sku)
    if product_ids:
        return product_ids[0], "sku"

    # 2. Fallback nom produit
    product_ids = find_product_by_name(models, db, uid, password, name)
    if product_ids:
        return product_ids[0], "name_fallback"

    # 3. Rien trouvé
    return None, {
        "sku": sku,
        "name": name,
        "ecwid_product_id": product_id,
        "ecwid_variation_id": variation_id,
        "quantity": item.get("quantity", 1),
    }


@app.get("/")
async def root():
    return {"message": "MisterCochon API running"}


@app.get("/ecwid/products")
async def get_ecwid_products():
    return ecwid_get("products")


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


@app.get("/ecwid/import-products-to-odoo")
async def import_products_to_odoo():
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

            existing = models.execute_kw(
                db,
                uid,
                password,
                "product.template",
                "search",
                [[["default_code", "=", sku]]],
                {"limit": 1},
            )

            values = {
                "name": name,
                "default_code": sku,
                "list_price": price,
                "sale_ok": True,
                "purchase_ok": True,
            }

            if existing:
                models.execute_kw(
                    db,
                    uid,
                    password,
                    "product.template",
                    "write",
                    [existing, values],
                )

                imported.append({
                    "sku": sku,
                    "name": name,
                    "status": "updated",
                })

            else:
                new_id = models.execute_kw(
                    db,
                    uid,
                    password,
                    "product.template",
                    "create",
                    [values],
                )

                imported.append({
                    "sku": sku,
                    "name": name,
                    "status": "created",
                    "id": new_id,
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

                resolved, method_or_error = resolve_ecwid_item(
                    models,
                    db,
                    uid,
                    password,
                    item,
                )

                if not resolved:
                    error = method_or_error
                    error["order"] = ecwid_order_id
                    order_unresolved.append(error)
                    unresolved_items.append(error)
                    continue

                product_id = resolved
                match_method = method_or_error

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
