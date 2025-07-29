# main.py  – Back‑in‑stock proxy  (Python 3.11 / FastAPI 0.111)
# ------------------------------------------------------------
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr
import os, httpx

# === Miljövariabler ========================================================
SHOP  = os.getenv("SHOPIFY_SHOP", "").strip()          # ex: "din-butik.myshopify.com"
TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()   # Admin‑API access token (shpat_***)

if not SHOP or not TOKEN:
    raise RuntimeError("SHOPIFY_SHOP eller SHOPIFY_ADMIN_TOKEN saknas!")

# === Datamodell för inkommande JSON ========================================
class Payload(BaseModel):
    email: EmailStr
    tags:  str
    note:  str | None = None

# === FastAPI‑instans =======================================================
app = FastAPI(title="Back‑in‑stock customer proxy")

# Hälsokoll – Render visar 404 annars
@app.get("/")
def ping():
    return {"status": "ok"}

# === Hjälpfunktion för GraphQL‑anrop =======================================
async def gql(query: str, variables: dict):
    url = f"https://{SHOP}/admin/api/2025-07/graphql.json"
    headers = {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json={"query": query, "variables": variables})
    # Raise för 4xx/5xx fel -> fångas nedan
    r.raise_for_status()
    return r.json()

# === Huvud‑endpoint ========================================================
@app.post("/back-in-stock-customer")
async def back_in_stock(data: Payload):
    try:
        # 1 – Finns kunden?
        search_q = """
          query($q:String!){customers(first:1,query:$q){edges{node{id}}}}
        """
        res = await gql(search_q, {"q": f"email:{data.email}"})
        hit = res["data"]["customers"]["edges"]

        if hit:
            cid = hit[0]["node"]["id"]

            # 1a – lägg till taggar
            await gql("""mutation($id:ID!,$tags:[String!]!){
                           tagsAdd(id:$id,tags:$tags){userErrors{message}}}""",
                      {"id": cid, "tags": data.tags.split(",")})

            # 1b – sätt SUBSCRIBED
            upd = await gql("""mutation($id:ID!){
                                 customerUpdate(id:$id,
                                   input:{emailMarketingConsent:{state:SUBSCRIBED}}){
                                   userErrors{message}}}""",
                             {"id": cid})
            if errs := upd["data"]["customerUpdate"]["userErrors"]:
                raise HTTPException(500, errs[0]["message"])

            return {"updated": True}

        # 2 – Skapa kund + SUBSCRIBED
        crt = await gql("""mutation($in:CustomerInput!){
                             customerCreate(input:$in){
                               customer{id} userErrors{message}}}""",
                        {"in": {
                           "email": data.email,
                           "tags":  data.tags.split(","),
                           "note":  data.note,
                           "emailMarketingConsent": {"state": "SUBSCRIBED"}
                        }})
        if errs := crt["data"]["customerCreate"]["userErrors"]:
            raise HTTPException(500, errs[0]["message"])

        return {"created": True}

    # Shopify returnerade 401/403 osv.
    except httpx.HTTPStatusError as e:
        detail = f"Shopify error {e.response.status_code}: {e.response.text}"
        raise HTTPException(e.response.status_code, detail)

    # Nätverksfel, timeouts m.m.
    except httpx.RequestError as e:
        raise HTTPException(502, f"HTTPX error: {e}") from e
