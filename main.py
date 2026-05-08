from fastapi import FastAPI, HTTPException
import requests
import os
import xmlrpc.client

app = FastAPI()

ECWID_STORE_ID = os.getenv("ECWID_STORE_ID")
ECWID_TOKEN = os.getenv("ECWID_TOKEN")


@app.get("/")
async def root():
    return {"message": "MisterCochon API running"}


@app.get("/ecwid/products")
async def get_ecwid_products():

    if not ECWID_STORE_ID or not ECWID_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="ECWID_STORE_ID ou ECWID_TOKEN manquant dans Render",
        )

    url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/products"

    headers = {
        "Authorization": f"Bearer {ECWID_TOKEN}"
    }

    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text,
        )

    return response.json()


@app.get("/odoo/test")
async def test_odoo():

    try:

        url = os.getenv("ODOO_URL")
        db = os.getenv("ODOO_DB")
        username = os.getenv("ODOO_LOGIN")
        password = os.getenv("ODOO_PASSWORD")

        if not url or not db or not username or not password:
            return {
                "status": "missing_variables",
                "url": bool(url),
                "db": bool(db),
                "username": bool(username),
                "password": bool(password),
            }

        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")

        uid = common.authenticate(db, username, password, {})

        return {
            "status": "connected" if uid else "failed",
            "uid": uid,
            "url": url,
            "db": db,
            "username": username,
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

        # -----------------------------
        # ECWID
        # -----------------------------

        ecwid_url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/products"

        ecwid_headers = {
            "Authorization": f"Bearer {ECWID_TOKEN}"
        }

        ecwid_response = requests.get(
            ecwid_url,
            headers=ecwid_headers
        )

        ecwid_products = ecwid_response.json().get("items", [])

        # -----------------------------
        # ODOO CONNECTION
        # -----------------------------

        url = os.getenv("ODOO_URL")
        db = os.getenv("ODOO_DB")
        username = os.getenv("ODOO_LOGIN")
        password = os.getenv("ODOO_PASSWORD")

        common = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/common"
        )

        uid = common.authenticate(
            db,
            username,
            password,
            {}
        )

        models = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/object"
        )

        imported = []

        # -----------------------------
        # IMPORT PRODUCTS
        # -----------------------------

        for product in ecwid_products:

            name = product.get("name")
            sku = product.get("sku")
            price = product.get("price", 0)

            existing = models.execute_kw(
                db,
                uid,
                password,
                'product.template',
                'search',
                [[['default_code', '=', sku]]]
            )

            values = {
                'name': name,
                'default_code': sku,
                'list_price': price,
            }

            if existing:

                models.execute_kw(
                    db,
                    uid,
                    password,
                    'product.template',
                    'write',
                    [existing, values]
                )

                imported.append({
                    "sku": sku,
                    "status": "updated"
                })

            else:

                new_id = models.execute_kw(
                    db,
                    uid,
                    password,
                    'product.template',
                    'create',
                    [values]
                )

                imported.append({
                    "sku": sku,
                    "status": "created",
                    "id": new_id
                })

        return {
            "status": "success",
            "products": imported
        }

    except Exception as e:

        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__,
        }
