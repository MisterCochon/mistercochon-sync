import xmlrpc.client
import os

@app.get("/odoo/test")
async def test_odoo():

    url = os.getenv("ODOO_URL")
    db = os.getenv("ODOO_DB")
    username = os.getenv("ODOO_LOGIN")
    password = os.getenv("ODOO_PASSWORD")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")

    uid = common.authenticate(db, username, password, {})

    if uid:
        return {"status": "connected", "uid": uid}

    return {"status": "failed"}
