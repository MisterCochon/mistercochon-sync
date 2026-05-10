@app.get("/odoo/test")
async def test_odoo():
    try:
        url = os.getenv("ODOO_URL")
        db = os.getenv("ODOO_DB")
        username = os.getenv("ODOO_LOGIN")
        password = os.getenv("ODOO_PASSWORD")

        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)

        version = common.version()

        uid = common.authenticate(db, username, password, {})

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
