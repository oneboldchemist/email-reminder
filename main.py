# main.py – Back-in-stock-proxy (FastAPI + Shopify GraphQL)
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr
import os, httpx
import logging

# ==== Environment Variables =============================================
SHOP = os.getenv("SHOPIFY_SHOP", "").strip()
TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()

if not SHOP or not TOKEN:
    raise RuntimeError("Environment variables SHOPIFY_SHOP and SHOPIFY_ADMIN_TOKEN must be set.")

# ==== FastAPI App ===================================================
app = FastAPI(title="Back-in-stock customer proxy")

@app.get("/")
def health():
    return {"status": "ok"}

# ==== Request Models ================================================
class Payload(BaseModel):
    email: EmailStr
    tags: str
    note: str | None = None

# ==== GraphQL Helper ================================================
async def gql(query: str, variables: dict):
    # Fixed: Use 2023-07 API version
    base_url = f"https://{SHOP}/admin/api/2023-07"
    url = f"{base_url}/graphql.json"
    
    headers = {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "query": query,
        "variables": variables
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            
        # Check HTTP status
        if resp.status_code != 200:
            raise HTTPException(500, f"Shopify API error: {resp.status_code} - {resp.text}")
            
        result = resp.json()
        
        # Check for GraphQL errors
        if "errors" in result:
            error_msg = result["errors"][0].get("message", "Unknown GraphQL error")
            raise HTTPException(500, f"GraphQL error: {error_msg}")
            
        return result
        
    except httpx.TimeoutException:
        raise HTTPException(500, "Request to Shopify API timed out")
    except httpx.RequestError as e:
        raise HTTPException(500, f"Request error: {str(e)}")

# ==== Main Endpoint =================================================
@app.post("/back-in-stock-customer")
async def back_in_stock(data: Payload):
    try:
        # 1. Check if customer exists
        customer_query = """
        query($query: String!) {
            customers(first: 1, query: $query) {
                edges {
                    node {
                        id
                        tags
                    }
                }
            }
        }
        """
        
        res = await gql(customer_query, {"query": f"email:{data.email}"})
        edges = res["data"]["customers"]["edges"]
        
        if edges:  # Update existing customer
            customer_id = edges[0]["node"]["id"]
            
            # Add tags
            tags_mutation = """
            mutation($id: ID!, $tags: [String!]!) {
                tagsAdd(id: $id, tags: $tags) {
                    userErrors {
                        field
                        message
                    }
                }
            }
            """
            
            tag_list = [tag.strip() for tag in data.tags.split(",") if tag.strip()]
            await gql(tags_mutation, {"id": customer_id, "tags": tag_list})
            
            # Update email marketing consent
            update_mutation = """
            mutation($id: ID!) {
                customerUpdate(id: $id, input: {
                    emailMarketingConsent: {
                        marketingState: SUBSCRIBED
                    }
                }) {
                    customer {
                        id
                    }
                    userErrors {
                        field
                        message
                    }
                }
            }
            """
            
            update_result = await gql(update_mutation, {"id": customer_id})
            
            if update_result["data"]["customerUpdate"]["userErrors"]:
                error_msg = update_result["data"]["customerUpdate"]["userErrors"][0]["message"]
                raise HTTPException(500, f"Failed to update customer: {error_msg}")
                
            return {"updated": True, "customer_id": customer_id}
            
        else:  # Create new customer
            create_mutation = """
            mutation($input: CustomerInput!) {
                customerCreate(input: $input) {
                    customer {
                        id
                        email
                    }
                    userErrors {
                        field
                        message
                    }
                }
            }
            """
            
            tag_list = [tag.strip() for tag in data.tags.split(",") if tag.strip()]
            
            customer_input = {
                "email": data.email,
                "tags": tag_list,
                "emailMarketingConsent": {
                    "marketingState": "SUBSCRIBED"
                }
            }
            
            if data.note:
                customer_input["note"] = data.note
                
            create_result = await gql(create_mutation, {"input": customer_input})
            
            if create_result["data"]["customerCreate"]["userErrors"]:
                error_msg = create_result["data"]["customerCreate"]["userErrors"][0]["message"]
                raise HTTPException(500, f"Failed to create customer: {error_msg}")
                
            customer_id = create_result["data"]["customerCreate"]["customer"]["id"]
            return {"created": True, "customer_id": customer_id}
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Unexpected error: {str(e)}")
