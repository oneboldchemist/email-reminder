from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr
import os, httpx

app   = FastAPI()
SHOP  = os.getenv("SHOPIFY_SHOP")          # din-butik.myshopify.com
TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN")   # Admin API access‑token

class Payload(BaseModel):
    email: EmailStr
    tags:  str
    note:  str | None = None

async def gql(q: str, v: dict):
    url = f"https://{SHOP}/admin/api/2025-07/graphql.json"
    headers = {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(url, json={"query": q, "variables": v}, timeout=15)
        r.raise_for_status()
        return r.json()

@app.post("/back-in-stock-customer")
async def bis(data: Payload):
    # sök kund
    hit = await gql("""query($q:String!){customers(first:1,query:$q){edges{node{id}}}}""",
                    {"q": f"email:{data.email}"})
    edge = hit["data"]["customers"]["edges"]
    if edge:  # kund finns
        cid = edge[0]["node"]["id"]
        # lägg taggar
        await gql("""mutation($id:ID!,$tags:[String!]!){
                       tagsAdd(id:$id,tags:$tags){userErrors{message}}}""",
                  {"id": cid, "tags": data.tags.split(",")})
        # sätt SUBSCRIBED
        await gql("""mutation($id:ID!){
                       customerUpdate(id:$id,
                         input:{emailMarketingConsent:{state:SUBSCRIBED}}){
                         userErrors{message}}}""",
                  {"id": cid})
        return {"updated": True}

    # skapa kund
    resp = await gql("""mutation($in:CustomerInput!){
                          customerCreate(input:$in){
                            customer{id} userErrors{message}}}""",
                     {"in": {
                        "email": data.email,
                        "tags": data.tags.split(","),
                        "note": data.note,
                        "emailMarketingConsent": {"state": "SUBSCRIBED"}
                     }})
    err = resp["data"]["customerCreate"]["userErrors"]
    if err:
        raise HTTPException(500, err[0]["message"])
    return {"created": True}
