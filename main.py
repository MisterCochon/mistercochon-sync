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
    if not ODOO_URL or not ODOO_DB or not ODOO_LOGIN or not ODOO_PASSWORD:
        raise Exception("Variables Odoo manquantes")

    common = xmlrpc.client.ServerProxy(
        f"{ODOO_URL}/xmlrpc/2/common",
        allow_none=True
    )

    uid = common.authenticate(
        ODOO_DB,
        ODOO_LOGIN,
        ODOO_PASSWORD,
        {}
    )

    if not uid:
        raise Exception("Connexion Odoo échouée")

    models = xmlrpc.client.ServerProxy(
        f"{ODOO_URL}/xmlrpc/2/object",
        allow_none=True
    )

    return ODOO_DB, uid, ODOO_PASSWORD, models


def ecwid_get(endpoint):
    if not ECWID_STORE_ID or not ECWID_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Variables Ecwid manquantes"
        )

    url = f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/{endpoint}"
    headers = {"Authorization": f"Bearer {ECWID_TOKEN}"}

    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

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
        "search_read",
        [[["default_code", "=", sku]]],
        {
            "fields": ["id", "name", "default_code"],
            "limit": 1
        }
    )


def find_product_by_name(models, db, uid, password, name):
    if not name:
        return []

    return models.execute_kw(
        db,
        uid,
        password,
        "product.product",
        "search_read",
        [[["name", "ilike", name]]],
        {
            "fields": ["id", "name", "default_code"],
            "limit": 1
        }
    )


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "mistercochon-backend"
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
                "fields": ["id", "name", "default_code"],
                "limit": 100
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

        product = find_product_by_sku(
            models,
            db,
            uid,
            password,
            sku
        )

        if not product:
            return {
                "status": "not_found",
                "sku": sku
            }

        return {
            "status": "ok",
            "product": product
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
            "store": data.get("generalInfo", {}).get("storeName"),
            "store_id": ECWID_STORE_ID
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }            "type": type(e).__name__
        }
