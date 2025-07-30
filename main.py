# main.py – Back‑in‑stock‑proxy (FastAPI + Shopify GraphQL)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, field_validator
import os, httpx, logging, re, asyncio

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # byt gärna mot din butiksdomän
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
    tags: str                 # komma‑separerade taggar – frontenden oförändrad
    note: str | None = None

    # Pydantic v2‑sätt att validera fältet
    @field_validator("tags")
    @classmethod
    def tags_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("tags får inte vara tomt")
        return v

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
    "marketingState": "SUBSCRIBED",
    "marketingOptInLevel": "SINGLE_OPT_IN",
}

GID_RX = re.compile(r"^gid://shopify/ProductVariant/(\d+)$")
NUM_RX = re.compile(r"^\d+$")

async def build_backin_tag(variant_gid: str) -> str:
    """Returnerar backin‑tagg `backin|handle|id` för given variant‑GID."""
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
        raise HTTPException(400, f"Ogiltig variant‑GID: {variant_gid}")

    handle = node["product"]["handle"]
    variant_id = variant_gid.split("/")[-1]
    return f"backin|{handle}|{variant_id}"

async def normalize_tags(raw: str) -> list[str]:
    """
    Tar en komma‑separerad taggsträng och returnerar en lista där varje
    variant‑GID eller siffra ersätts av backin‑taggen.
    """
    result: list[str] = []
    tasks: list[asyncio.Task] = []

    for t in [x.strip() for x in raw.split(",") if x.strip()]:
        if GID_RX.fullmatch(t):
            tasks.append(asyncio.create_task(build_backin_tag(t)))
        elif NUM_RX.fullmatch(t):
            gid = f"gid://shopify/ProductVariant/{t}"
            tasks.append(asyncio.create_task(build_backin_tag(gid)))
        else:
            result.append(t)  # vanlig tagg

    for task in tasks:
        result.append(await task)

    # ta bort dubletter men behåll ordning
    return list(dict.fromkeys(result))

# ==== Main endpoint ========================================================
@app.post("/back-in-stock-customer")
async def back_in_stock(p: Payload):
    """
    Frontend skickar samma sak som alltid:
    { "email": "...", "tags": "...", "note": "..." }

    • Variant‑ID eller GID i `tags` översätts till backin‑tagg
    • Kunden skapas/uppdateras, sätts som SUBSCRIBED
    """
    try:
        tags = await normalize_tags(p.tags)

        # 1. Finns kunden redan?
        res = await gql(
            """
            query($query: String!) {
              customers(first: 1, query: $query) {
                edges { node { id } }
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

            # a. Lägg till taggarna
            if tags:
                await gql(
                    """
                    mutation($id: ID!, $tags: [String!]!) {
                      tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
                    }
                    """,
                    {"id": cid, "tags": tags},
                )

            # b. Sätt SUBSCRIBED
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
