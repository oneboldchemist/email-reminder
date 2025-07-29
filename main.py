# main.py – Back‑in‑stock‑proxy (FastAPI + Shopify GraphQL)
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr
import os, httpx, logging

# ==== Environment =========================================================
SHOP  = os.getenv("SHOPIFY_SHOP", "").strip()
TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2023-07").strip()   # override if you like

if not SHOP or not TOKEN:
    raise RuntimeError(
        "Environment variables SHOPIFY_SHOP and SHOPIFY_ADMIN_TOKEN must be set."
    )

# ==== FastAPI app =========================================================
app = FastAPI(title="Back‑in‑stock customer proxy")

@app.get("/")
def health():
    return {"status": "ok"}

# ==== Request model =======================================================
class Payload(BaseModel):
    email: EmailStr
    tags: str
    note: str | None = None

# ==== GraphQL helper ======================================================
async def gql(query: str, variables: dict[str, object]):
    """Send a GraphQL request to the Admin API and return the JSON body."""
    url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json={"query": query, "variables": variables}, headers=headers)
        if r.status_code != 200:
            raise HTTPException(500, f"Shopify API error {r.status_code}: {r.text}")

        data = r.json()
        if "errors" in data:
            raise HTTPException(500, f"GraphQL error: {data['errors'][0].get('message','?')}")
        return data

    except httpx.TimeoutException:
        raise HTTPException(500, "Request to Shopify API timed out")
    except httpx.RequestError as exc:
        raise HTTPException(500, f"Request error: {str(exc)}")

# ==== Helpers ==============================================================
def clean_tags(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]

EMAIL_CONSENT_SUBSCRIBED = {
    "state": "SUBSCRIBED",
    "optInLevel": "SINGLE_OPT_IN",
}

# ==== Main endpoint ========================================================
@app.post("/back-in-stock-customer")
async def back_in_stock(p: Payload):
    """
    Ensure a Shopify customer exists, is tagged, and is *instantly*
    subscribed to email marketing (single opt‑in = no confirmation email).
    """
    try:
        # 1. Look up by e‑mail
        q = """
        query($query: String!) {
          customers(first: 1, query: $query) {
            edges { node { id tags } }
          }
        }
        """
        res = await gql(q, {"query": f"email:{p.email}"})
        edges = res["data"]["customers"]["edges"]
        tags = clean_tags(p.tags)

        if edges:                     # --- update ---------------------------------
            cid = edges[0]["node"]["id"]

            # a. merge tags
            if tags:
                tag_mut = """
                mutation($id: ID!, $tags: [String!]!) {
                  tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
                }
                """
                await gql(tag_mut, {"id": cid, "tags": tags})

            # b. force subscription (single opt‑in)
            consent_mut = """
            mutation($input: CustomerEmailMarketingConsentUpdateInput!) {
              customerEmailMarketingConsentUpdate(input: $input) {
                customer { id }
                userErrors { field message }
              }
            }
            """
            cres = await gql(
                consent_mut,
                {"input": {"customerId": cid, "emailMarketingConsent": EMAIL_CONSENT_SUBSCRIBED}},
            )
            if cres["data"]["customerEmailMarketingConsentUpdate"]["userErrors"]:
                msg = cres["data"]["customerEmailMarketingConsentUpdate"]["userErrors"][0]["message"]
                raise HTTPException(500, f"Email consent update failed: {msg}")

            return {"updated": True, "customer_id": cid}

        else:                         # --- create --------------------------------
            create_mut = """
            mutation($input: CustomerInput!) {
              customerCreate(input: $input) {
                customer { id email }
                userErrors { field message }
              }
            }
            """
            cust_input: dict[str, object] = {
                "email": p.email,
                "tags": tags,
                "emailMarketingConsent": EMAIL_CONSENT_SUBSCRIBED,
            }
            if p.note:
                cust_input["note"] = p.note

            cres = await gql(create_mut, {"input": cust_input})
            if cres["data"]["customerCreate"]["userErrors"]:
                msg = cres["data"]["customerCreate"]["userErrors"][0]["message"]
                raise HTTPException(500, f"Customer create failed: {msg}")

            cid = cres["data"]["customerCreate"]["customer"]["id"]
            return {"created": True, "customer_id": cid}

    except HTTPException:
        raise
    except Exception as exc:
        logging.exception("Unexpected error")
        raise HTTPException(500, f"Unexpected error: {str(exc)}")
