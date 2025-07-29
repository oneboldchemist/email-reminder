# main.py – Back-in-stock-proxy (FastAPI + Shopify GraphQL)
# ------------------------------------------
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr
import os, httpx

# ==== Environment Variables ===============================================
SHOP  = os.getenv("SHOPIFY_SHOP", "").strip()          # your-shop.myshopify.com
TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()   # shpat_...

if not SHOP or not TOKEN:
    raise RuntimeError("Environment variables SHOPIFY_SHOP and SHOPIFY_ADMIN_TOKEN must be set.")

# ==== FastAPI app =========================================================
app = FastAPI(title="Back-in-stock customer proxy")

@app.get("/")
def health():
    return {"status": "ok"}

# ==== Incoming payload ===================================================
class Payload(BaseModel):
    email: EmailStr
    tags:  str
    note:  str | None = None

# ==== Helper for GraphQL calls ===========================================
async def gql(query: str, variables: dict):
    url = f"https://{SHOP}/admin/api/2023-07/graphql.json"
    headers = {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json={"query": query, "variables": variables}, headers=headers)
    resp.raise_for_status()
    return resp.json()

# ==== Main endpoint =======================================================
@app.post("/back-in-stock-customer")
async def back_in_stock(data: Payload):
    # 1. Check if customer exists
    res = await gql(
        """query($q:String!){
             customers(first:1, query:$q){edges{node{id}}}}""",
        {"q": f"email:{data.email}"}
    )
    edges = res["data"]["customers"]["edges"]

    if edges:  # Update existing customer
        cid = edges[0]["node"]["id"]

        # Add tags
        await gql(
            """mutation($id:ID!,$tags:[String!]!){
                 tagsAdd(id:$id, tags:$tags){userErrors{message}}}""",
            {"id": cid, "tags": data.tags.split(",")}
        )

        # Set SUBSCRIBED (if not already)
        upd = await gql(
            """mutation($id:ID!){
                 customerUpdate(id:$id, input:{
                   emailMarketingConsent:{
                     state:SUBSCRIBED, 
                     marketingOptInLevel:SINGLE_OPT_IN
                   }
                 }){userErrors{message}}}""",
            {"id": cid}
        )
        if errs := upd["data"]["customerUpdate"]["userErrors"]:
            raise HTTPException(500, errs[0]["message"])

        return {"updated": True}

    # Create new customer
    crt = await gql(
        """mutation($input:CustomerInput!){
             customerCreate(input:$input){
               customer{id} userErrors{message}}}""",
        {"input": {
            "email": data.email,
            "tags":  data.tags.split(","),
            "note":  data.note,
            "emailMarketingConsent": {
                "state": "SUBSCRIBED",
                "marketingOptInLevel": "SINGLE_OPT_IN"
            }
        }}
    )
    if errs := crt["data"]["customerCreate"]["userErrors"]:
        raise HTTPException(500, errs[0]["message"])

    return {"created": True}
