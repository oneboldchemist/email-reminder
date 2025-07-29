# main.py – Back‑in‑stock‑proxy (FastAPI + Shopify GraphQL)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
import os, httpx, logging

# ==== Environment =========================================================
SHOP  = os.getenv("SHOPIFY_SHOP", "").strip()
TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2023-07").strip()

if not SHOP or not TOKEN:
    raise RuntimeError(
        "Environment variables SHOPIFY_SHOP and SHOPIFY_ADMIN_TOKEN must be set."
    )

# ==== FastAPI app =========================================================
app = FastAPI(title="Back‑in‑stock customer proxy")

# Allow the browser in your storefront to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # byt gärna mot din butiksdomän för striktare policy
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)

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
    "marketingState": "SUBSCRIBED",           # kunden hamnar direkt som prenumerant
    "marketingOptInLevel": "SINGLE_OPT_IN",   # ingen dubbel opt‑in‑mejl skickas
}

# ==== Main endpoint ========================================================
@app.post("/back-in-stock-customer")
async def back_in_stock(p: Payload):
    """
    Säkerställ att en kund finns, taggas och hamnar direkt som SUBSCRIBED
    utan bekräftelsemejl (single opt‑in).
    """
    try:
        # 1. Leta upp kunden på e‑post
        res = await gql(
            """
            query($query: String!) {
              customers(first: 1, query: $query) {
                edges { node { id tags } }
              }
            }
            """,
            {"query": f"email:{p.email}"},
        )
        edges = res["data"]["customers"]["edges"]
        tags = clean_tags(p.tags)

        # ------------------------------------------------------------------
        # Befintlig kund → uppdatera
        # ------------------------------------------------------------------
        if edges:
            cid = edges[0]["node"]["id"]

            # a. Lägg till taggar
            if tags:
                await gql(
                    """
                    mutation($id: ID!, $tags: [String!]!) {
                      tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
                    }
                    """,
                    {"id": cid, "tags": tags},
                )

            # b. Tvinga prenumeration
            cres = await gql(
                """
                mutation($input: CustomerEmailMarketingConsentUpdateInput!) {
                  customerEmailMarketingConsentUpdate(input: $input) {
                    userErrors { field message }
                  }
                }
                """,
                {
                    "input": {
                        "customerId": cid,
                        "emailMarketingConsent": EMAIL_CONSENT_SUBSCRIBED,
                    }
                },
            )
            if cres["data"]["customerEmailMarketingConsentUpdate"]["userErrors"]:
                msg = cres["data"]["customerEmailMarketingConsentUpdate"]["userErrors"][0]["message"]
                raise HTTPException(500, f"Email consent update failed: {msg}")

            return {"updated": True, "customer_id": cid}

        # ------------------------------------------------------------------
        # Ny kund → skapa
        # ------------------------------------------------------------------
        cust_input: dict[str, object] = {
            "email": p.email,
            "tags": tags,
            "emailMarketingConsent": EMAIL_CONSENT_SUBSCRIBED,
        }
        if p.note:
            cust_input["note"] = p.note

        cres = await gql(
            """
            mutation($input: CustomerInput!) {
              customerCreate(input: $input) {
                customer { id }
                userErrors { field message }
              }
            }
            """,
            {"input": cust_input},
        )
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
