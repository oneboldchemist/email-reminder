# main.py  – Back‑in‑stock‑proxy (FastAPI + Shopify GraphQL)
# ------------------------------------------
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr
import os, httpx

# ==== Miljövariabler =======================================================
SHOP  = os.getenv("SHOPIFY_SHOP", "").strip()          # 8bc028-b3.myshopify.com
TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()   # shpat_...

if not SHOP or not TOKEN:
    raise RuntimeError("Miljövariablerna SHOPIFY_SHOP och SHOPIFY_ADMIN_TOKEN måste vara satta.")

# ==== FastAPI‑app ===========================================================
app = FastAPI(title="Back‑in‑stock customer proxy")

@app.get("/")                            # enkel “ping”
def health():
    return {"status": "ok"}

# ==== Inkommande payload ====================================================
class Payload(BaseModel):
    email: EmailStr
    tags:  str
    note:  str | None = None

# ==== Helper for GraphQL calls =============================================
async def gql(query: str, variables: dict):
    url = f"https://{SHOP}/admin/api/2025-07/graphql.json"
    headers = {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json={"query": query, "variables": variables}, headers=headers)
    resp.raise_for_status()
    return resp.json()

# ==== Huvud‑endpoint ========================================================
@app.post("/back-in-stock-customer")
async def back_in_stock(data: Payload):
    # 1 · Finns kunden?
    res = await gql(
        """query($q:String!){
             customers(first:1, query:$q){edges{node{id}}}}""",
        {"q": f"email:{data.email}"}
    )
    edges = res["data"]["customers"]["edges"]

    if edges:  # ----- uppdatera befintlig kund ------------------------------
        cid = edges[0]["node"]["id"]

        # a) lägg till taggar
        await gql(
            """mutation($id:ID!,$tags:[String!]!){
                 tagsAdd(id:$id, tags:$tags){userErrors{message}}}""",
            {"id": cid, "tags": data.tags.split(",")}
        )

        # b) sätt SUBSCRIBED (om inte redan)
        upd = await gql(
            """mutation($id:ID!){
                 customerUpdate(id:$id, input:{
                   emailMarketingConsent:{state:SUBSCRIBED}
                 }){userErrors{message}}}""",
            {"id": cid}
        )
        if errs := upd["data"]["customerUpdate"]["userErrors"]:
            raise HTTPException(500, errs[0]["message"])

        return {"updated": True}

    # ----- skapa ny kund ----------------------------------------------------
    crt = await gql(
        """mutation($input:CustomerInput!){
             customerCreate(input:$input){
               customer{id} userErrors{message}}}""",
        {"input": {
            "email": data.email,
            "tags":  data.tags.split(","),
            "note":  data.note,
            "emailMarketingConsent": {"state": "SUBSCRIBED"}
        }}
    )
    if errs := crt["data"]["customerCreate"]["userErrors"]:
        raise HTTPException(500, errs[0]["message"])

    return {"created": True}
