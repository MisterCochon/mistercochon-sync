import os
import xmlrpc.client
from fastapi import FastAPI

app = FastAPI()


def get_odoo():
    url = os.getenv("ODOO_URL")
    db = os.getenv("ODOO_DB")
    username = os.getenv("ODOO_LOGIN")
    password = os.getenv("ODOO_PASSWORD")

    common = xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/common",
        allow_none=True
    )

    uid = common.authenticate(
        db,
        username,
        password,
        {}
    )

    models = xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/object",
        allow_none=True
    )

    return db, uid, password, models


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "mistercochon-backend"
    }


@app.get("/odoo/test")
async def test_odoo():
    try:
        db, uid, password, models = get_odoo()

        return {
            "status": "connected" if uid else "auth_failed",
            "uid": uid,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__,
        }


@app.get("/odoo/products/check-sku")
async def check_sku():
    try:
        db, uid, password, models = get_odoo()

        products = models.execute_kw(
            db,
            uid,
            password,
            "product.product",
            "search_read",
            [[]],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code"
                ],
                "limit": 100,
            },
        )

        missing_sku = [
            p for p in products
            if not p.get("default_code")
        ]

        return {
            "status": "ok",
            "total_checked": len(products),
            "missing_sku_count": len(missing_sku),
            "missing_sku": missing_sku,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__,
        }


@app.get("/odoo/product/by-sku/{sku}")
async def get_product_by_sku(sku: str):
    try:
        db, uid, password, models = get_odoo()

        product_ids = models.execute_kw(
            db,
            uid,
            password,
            "product.product",
            "search",
            [[["default_code", "=", sku]]],
            {"limit": 1}
        )

        if not product_ids:
            return {
                "status": "not_found",
                "sku": sku
            }

        product = models.execute_kw(
            db,
            uid,
            password,
            "product.product",
            "read",
            [product_ids],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "list_price"
                ]
            }
        )[0]

        return {
            "status": "found",
            "product": product
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__,
        }


@app.get("/odoo/product/search/{term}")
async def search_product(term: str):
    try:
        db, uid, password, models = get_odoo()

        products = models.execute_kw(
            db,
            uid,
            password,
            "product.product",
            "search_read",
            [[
                "|",
                ["name", "ilike", term],
                ["default_code", "ilike", term]
            ]],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code"
                ],
                "limit": 20
            }
        )

        return {
            "status": "ok",
            "count": len(products),
            "products": products
        }@app.get("/odoo-test")
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
            {
                "fields": [
                    "id",
                    "name",
                    "login"
                ]
            }
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
