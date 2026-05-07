import os
import requests
from fastapi import FastAPI, HTTPException

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
