# probe.py  – enda syfte: slå en simpel { shop { name } }‑query
import os, httpx, sys, asyncio, json
SHOP  = os.getenv("SHOPIFY_SHOP", "").strip()
TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()
print("SHOP =", SHOP)
print("TOKEN prefix =", TOKEN[:12], "len =", len(TOKEN))

async def main():
    url = f"https://{SHOP}/admin/api/2025-07/graphql.json"
    q   = {"query":"{ shop { name } }"}
    h   = {"X-Shopify-Access-Token":TOKEN,"Content-Type":"application/json"}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json=q, headers=h)
    print("Status:", r.status_code)
    print("Body  :", r.text)
    r.raise_for_status()

asyncio.run(main())
