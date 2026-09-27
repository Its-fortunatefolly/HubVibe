"""Shopify storefront cart, keyless: the store's own product JSON, a cart
session, and its shipping-rate quote for a destination.

Every Shopify store answers `/products/<handle>.js` (variants with ids,
prices, availability), `POST /cart/add.js` (a cookie-bound cart),
`GET /cart.js` (totals, currency) and `GET /cart/shipping_rates.json`
(the shipping options the store offers to an address, or a 422 naming
what is missing or unsupported). Verified 2026-09-27 on two stores. No
order is ever created: a cart is a quote, and the cookie is dropped after
the call.
"""

import json
import os
import re
from typing import Optional
from urllib.parse import urlparse, urlunparse

import httpx

from .. import runtime
from . import web

_TIMEOUT = float(os.environ.get("WORKER_SHOPIFY_TIMEOUT_SECONDS", "30"))
USER_AGENT = os.environ.get("WORKER_SHOPIFY_USER_AGENT",
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 HubVibe-worker/1.0")
_PRODUCT_PATH = re.compile(r"^(?:/[a-z]{2}(?:-[a-z]{2})?)?(?:/collections/[^/]+)?/products/([A-Za-z0-9._-]+)/?$")


def product_js_url(url: str) -> Optional[str]:
    """`/products/<handle>.js` on the same host for a product page URL."""
    parsed = urlparse(url)
    match = _PRODUCT_PATH.match(parsed.path)
    if not match:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, f"/products/{match.group(1)}.js", "", "", ""))


def store_base(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _guard(url: str) -> None:
    problem = web.target_problem(url)
    if problem:
        raise runtime.InvalidRequest(f"`url` {problem}.")


def _json(response, what: str):
    try:
        return response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"The store answered {what} without JSON (HTTP {response.status_code}).") from None


class _ShopifyCart:
    id = "shopify-cart-api"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def product(self, url: str) -> runtime.ProviderResult:
        """The product's variants from the store's own JSON, or a
        PermanentProviderError when the URL is not a Shopify product page."""
        js = product_js_url(url)
        if not js:
            raise runtime.PermanentProviderError(
                "The URL is not a store product page (/products/<handle>); only Shopify storefronts expose a cart.")
        _guard(js)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
                response = await client.get(js, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"The store timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"The store is unreachable: {exc}") from exc
        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(f"The store answered {response.status_code} for the product JSON.")
        if response.status_code != 200:
            raise runtime.PermanentProviderError(
                f"The store did not serve product JSON (HTTP {response.status_code}); this is not a Shopify storefront.")
        try:
            product = response.json()  # served as application/javascript by many stores; the body is JSON
        except ValueError:
            raise runtime.PermanentProviderError("The store did not serve product JSON; this is not a Shopify storefront.") from None
        if not isinstance(product, dict) or not isinstance(product.get("variants"), list):
            raise runtime.InvalidProviderResponse("The product JSON has no variants.")
        return runtime.ProviderResult(value=product, cost_micros=0, cost_measured=True,
                                      usage=f"variants={len(product['variants'])}")

    async def quote(self, product_url: str, variant_id: int, quantity: int, ship_to: dict) -> runtime.ProviderResult:
        """Add the variant to a fresh cart, read the cart, ask for rates."""
        base = store_base(product_url)
        _guard(base + "/cart/add.js")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        address = {"shipping_address[country]": ship_to.get("country") or ""}
        if ship_to.get("province"):
            address["shipping_address[province]"] = ship_to["province"]
        if ship_to.get("postal_code"):
            address["shipping_address[zip]"] = ship_to["postal_code"]
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
                add = await client.post(base + "/cart/add.js", headers={**headers, "Content-Type": "application/json"},
                                        content=json.dumps({"items": [{"id": int(variant_id), "quantity": int(quantity)}]}))
                if add.status_code in (429, 500, 502, 503, 504):
                    raise runtime.TransientProviderError(f"The store answered {add.status_code} when adding to the cart.")
                add_body = _json(add, "the cart add")
                if add.status_code != 200:
                    message = add_body.get("description") or add_body.get("message") or add_body if isinstance(add_body, dict) else add_body
                    return runtime.ProviderResult(value={"added": False, "add_error": str(message)[:300], "item": None,
                                                         "cart": None, "rates": None, "errors": None, "rates_status": None},
                                                  cost_micros=0, cost_measured=True, usage=f"add={add.status_code}")
                item = (add_body.get("items") or [None])[0] if isinstance(add_body, dict) else None
                cart = _json(await client.get(base + "/cart.js", headers=headers), "the cart")
                rates_response = await client.get(base + "/cart/shipping_rates.json", params=address, headers=headers)
                if rates_response.status_code in (429, 500, 502, 503, 504):
                    raise runtime.TransientProviderError(f"The store answered {rates_response.status_code} for shipping rates.")
                rates_body = _json(rates_response, "the shipping rates")
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"The store timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"The store is unreachable: {exc}") from exc
        rates, errors = None, None
        if rates_response.status_code == 200 and isinstance(rates_body, dict):
            rates = [r for r in (rates_body.get("shipping_rates") or []) if isinstance(r, dict)]
        elif isinstance(rates_body, dict):
            errors = {k: [str(m) for m in (v if isinstance(v, list) else [v])] for k, v in rates_body.items()}
        return runtime.ProviderResult(
            value={"added": True, "add_error": None, "item": item if isinstance(item, dict) else None,
                   "cart": cart if isinstance(cart, dict) else None, "rates": rates, "errors": errors,
                   "rates_status": rates_response.status_code},
            cost_micros=0, cost_measured=True,
            usage=f"add=200 rates={rates_response.status_code} options={len(rates or [])}")


PROVIDERS = [_ShopifyCart()]
