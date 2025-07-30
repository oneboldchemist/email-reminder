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
    variant_gid: str              # t.ex. "gid://shopify/ProductVariant/123456789"
    note: str | None = None       # valfritt kund‑anteckningsfält

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

# ==== Helpers =============================================================
EMAIL_CONSENT_SUBSCRIBED = {
    "marketingState": "SUBSCRIBED",           # kunden hamnar direkt som prenumerant
    "marketingOptInLevel": "SINGLE_OPT_IN",   # ingen dubbel opt‑in‑mejl skickas
}

async def build_backin_tag(variant_gid: str) -> str:
    """
    Hämtar produkt‑handle från en variant‑GID och bygger taggen:
    backin|<handle>|<variant‑id>
    """
    # 1. Slå upp variant → få product.handle
    res = await gql(
        """
        query($id: ID!) {
          node(id: $id) {
            ... on ProductVariant {
              id
              product { handle }
            }
          }
        }
        """,
        {"id": variant_gid},
    )
    node = res["data"]["node"]
    if not node:
        raise HTTPException(400, "Ogiltig variant‑GID")

    handle = node["product"]["handle"]
    variant_id = variant_gid.split("/")[-1]  # numeriska ID:t

    return f"backin|{handle}|{variant_id}"

# ==== Main endpoint ========================================================
@app.post("/back-in-stock-customer")
async def back_in_stock(p: Payload):
    """
    Skapar eller uppdaterar en kund, lägger till backin‑taggen
    och sätter e‑postsamtycke till SUBSCRIBED (single opt‑in).
    """
    try:
        # 0. Bygg taggen för den aktuella varianten
        tag = await build_backin_tag(p.variant_gid)

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

        # ------------------------------------------------------------------
        # Befintlig kund → uppdatera
        # ------------------------------------------------------------------
        if edges:
            cid = edges[0]["node"]["id"]

            # a. Lägg till taggen
            await gql(
                """
                mutation($id: ID!, $tags: [String!]!) {
                  tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
                }
                """,
                {"id": cid, "tags": [tag]},
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
            "tags": [tag],
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
