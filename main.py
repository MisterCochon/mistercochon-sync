import os
import xmlrpc.client
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "mistercochon-backend"
    }


@app.get("/odoo/test")
async def test_odoo():
    try:
        url = os.getenv("ODOO_URL")
        db = os.getenv("ODOO_DB")
        username = os.getenv("ODOO_LOGIN")
        password = os.getenv("ODOO_PASSWORD")

        common = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/common",
            allow_none=True
        )

        version = common.version()

        uid = common.authenticate(
            db,
            username,
            password,
            {}
        )

        return {
            "status": "connected" if uid else "auth_failed",
            "url": url,
            "db": db,
            "login": username,
            "password_present": bool(password),
            "odoo_version": version,
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
