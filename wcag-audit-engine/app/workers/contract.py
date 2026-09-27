"""The machine-readable contract of every worker: what a paid call returns.

One JSON Schema per worker, written from the literal `return {...}` of the
skill that produces it (app/workers/skills/*.py), plus the envelope the
router wraps every result in. Three surfaces read these and must agree:

  * openapi.json     -- the 200 response schema and example of each /work route
  * agent.json       -- `output_schema` on each capability, `response_envelope`
  * the 402's Bazaar record -- `info.output` example and schema

Before this file every one of those said `{"status": "ok"}` or "object; see
`returns`", which told a shopping agent nothing about what it was buying.
The Bazaar ranks on the completeness of the output schema; an agent choosing
between two sellers reads it; a pipeline routing a job checks the keys. So
the keys here are the keys the code emits -- a guard test generates an
example from each schema and validates it, and the live simulation
(scripts/simulate-work-calls.py) can validate delivered results against them.

`examples` on a property is the value the generated example uses. JSON Schema
2020-12 defines the keyword; validators ignore it; OpenAPI 3.1 renders it.

REPRESENTATIVE_QUERIES feeds /.well-known/ard.json: the natural-language
asks a registry builds its semantic index from (ARD spec, "SHOULD contain
2-5 examples").
"""

import logging
from typing import Any, Optional

_log = logging.getLogger("hubvibe.workers.contract")
_validator_missing_logged = False


def check(schema: Optional[dict], value: Any) -> Optional[str]:
    """The delivery contract: does `value` match the output schema this route
    published? None when it does, otherwise one line saying where it does
    not (JSON path and message) -- the caller turns that into an unbilled
    failure, so a paid result can never differ in shape from the schema in
    openapi.json, the MCP outputSchema, the 402's Bazaar record and the A2A
    skill. Pure: no I/O.

    Without a validator the check cannot run; it then reports None once
    with a log line rather than refusing every sale on a packaging error.
    """
    global _validator_missing_logged
    if not schema:
        return None
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover - requirements.txt pins it
        if not _validator_missing_logged:
            _validator_missing_logged = True
            _log.error("jsonschema is not installed: the delivery contract is not being checked")
        return None
    try:
        error = next(iter(sorted(Draft202012Validator(schema).iter_errors(value),
                                 key=lambda e: list(e.absolute_path))), None)
    except Exception as exc:  # a broken schema is our defect, never the buyer's
        _log.error("output schema could not be evaluated: %s", exc)
        return None
    if error is None:
        return None
    where = "/".join(str(p) for p in error.absolute_path) or "(root)"
    return f"{where}: {error.message}"[:300]

# --- schema helpers -----------------------------------------------------------


def _s(description: str, example: Any, nullable: bool = False) -> dict:
    """A string property with the example the docs show."""
    return {"type": ["string", "null"] if nullable else "string",
            "description": description, "examples": [example]}


def _i(description: str, example: int, nullable: bool = False) -> dict:
    return {"type": ["integer", "null"] if nullable else "integer",
            "description": description, "examples": [example]}


def _n(description: str, example: float, nullable: bool = False) -> dict:
    return {"type": ["number", "null"] if nullable else "number",
            "description": description, "examples": [example]}


def _b(description: str, example: bool) -> dict:
    return {"type": "boolean", "description": description, "examples": [example]}


def _const(value: str, description: str) -> dict:
    return {"type": "string", "const": value, "description": description}


def _enum(values: list, description: str, example: str) -> dict:
    return {"type": "string", "enum": values, "description": description, "examples": [example]}


def _obj(properties: dict, required: list, description: str = "", **extra) -> dict:
    schema = {"type": "object", "properties": properties, "required": required}
    if description:
        schema["description"] = description
    schema.update(extra)
    return schema


def _arr(items: dict, description: str, example: list = None) -> dict:
    schema = {"type": "array", "items": items, "description": description}
    if example is not None:
        schema["examples"] = [example]
    return schema


def _free(description: str, example: Any = None, types=None) -> dict:
    """A value whose shape belongs to an upstream provider (a Maps result, a
    raw JSON-RPC result). Described, exemplified, not constrained."""
    schema = {"description": description}
    if types:
        schema["type"] = types
    if example is not None:
        schema["examples"] = [example]
    return schema


# --- shared fragments -----------------------------------------------------------

_MODEL = _s("The model that produced the answer.", "gemini-2.5-flash")
_TOKENS = _obj({"prompt": _i("Prompt tokens billed by the provider.", 312),
                "output": _i("Output tokens billed by the provider.", 88)},
               ["prompt", "output"], "Token usage for the call.")

_CITED_SOURCE = _obj({"n": _i("Citation number used as [n] in the text.", 1),
                      "url": _s("Source URL.", "https://example.com/about"),
                      "title": _s("Page title, when the page had one.", "About Example", nullable=True)},
                     ["n", "url"], "One source the answer cites.")
_UNREAD_SOURCE = _obj({"url": _s("Source that could not be read.", "https://example.com/paywalled"),
                       "reason": _s("Why it could not be read.", "HTTP 403 from the target")},
                      ["url", "reason"], "A source that was found but not read; disclosed, never silently dropped.")

_BQ_COLUMNS = _arr(_s("Column name.", "name"), "Result column names, in order.", ["name", "n"])
_BQ_ROWS = _arr(_obj({}, [], "One row keyed by column name.", additionalProperties=True,
                     examples=[{"name": "James", "n": 4942431}]),
                "Result rows, keyed by column name.", [{"name": "James", "n": 4942431}])
_GIB = _n("Gibibytes BigQuery scanned; the metered cost basis.", 0.012)

_AVAILABILITY = ["in_stock", "out_of_stock", "preorder", "backorder", "limited", "discontinued", "unknown"]


def _b_or_null(description: str, example: bool) -> dict:
    return {"type": ["boolean", "null"], "description": description, "examples": [example]}


_COMMERCE_OPTION = _obj({
    "name": _s("The option as the page names it.", "Natural Black / 10"),
    "available": _b_or_null("Purchasable now; null when the page does not say.", True),
    "availability": _enum(_AVAILABILITY, "Availability of this option.", "in_stock"),
    "price": _n("Price of this option; null when not stated.", 110.0, nullable=True),
    "currency": _s("ISO 4217 code; null when not stated.", "USD", nullable=True),
    "sku": _s("SKU when the page states one.", "WR3MNCW100", nullable=True),
    "url": _s("Option-specific URL when the page gives one.", "https://example.com/products/wool-runner?variant=10", nullable=True),
}, ["name", "available", "availability", "price", "currency"], "One purchasable variation.")

_CHECKED_AT = {"type": "string", "format": "date-time",
               "description": "When this node read the source (UTC). Nothing here is cached.",
               "examples": ["2026-09-26T18:00:00Z"]}
_OHLCV = _obj({
    "date": _s("Trading day (YYYY-MM-DD).", "2026-09-25"),
    "open": _n("Open.", 336.04, nullable=True), "high": _n("High.", 341.67, nullable=True),
    "low": _n("Low.", 334.53, nullable=True), "close": _n("Close.", 341.07, nullable=True),
    "volume": _n("Shares traded.", 30002510, nullable=True),
}, ["date", "close"], "One trading day.")
_XBRL_VALUE = _obj({
    "end": _s("Period end (YYYY-MM-DD).", "2026-06-27"),
    "start": _s("Period start for flows; null for instants (balance-sheet items).", "2026-03-29", nullable=True),
    "value": _n("The reported value, in `unit`.", 109417000000),
    "unit": _s("XBRL unit.", "USD"),
    "fiscal_year": _i("Filer's fiscal year.", 2026, nullable=True),
    "fiscal_period": _s("FY, Q1, Q2 or Q3.", "Q3", nullable=True),
    "form": _s("Form it was reported on.", "10-Q", nullable=True),
    "filed": _s("Filing date.", "2026-07-31", nullable=True),
    "frame": _s("SEC calendar frame when assigned.", "CY2026Q2", nullable=True),
}, ["end", "value", "unit"], "One reported value.")
_XBRL_VALUE_EXAMPLE = {"end": "2026-06-27", "start": "2026-03-29", "value": 109417000000, "unit": "USD",
                       "fiscal_year": 2026, "fiscal_period": "Q3", "form": "10-Q", "filed": "2026-07-31",
                       "frame": "CY2026Q2"}
_XBRL_CONCEPT = _obj({
    "concept": _s("The XBRL concept actually used (an alternate may stand in for the one asked).",
                  "RevenueFromContractWithCustomerExcludingAssessedTax"),
    "taxonomy": _s("us-gaap, ifrs-full or dei.", "us-gaap"),
    "label": _s("The concept's label.", "Revenue from Contract with Customer, Excluding Assessed Tax", nullable=True),
    "unit": _s("Unit of every value.", "USD"),
    "values": _arr(_XBRL_VALUE, ("Reported values, newest period first, one per reported (start, end) period: a "
                                 "quarter and the year-to-date figure that share an end date are separate entries; "
                                 "a restated period shows the latest filing's figure."), [_XBRL_VALUE_EXAMPLE]),
}, ["concept", "taxonomy", "unit", "values"], "One concept's reported values.")
_XBRL_CONCEPT_EXAMPLE = {"concept": "RevenueFromContractWithCustomerExcludingAssessedTax", "taxonomy": "us-gaap",
                         "label": "Revenue from Contract with Customer, Excluding Assessed Tax", "unit": "USD",
                         "values": [_XBRL_VALUE_EXAMPLE]}
_BY_WINDOW = _obj({}, [], "Value per window, keyed by the window length.", additionalProperties={"type": ["number", "null"]},
                  examples=[{"20": 116.4, "50": 110.2}])

_BSKY_POST = _obj({
    "uri": _s("Post AT-URI.", "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.post/3mw2cdr44fc2a"),
    "cid": _s("Content id.", "bafyreicrjbyelww7gvuymagi7g2rirokyrvedwzeh5vflmsvnwhnwjvnyi", nullable=True),
    "url": _s("Web URL of the post.", "https://bsky.app/profile/bsky.app/post/3mw2cdr44fc2a", nullable=True),
    "author_handle": _s("Author handle.", "bsky.app", nullable=True),
    "author_did": _s("Author DID.", "did:plc:z72i7hdynmk6r22z27h6tvur", nullable=True),
    "text": _s("Post text.", "hello world"),
    "created_at": _s("Author's creation time.", "2026-09-26T20:00:00.000Z", nullable=True),
    "indexed_at": _s("AppView indexing time.", "2026-09-26T20:00:00.000Z", nullable=True),
    "likes": _i("Likes.", 90, nullable=True), "reposts": _i("Reposts.", 7, nullable=True),
    "replies": _i("Replies.", 5, nullable=True), "quotes": _i("Quote posts.", 2, nullable=True),
    "langs": _arr(_s("BCP-47 tag.", "en"), "Languages the author declared.", ["en"]),
    "is_repost": _b("Whether this feed item is a repost by the actor.", False),
    "reply_to": _s("AT-URI of the parent post when this is a reply.", None, nullable=True),
    "embed_type": _s("Embed type ($type) when the post carries one.", "app.bsky.embed.images#view", nullable=True),
}, ["uri", "cid", "url", "author_handle", "author_did", "text", "created_at", "indexed_at", "likes", "reposts",
    "replies", "quotes", "langs", "is_repost", "reply_to", "embed_type"], "One Bluesky post.")
_BSKY_PROFILE = _obj({
    "did": _s("DID.", "did:plc:z72i7hdynmk6r22z27h6tvur", nullable=True),
    "handle": _s("Handle.", "bsky.app", nullable=True),
    "display_name": _s("Display name.", "Bluesky", nullable=True),
    "description": _s("Bio.", "official bluesky account", nullable=True),
    "avatar_url": _s("Avatar URL.", "https://cdn.bsky.app/img/avatar/plain/did:plc:z72i7hdynmk6r22z27h6tvur/x@jpeg", nullable=True),
    "followers": _i("Followers.", 4200000, nullable=True), "follows": _i("Follows.", 3, nullable=True),
    "posts": _i("Posts.", 1234, nullable=True),
    "created_at": _s("Account creation time.", "2023-04-12T04:53:57.057Z", nullable=True),
}, ["did", "handle", "display_name", "description", "avatar_url", "followers", "follows", "posts", "created_at"],
    "A Bluesky account.")
_BSKY_ACTOR = _obj({
    "did": _s("DID.", "did:plc:coin", nullable=True), "handle": _s("Handle.", "coinbase.com", nullable=True),
    "display_name": _s("Display name.", "Coinbase", nullable=True), "description": _s("Bio.", "Coinbase on Bluesky", nullable=True),
    "avatar_url": _s("Avatar URL.", "https://cdn.bsky.app/img/avatar/plain/did:plc:coin/y@jpeg", nullable=True),
}, ["did", "handle", "display_name", "description", "avatar_url"], "An account from actor search.")
_MASTO_STATUS = _obj({
    "id": _s("Status id.", "115000000000000001"),
    "url": _s("Web URL.", "https://mastodon.social/@Gargron/115000000000000001", nullable=True),
    "created_at": _s("Creation time.", "2026-09-27T01:00:00.000Z", nullable=True),
    "text": _s("Plain-text content.", "Open source rocks"),
    "content_html": _s("Content as the instance serves it (sanitized HTML).", "<p>Open source rocks</p>"),
    "language": _s("Language code.", "en", nullable=True),
    "visibility": _s("public, unlisted...", "public", nullable=True),
    "replies": _i("Replies.", 1, nullable=True), "reblogs": _i("Boosts.", 2, nullable=True),
    "favourites": _i("Favourites.", 3, nullable=True),
    "is_reblog": _b("Whether this status is a boost of another.", False),
    "reblog_of_url": _s("URL of the boosted status when is_reblog.", None, nullable=True),
    "author_acct": _s("Author acct.", "Gargron", nullable=True),
    "author_display_name": _s("Author display name.", "Eugen Rochko", nullable=True),
    "media": _arr(_obj({"type": _s("image, video, gifv, audio...", "image", nullable=True),
                        "url": _s("Media URL.", "https://files.mastodon.social/media_attachments/files/x.png")},
                       ["type", "url"], "One attachment."), "Media attachments.", []),
    "tags": _arr(_s("Hashtag name.", "opensource"), "Hashtags on the status.", ["opensource"]),
    "sensitive": _b("Marked sensitive.", False),
    "spoiler_text": _s("Content warning text, empty when none.", ""),
}, ["id", "url", "created_at", "text", "content_html", "language", "visibility", "replies", "reblogs", "favourites",
    "is_reblog", "reblog_of_url", "author_acct", "author_display_name", "media", "tags", "sensitive", "spoiler_text"],
    "One Mastodon status.")
_MASTO_ACCOUNT = _obj({
    "id": _s("Account id on the instance.", "1", nullable=True),
    "username": _s("Username.", "Gargron", nullable=True), "acct": _s("acct (username or username@domain).", "Gargron", nullable=True),
    "display_name": _s("Display name.", "Eugen Rochko", nullable=True),
    "url": _s("Profile URL.", "https://mastodon.social/@Gargron", nullable=True),
    "bio": _s("Bio as plain text.", "Founder of Mastodon"),
    "avatar_url": _s("Avatar URL.", "https://files.mastodon.social/accounts/avatars/x.png", nullable=True),
    "followers": _i("Followers.", 500000, nullable=True), "following": _i("Following.", 400, nullable=True),
    "statuses": _i("Statuses posted.", 75000, nullable=True),
    "created_at": _s("Account creation time.", "2016-03-16T00:00:00.000Z", nullable=True),
    "bot": {"type": ["boolean", "null"], "description": "Declared as a bot; null when not stated.", "examples": [False]},
}, ["id", "username", "acct", "display_name", "url", "bio", "avatar_url", "followers", "following", "statuses",
    "created_at", "bot"], "A Mastodon account.")
_MASTO_TAG = _obj({
    "name": _s("Hashtag name.", "opensource"),
    "url": _s("Tag URL on the instance.", "https://mastodon.social/tags/opensource", nullable=True),
    "uses_7d": _i("Uses over the history the instance reports (about a week); null when none.", 120, nullable=True),
    "accounts_7d": _i("Distinct accounts over that history; null when none.", 90, nullable=True),
    "days": _i("Days of history the instance reported.", 7),
}, ["name", "url", "uses_7d", "accounts_7d", "days"], "A hashtag with its recent activity.")

_PROBABILITY = _obj({"outcome": _s("Outcome label.", "Yes"),
                     "probability_pct": _n("Implied probability in percent.", 62.5, nullable=True)},
                    ["outcome"], "One outcome and what the market prices it at.")
_MARKET = _obj({
    "id": _s("Polymarket market id.", "512345"),
    "question": _s("The market question.", "Will BTC close above $100k on Dec 31?"),
    "slug": _s("Polymarket slug.", "will-btc-close-above-100k"),
    "active": _b("Whether the market is active.", True),
    "closed": _b("Whether the market is closed.", False),
    "end_date": _s("ISO 8601 end date.", "2026-12-31T00:00:00Z", nullable=True),
    "volume": _n("Traded volume in USD.", 1834520.5, nullable=True),
    "liquidity": _n("Liquidity in USD.", 240310.2, nullable=True),
    "implied_probabilities": _arr(_PROBABILITY, "Each outcome with its implied probability.",
                                  [{"outcome": "Yes", "probability_pct": 62.5},
                                   {"outcome": "No", "probability_pct": 37.5}]),
}, ["id", "question", "implied_probabilities"], "One prediction market.")

_SPOT_QUOTE_PROPS = {
    "product_id": _s("Coinbase product, base-quote.", "BTC-USD"),
    "price": _s("Last trade price, as the exchange reports it.", "97231.45"),
    "price_change_24h_pct": _s("24-hour percentage change.", "1.82", nullable=True),
    "volume_24h": _s("24-hour volume in the base currency.", "14231.9", nullable=True),
    "base_currency": _s("Base currency code.", "BTC", nullable=True),
    "quote_currency": _s("Quote currency code.", "USD", nullable=True),
    "status": _s("Product status as reported by the exchange.", "online", nullable=True),
    "source": _const("coinbase-advanced-trade-public", "Data source."),
}
_SPOT_QUOTE = _obj(_SPOT_QUOTE_PROPS, ["product_id", "price", "source"], "Live spot quote.")

_PAGE_SOURCE = _obj({
    "text_chars": _i("Characters of readable text extracted.", 8421),
    "truncated": _b("Whether the text was cut at the extraction limit.", False),
    "javascript_rendered": _b("Whether a real browser rendered the page first.", True),
}, ["text_chars", "truncated", "javascript_rendered"], "How the page was read.")

_LINK = _obj({"text": _s("Anchor text.", "Pricing"),
              "href": _s("Absolute href.", "https://example.com/pricing")},
             ["href"], "One link on the page.")

_MCP_TOOL = _obj({"name": _s("Tool name.", "get_weather", nullable=True),
                  "description": _s("Tool description.", "Current weather for a city.", nullable=True),
                  "annotations": _free("The tool's annotations object, as served.",
                                       {"readOnlyHint": True}, ["object", "null"])},
                 ["name"], "One tool the inspected server lists.")

_VERDICT = _obj({
    "claim": _s("The claim, as given.", "HubVibe sells machine-payable site audits."),
    "verdict": _enum(["SUPPORTED", "CONTRADICTED", "UNSUPPORTED"],
                     "Whether the sources support, contradict, or do not address the claim.",
                     "SUPPORTED"),
    "quote": _s("The sentence the verdict rests on; null when UNSUPPORTED.",
                "Every check is priced per call over HTTP 402.", nullable=True),
    "source_n": _i("Which source (by n) the quote is from; null when UNSUPPORTED.", 1, nullable=True),
}, ["claim", "verdict"], "One claim and its verdict.")


_BOUNDS = _arr(_n("Bound.", 0.0), "Lower and upper bound at the confidence level.", [1.43, 2.53])
_T_TEST = _obj({
    "null_hypothesis": _s("What the test rejects.", "slope = 0 (no linear relationship)"),
    "t": _n("Student t statistic; null when undefined (a perfect fit).", 11.4, nullable=True),
    "p_value": _n("Two-sided p-value; null when undefined.", 0.0015, nullable=True),
    "significant_at_alpha": {"type": ["boolean", "null"], "description": "p_value < alpha.",
                             "examples": [True]},
}, ["null_hypothesis", "t", "p_value", "significant_at_alpha"], "One two-sided Student t test.")
_NORMALITY = {
    "type": ["object", "null"],
    "description": "Jarque-Bera normality test; null when the values are constant.",
    "properties": {
        "test": _const("jarque_bera", "The test."),
        "statistic": _n("Jarque-Bera statistic.", 0.42),
        "p_value": _n("Chi-square(2) p-value, exp(-JB/2).", 0.81),
        "null_hypothesis": _s("What the test rejects.", "the values are normally distributed"),
        "reject_at_alpha": _b("p_value < alpha: not normal at this alpha.", False),
    },
    "required": ["test", "statistic", "p_value", "null_hypothesis", "reject_at_alpha"],
}


def _nobj(properties: dict, required: list, description: str) -> dict:
    """An object that is null when the metric was not requested or is undefined."""
    return {"type": ["object", "null"], "properties": properties, "required": required,
            "description": description}


# --- one schema per worker, keyed by catalog name -------------------------------

_AIRPORT = _obj({"iata_code": _s("IATA code.", "LHR", nullable=True), "name": _s("Airport name.", "Heathrow Airport", nullable=True),
                 "city_name": _s("City served.", "London", nullable=True)}, ["iata_code", "name", "city_name"], "An airport.")
_CARRIER = _obj({"iata_code": _s("Airline IATA code.", "BA", nullable=True), "name": _s("Airline name.", "British Airways", nullable=True)},
                ["iata_code", "name"], "An airline.")
_PLACE = _obj({"iata_code": _s("Resolved IATA code (city or airport).", "LHR", nullable=True),
               "name": _s("Place name as the provider lists it.", "Heathrow Airport", nullable=True),
               "type": _s("city or airport; null when an IATA code was given directly.", "airport", nullable=True),
               "city_name": _s("City name.", "London", nullable=True),
               "country_code": _s("ISO 3166-1 alpha-2.", "GB", nullable=True),
               "given": _s("What the caller wrote.", "LHR")},
              ["iata_code", "name", "type", "city_name", "country_code", "given"], "A place as resolved for the search.")
_CONDITION = _obj({"allowed": _b("Whether the airline allows it; null when unstated.", True),
                   "penalty_amount": _n("Fee charged when allowed.", 40.0, nullable=True),
                   "penalty_currency": _s("Fee currency.", "USD", nullable=True)},
                  ["allowed", "penalty_amount", "penalty_currency"], "A fare condition.")
_CONDITION["properties"]["allowed"] = {"type": ["boolean", "null"], "description": "Whether the airline allows it; null when unstated.", "examples": [True]}


_HEADLINE = _obj({"title": _s("Headline.", "Bitcoin steadies as ETF inflows resume"),
                  "url": _s("Article link.", "https://news.google.com/rss/articles/CBMi..."),
                  "source_name": _s("Publisher.", "Reuters", nullable=True),
                  "published_at": _s("Publication time (UTC).", "2026-09-27T09:00:00Z", nullable=True)},
                 ["title", "url", "source_name", "published_at"], "One recent headline.")
_NEWS = _arr(_HEADLINE, "Recent headlines read for this call (Google News), newest first; empty when none.", [])
_NEWS_NOTE = _s("Why headlines are missing or partial; null when they were read cleanly.",
                "Headlines unavailable for this call: Google News (US:en) timed out", nullable=True)


OUTPUT_SCHEMAS = {
    "chain.network": _obj({
        "network": _const("base-mainnet", "Chain read."),
        "block_number": _i("Current head block number.", 41230577),
        "gas_price_wei": _s("Current gas price in wei, as a decimal string.", "12500000"),
        "gas_price_gwei": _n("Current gas price in gwei.", 0.0125),
    }, ["network", "block_number", "gas_price_wei", "gas_price_gwei"]),

    "chain.address": _obj({
        "address": _s("The address queried.", "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"),
        "network": _const("base-mainnet", "Chain read."),
        "balance_wei": _s("ETH balance in wei, as a decimal string.", "1250000000000000000"),
        "balance_eth": _n("ETH balance.", 1.25),
        "transaction_count": _i("Nonce: transactions sent from this address.", 42),
        "is_contract": _b("Whether code is deployed at the address.", False),
        "code_size_bytes": _i("Deployed bytecode size; 0 for an externally owned account.", 0),
    }, ["address", "network", "balance_wei", "balance_eth", "transaction_count",
        "is_contract", "code_size_bytes"]),

    "chain.transaction": _obj({
        "hash": _s("Transaction hash.", "0x" + "ab" * 32),
        "network": _const("base-mainnet", "Chain read."),
        "from": _s("Sender.", "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd", nullable=True),
        "to": _s("Recipient; null for a contract creation.",
                 "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", nullable=True),
        "value_wei": _s("Value transferred in wei, as a decimal string.", "0"),
        "value_eth": _n("Value transferred in ETH.", 0.0),
        "block_number": _i("Block the transaction was mined in; null while pending.", 41230501, nullable=True),
        "mined": _b("Whether the transaction is in a block.", True),
        "status": _enum(["success", "failed", "pending"], "Receipt status.", "success"),
        "gas_used": _i("Gas used, from the receipt.", 65231),
        "log_count": _i("Number of logs emitted, from the receipt.", 2),
    }, ["hash", "network", "from", "to", "value_wei", "value_eth", "block_number", "mined", "status"]),

    "chain.rpc": _obj({
        "method": _s("The JSON-RPC method called.", "eth_blockNumber"),
        "endpoint": _s("The RPC endpoint that answered.", "https://mainnet.base.org"),
        "result": _free("The chain's `result`, verbatim, when the call succeeded.", "0x2751f11"),
        "error": _free("The chain's JSON-RPC error object, verbatim, when it answered with one "
                       "(a revert reason, an unknown block). Either `result` or `error` is present.",
                       {"code": -32000, "message": "execution reverted"}, ["object", "null"]),
    }, ["method", "endpoint"]),

    "market.quote": _SPOT_QUOTE,

    "market.prediction": _obj({
        "query": _s("The search query, or null for the top markets by volume.", "bitcoin", nullable=True),
        "markets": _arr(_MARKET, "Matching markets, highest volume first."),
        "count": _i("Number of markets returned.", 1),
        "source": _const("polymarket-gamma", "Data source."),
        "note": _s("What the numbers are and are not.",
                   "Implied probabilities are what the market is currently pricing, not a forecast by HubVibe."),
    }, ["query", "markets", "count", "source", "note"]),

    "market.rates": _obj({
        "currency": _s("Base currency of the table.", "USD"),
        "rates": _obj({}, [], "Exchange rate per currency code, as decimal strings.",
                      additionalProperties={"type": "string"},
                      examples=[{"EUR": "0.92", "GBP": "0.78", "BTC": "0.0000103"}]),
        "source": _const("coinbase-exchange-rates", "Data source."),
    }, ["currency", "rates", "source"]),

    "market.ticker": _obj({
        "product_id": _s("Coinbase product, base-quote.", "BTC-USD"),
        "price": _s("Last trade price.", "97231.45", nullable=True),
        "bid": _s("Best bid.", "97230.10", nullable=True),
        "ask": _s("Best ask.", "97232.80", nullable=True),
        "volume": _s("24-hour volume in the base currency.", "14231.9", nullable=True),
        "time": _s("Exchange timestamp, ISO 8601.", "2026-09-22T04:10:12.345Z", nullable=True),
        "source": _const("coinbase-exchange", "Data source."),
    }, ["product_id", "price", "bid", "ask", "source"]),

    "prediction.market": _obj({
        "slug": _s("The slug queried.", "will-btc-close-above-100k"),
        "market": _MARKET,
        "source": _const("polymarket-gamma", "Data source."),
    }, ["slug", "market", "source"]),

    "prediction.events": _obj({
        "events": _arr(_obj({
            "id": _s("Event id.", "24011"),
            "title": _s("Event title.", "Bitcoin price on December 31"),
            "slug": _s("Event slug.", "bitcoin-price-on-december-31"),
            "volume": _n("Traded volume in USD.", 5230100.0, nullable=True),
            "end_date": _s("ISO 8601 end date.", "2026-12-31T00:00:00Z", nullable=True),
            "market_count": _i("Markets grouped under this event.", 6),
        }, ["id", "title", "slug", "market_count"], "One event."),
            "Events, highest volume first."),
        "count": _i("Number of events returned.", 1),
        "source": _const("polymarket-gamma", "Data source."),
    }, ["events", "count", "source"]),

    "extract.page": _obj({
        "url": _s("The URL requested.", "https://example.com"),
        "final_url": _s("The URL after redirects.", "https://example.com/", nullable=True),
        "title": _s("Page title.", "Example Domain", nullable=True),
        "description": _s("Meta description.", "Example Domain for documentation.", nullable=True),
        "text": _s("Readable text of the page.", "Example Domain. This domain is for use in illustrative examples..."),
        "text_chars": _i("Characters in `text`.", 172),
        "truncated": _b("Whether the text was cut at the extraction limit.", False),
        "links": _arr(_LINK, "Up to 100 links on the page.",
                      [{"text": "More information...", "href": "https://www.iana.org/domains/example"}]),
        "javascript_rendered": _b("Whether a real browser rendered the page first.", True),
    }, ["url", "text", "text_chars", "truncated", "links", "javascript_rendered"]),

    "fetch.raw": _obj({
        "url": _s("The URL requested.", "https://example.com"),
        "final_url": _s("The URL after redirects.", "https://example.com/", nullable=True),
        "status": _i("HTTP status the target returned; any status is a completed fetch.", 200),
        "content_type": _s("Content-Type header.", "text/html; charset=UTF-8", nullable=True),
        "bytes": _i("Response body size in bytes.", 1256),
        "text": _s("Body text for textual content types; null for binary.",
                   "<!doctype html><html>...", nullable=True),
        "truncated": _b("Whether `text` was cut at the fetch limit.", False),
        "headers": _obj({}, [], "Response headers, lower-cased names.",
                        additionalProperties={"type": "string"},
                        examples=[{"content-type": "text/html; charset=UTF-8", "cache-control": "max-age=604800"}]),
    }, ["url", "status", "bytes", "truncated", "headers"]),

    "search.web": _obj({
        "query": _s("The query searched.", "HTTP 402 Payment Required"),
        "answer": _s("Grounded answer with the sources it drew on.",
                     "HTTP 402 Payment Required is a status code reserved for payments..."),
        "sources": _arr(_obj({"url": _s("Source URL.", "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/402"),
                              "title": _s("Source title.", "402 Payment Required - HTTP | MDN", nullable=True)}, ["url"]),
                        "Web sources the answer is grounded in."),
        "search_queries_used": _arr(_s("A query the grounding step issued.", "HTTP 402 status code"),
                                    "Search queries the grounding step actually ran.", ["HTTP 402 status code"]),
        "model": _MODEL,
    }, ["query", "answer", "sources", "search_queries_used", "model"]),

    "llm.analyze": _obj({
        "question": _s("The question asked of the material.", "What does it sell, and at what price?"),
        "answer": _s("The answer, from the material only; says so when the material does not contain it.",
                     "It sells machine-payable site audits at $0.05 per call."),
        "model": _MODEL,
        "input_chars": _i("Characters of material analysed.", 61),
        "tokens": _TOKENS,
    }, ["question", "answer", "model", "input_chars", "tokens"]),

    "llm.extract": _obj({
        "fields": _obj({}, [], "Exactly the requested field names; null where the material does not state a value.",
                       additionalProperties=True,
                       examples=[{"title": "HubVibe", "pricing": "$0.05 per call"}]),
        "model": _MODEL,
        "tokens": _TOKENS,
    }, ["fields", "model", "tokens"]),

    "llm.generate": _obj({
        "text": _s("The completion.", "Here is a short poem about bees..."),
        "model": _s("The model used.", "gemini-2.5-flash"),
        "provider": _s("The provider that served it.", "gemini"),
        "finish_reason": _s("Why generation stopped.", "STOP", nullable=True),
        "usage": _obj({"input_tokens": _i("Input tokens.", 14), "output_tokens": _i("Output tokens.", 96)},
                      ["input_tokens", "output_tokens"], "Token usage."),
    }, ["text", "model", "provider", "usage"]),

    "code.execute": _obj({
        "code": _s("The code that ran.", "print(sum(range(10)))"),
        "output": _s("Captured stdout/stderr.", "45\n"),
        "outcome": _s("Sandbox outcome code.", "OUTCOME_OK"),
        "summary": _s("The model's one-line summary of the run.", "Printed the sum 45.", nullable=True),
        "model": _MODEL,
    }, ["code", "output", "outcome", "model"]),

    "image.generate": _obj({
        "prompt": _s("The prompt.", "A beehive built from circuit boards, isometric illustration"),
        "aspect_ratio": _s("Aspect ratio generated.", "1:1"),
        "image_base64": _s("The image, base64.", "iVBORw0KGgo..."),
        "mime_type": _s("Image MIME type.", "image/png"),
        "model": _s("Image model.", "imagen-4.0-generate-001"),
    }, ["prompt", "aspect_ratio", "image_base64", "mime_type", "model"]),

    "speech.synthesize": _obj({
        "text_chars": _i("Characters synthesised.", 58),
        "voice": _s("Voice used.", "en-US-Neural2-C"),
        "audio_base64": _s("The audio, base64.", "UklGRi4AAABXQVZF..."),
        "mime_type": _s("Audio MIME type.", "audio/mpeg"),
    }, ["text_chars", "voice", "audio_base64", "mime_type"]),

    "speech.transcribe": _obj({
        "transcript": _s("The transcript.", "hello world"),
        "language_code": _s("Language recognised.", "en-US"),
        "confidence": _n("Mean recognition confidence, 0-1; null when the engine gives none.", 0.94, nullable=True),
        "model": _s("Recognition model.", "latest_short"),
    }, ["transcript", "language_code", "model"]),

    "video.generate": _obj({
        "prompt": _s("The prompt.", "A single bee landing on a circuit-board flower, slow motion"),
        "aspect_ratio": _s("Aspect ratio generated.", "16:9"),
        "duration_seconds": _i("Clip length in seconds.", 4),
        "video_base64": _s("The video, base64, when returned inline.", "AAAAIGZ0eXBpc29t...", nullable=True),
        "gcs_uri": _s("Cloud Storage URI, when the provider stored it instead.", None, nullable=True),
        "mime_type": _s("Video MIME type.", "video/mp4"),
        "model": _s("Video model.", "veo-3.0-generate-001"),
    }, ["prompt", "aspect_ratio", "duration_seconds", "mime_type", "model"]),

    "stats.probability": _obj({
        "source": _obj({
            "type": _enum(["points", "bigquery"], "Where the points came from.", "points"),
            "table": _s("BigQuery table read, when source is bigquery.", None, nullable=True),
            "x_column": _s("Column used for x, when source is bigquery.", None, nullable=True),
            "y_column": _s("Column used for y, when source is bigquery.", None, nullable=True),
            "sql": _s("The read-only SQL that ran, when source is bigquery.", None, nullable=True),
            "rows_available": _i("Rows with finite x and y in the table; null for inline points.",
                                 None, nullable=True),
            "rows_used": _i("Points the statistics were computed from.", 5),
            "sampled": _b("True when the table had more usable rows than were read.", False),
            "gib_processed": _n("Gibibytes BigQuery scanned; null for inline points.", None,
                                nullable=True),
        }, ["type", "table", "x_column", "y_column", "sql", "rows_available", "rows_used",
            "sampled", "gib_processed"], "Where the data came from and how much of it was used."),
        "n": _i("Number of points.", 5),
        "alpha": _n("Significance level used.", 0.05),
        "confidence_level": _n("1 - alpha: the level of every interval.", 0.95),
        "metrics": _arr(_enum(["linear_regression", "normal_distribution", "p_values", "prediction"],
                              "Metric name.", "linear_regression"),
                        "Metrics computed, in canonical order.",
                        ["linear_regression", "normal_distribution", "p_values", "prediction"]),
        "linear_regression": _nobj({
            "slope": _n("OLS slope.", 1.98),
            "intercept": _n("OLS intercept.", 0.08),
            "r": _n("Pearson correlation; null when y is constant.", 0.998, nullable=True),
            "r_squared": _n("Coefficient of determination; null when y is constant.", 0.996, nullable=True),
            "adjusted_r_squared": _n("R^2 adjusted for degrees of freedom.", 0.995, nullable=True),
            "slope_std_error": _n("Standard error of the slope.", 0.071),
            "intercept_std_error": _n("Standard error of the intercept.", 0.236),
            "residual_std_error": _n("Standard error of the residuals, sqrt(SSE / df).", 0.225),
            "degrees_of_freedom": _i("n - 2.", 3),
            "slope_t": _n("t statistic of the slope; null when undefined.", 27.8, nullable=True),
            "intercept_t": _n("t statistic of the intercept; null when undefined.", 0.34, nullable=True),
            "t_critical": _n("Two-sided t critical value at alpha with df degrees of freedom.", 3.18),
            "slope_ci": _BOUNDS,
            "intercept_ci": _BOUNDS,
            "f_statistic": _n("F statistic of the regression (t^2); null when undefined.", 773.0,
                              nullable=True),
            "sse": _n("Sum of squared residuals.", 0.152),
            "sst": _n("Total sum of squares of y.", 39.4),
            "x_mean": _n("Mean of x.", 3.0),
            "y_mean": _n("Mean of y.", 6.02),
        }, ["slope", "intercept", "r", "r_squared", "adjusted_r_squared", "slope_std_error",
            "intercept_std_error", "residual_std_error", "degrees_of_freedom", "slope_t",
            "intercept_t", "t_critical", "slope_ci", "intercept_ci", "f_statistic", "sse", "sst",
            "x_mean", "y_mean"], "Ordinary least squares fit of y on x; null when not requested."),
        "normal_distribution": _nobj({
            "of": _enum(["y", "x", "residuals"], "Which values the model fits.", "y"),
            "mean": _n("Sample mean.", 6.02),
            "std_dev": _n("Sample standard deviation (n - 1).", 3.14),
            "variance": _n("Sample variance (n - 1).", 9.86),
            "min": _n("Smallest value.", 2.1),
            "max": _n("Largest value.", 10.1),
            "median": _n("Median of the values.", 6.2),
            "skewness": _n("Sample skewness; null when the values are constant.", 0.05, nullable=True),
            "excess_kurtosis": _n("Excess kurtosis; null when the values are constant.", -1.3,
                                  nullable=True),
            "quantiles": {"type": ["array", "null"],
                          "description": "Quantiles of the fitted normal; null when degenerate.",
                          "items": _obj({"p": _n("Probability.", 0.95),
                                         "value": _n("Value at that quantile.", 11.19)},
                                        ["p", "value"])},
            "probabilities": _arr(_obj({"query": _free("The query as sent.", {"below": 8.0}, ["object"]),
                                        "probability": _n("Probability under the fitted normal.", 0.736)},
                                       ["query", "probability"]),
                                  "Answers to probability_queries, in order.",
                                  [{"query": {"below": 8.0}, "probability": 0.736}]),
            "normality": _NORMALITY,
        }, ["of", "mean", "std_dev", "variance", "min", "max", "median", "skewness",
            "excess_kurtosis", "quantiles", "probabilities", "normality"],
            "Normal model of the chosen values; null when not requested."),
        "p_values": _nobj({
            "slope": _T_TEST,
            "intercept": _T_TEST,
            "normality_of_residuals": _NORMALITY,
        }, ["slope", "intercept", "normality_of_residuals"],
            "Hypothesis tests validated at alpha; null when not requested."),
        "prediction": {"type": ["array", "null"],
                       "description": "One entry per predict_x; null when not requested.",
                       "items": _obj({"x": _n("The x asked for.", 6.0),
                                      "y_hat": _n("Predicted y.", 11.96),
                                      "mean_ci": _BOUNDS,
                                      "prediction_interval": _BOUNDS},
                                     ["x", "y_hat", "mean_ci", "prediction_interval"])},
        "notes": _arr(_s("A caveat about a degenerate input.", "The points lie exactly on a line."),
                      "Caveats about the input; empty when there are none.", []),
        "method": _s("How the numbers were produced.",
                     "Ordinary least squares (closed form). Student t p-values from the "
                     "regularised incomplete beta function..."),
    }, ["source", "n", "alpha", "confidence_level", "metrics", "linear_regression",
        "normal_distribution", "p_values", "prediction", "notes", "method"]),

    "data.query": _obj({
        "sql": _s("The SQL that ran.", "SELECT name, SUM(number) AS n FROM `bigquery-public-data.usa_names.usa_1910_2013` GROUP BY name ORDER BY n DESC LIMIT 5"),
        "columns": _BQ_COLUMNS,
        "rows": _BQ_ROWS,
        "row_count": _i("Rows returned (capped).", 5),
        "total_rows": _i("Rows the query produced before the cap.", 5),
        "truncated": _b("Whether rows were cut at the cap.", False),
        "gib_processed": _GIB,
        "cache_hit": _b("Whether BigQuery served it from cache (no bytes billed).", False),
    }, ["sql", "columns", "rows", "row_count", "total_rows", "truncated", "gib_processed", "cache_hit"]),

    "data.question": _obj({
        "question": _s("The question asked.", "Which five names were given most often?"),
        "table": _s("The table queried.", "bigquery-public-data.usa_names.usa_1910_2013"),
        "answer": _s("The answer, read from the rows; states the figures.",
                     "James (4,942,431), John (4,834,422), ..."),
        "sql": _s("The SQL the model wrote and that ran.", "SELECT name, SUM(number) AS n FROM ... LIMIT 5"),
        "columns": _BQ_COLUMNS,
        "rows": _BQ_ROWS,
        "row_count": _i("Rows returned.", 5),
        "gib_processed": _GIB,
        "model": _MODEL,
    }, ["question", "table", "answer", "sql", "columns", "rows", "row_count", "gib_processed", "model"]),

    "data.forecast": _obj({
        "table": _s("Source table.", "bigquery-public-data.covid19_nyt.us_states"),
        "timestamp_col": _s("Timestamp column.", "date"),
        "data_col": _s("Value column forecast.", "confirmed_cases"),
        "horizon": _i("Forecast points requested.", 10),
        "columns": _arr(_s("AI.FORECAST output column.", "forecast_timestamp"),
                        "AI.FORECAST output columns.",
                        ["state_name", "forecast_timestamp", "forecast_value",
                         "confidence_level", "prediction_interval_lower_bound",
                         "prediction_interval_upper_bound", "ai_forecast_status"]),
        "rows": _arr(_obj({}, [], "One forecast point.", additionalProperties=True,
                          examples=[{"state_name": "Texas", "forecast_timestamp": "2023-03-24 00:00:00",
                                     "forecast_value": "8631745.2", "confidence_level": "0.95"}]),
                     "Forecast rows.",
                     [{"state_name": "Texas", "forecast_timestamp": "2023-03-24 00:00:00",
                       "forecast_value": "8631745.2", "confidence_level": "0.95"}]),
        "row_count": _i("Rows returned.", 10),
        "gib_processed": _GIB,
    }, ["table", "timestamp_col", "data_col", "horizon", "columns", "rows", "row_count", "gib_processed"]),

    "data.anomalies": _obj({
        "history_table": _s("Table the forecast is fitted on.", "bigquery-public-data.covid19_nyt.us_states"),
        "target_table": _s("Table checked for anomalies.", "bigquery-public-data.covid19_nyt.us_states"),
        "timestamp_col": _s("Timestamp column.", "date"),
        "data_col": _s("Value column.", "confirmed_cases"),
        "anomaly_prob_threshold": _n("Probability threshold used.", 0.95),
        "columns": _arr(_s("AI.DETECT_ANOMALIES output column.", "is_anomaly"),
                        "AI.DETECT_ANOMALIES output columns.",
                        ["state_name", "date", "confirmed_cases", "is_anomaly",
                         "lower_bound", "upper_bound", "anomaly_probability"]),
        "rows": _arr(_obj({}, [], "One checked point.", additionalProperties=True,
                          examples=[{"state_name": "Texas", "date": "2023-03-20", "confirmed_cases": "8631000",
                                     "is_anomaly": "false", "anomaly_probability": "0.12"}]),
                     "Checked rows with their anomaly flag.",
                     [{"state_name": "Texas", "date": "2023-03-20", "confirmed_cases": "8631000",
                       "is_anomaly": "false", "anomaly_probability": "0.12"}]),
        "row_count": _i("Rows returned.", 10),
        "gib_processed": _GIB,
    }, ["history_table", "target_table", "timestamp_col", "data_col", "anomaly_prob_threshold",
        "columns", "rows", "row_count", "gib_processed"]),

    "monitor.snapshot": _obj({
        "url": _s("The URL baselined.", "https://example.com"),
        "title": _s("Page title at snapshot time.", "Example Domain", nullable=True),
        "text_chars": _i("Characters of readable text captured.", 172),
        "content_hash": _s("sha256 of the readable text, hex.", "a" * 64),
        "note": _s("What to do next.", "Baseline saved. Call monitor.check on this URL later to see what changed."),
    }, ["url", "text_chars", "content_hash", "note"]),

    "monitor.check": _obj({
        "url": _s("The URL checked.", "https://example.com"),
        "title": _s("Page title now.", "Example Domain", nullable=True),
        "changed": _b("Whether the readable text differs from the baseline.", True),
        "baseline_age_seconds": _n("Seconds since the baseline was taken.", 86400),
        "change_summary": _s("What changed, in words; or that nothing did.",
                             "The pricing section now lists a $0.15 bundle; the headline is unchanged."),
        "model": _s("Model that summarised the diff; present only when something changed.", "gemini-2.5-flash"),
    }, ["url", "changed", "baseline_age_seconds", "change_summary"]),

    "commerce.availability": _obj({
        "url": _s("The page checked.", "https://example.com/products/wool-runner"),
        "final_url": _s("The URL after redirects.", "https://example.com/products/wool-runner", nullable=True),
        "title": _s("Product or listing name from the page.", "Wool Runner", nullable=True),
        "available": _b_or_null("Purchasable or bookable right now under the conditions asked; null when the page does not say.", True),
        "availability": _enum(_AVAILABILITY, "Availability of the option asked for (or of the product when no variant was named).", "in_stock"),
        "price": _n("Price of the matched option, else the product's; null when not stated.", 110.0, nullable=True),
        "currency": _s("ISO 4217 currency of `price`; null when not stated.", "USD", nullable=True),
        "options": _arr(_COMMERCE_OPTION, "Every purchasable variation the page lists (sizes, colours, dates, rooms, fares)."),
        "options_count": _i("How many options the page lists.", 7),
        "matched_option": {"type": ["object", "null"],
                           "description": "The option matching `variant`; null when no variant was asked or none matched.",
                           "properties": _COMMERCE_OPTION["properties"], "required": _COMMERCE_OPTION["required"]},
        "variant": _s("The variant asked for, as given.", "size 10", nullable=True),
        "quantity": _i("The quantity asked for.", 2, nullable=True),
        "quantity_ok": _b_or_null("Whether that quantity can be ordered; null when the page does not say.", True),
        "ship_to": _s("The country asked for.", "US", nullable=True),
        "ship_to_ok": _b_or_null("Whether the page says it delivers there; null when it does not say.", True),
        "shipping": _s("What the page states about shipping or delivery; null unless stated.",
                       "Free shipping on orders over $75", nullable=True),
        "eligibility_notes": _arr(_s("A restriction or caveat the page states, or why a condition could not be checked.",
                                     "Ships to US, CA and GB only."),
                                  "Restrictions and caveats, in words.", []),
        "evidence": _arr(_s("What the answer rests on: a structured-data field or an exact quote from the page.",
                            "JSON-LD Offer availability https://schema.org/InStock price 110 USD"),
                         "The evidence behind the answer, best first."),
        "source": _enum(["jsonld", "shopify", "opengraph", "llm", "none"],
                        "Which layer answered: the page's JSON-LD, the Shopify product JSON, Open Graph tags, the model over the rendered page, or nothing usable.",
                        "jsonld"),
        "javascript_rendered": _b("Whether a real browser had to render the page to answer.", False),
        "http_status": _i("HTTP status of the page fetch.", 200, nullable=True),
        "confidence": _enum(["high", "medium", "low"],
                            "high: exact structured data for the option asked; medium: page-level tags only; low: model reading or nothing found.",
                            "high"),
        "checked_at": {"type": "string", "format": "date-time",
                       "description": "When the page was read (UTC). The answer was true at this moment; nothing here is cached.",
                       "examples": ["2026-09-26T18:00:00Z"]},
        "language": _s("BCP-47 tag the prose fields were written in; null for the page's own language.", "en", nullable=True),
        "model": _s("The model used, only when the model layer ran.", "gemini-2.5-flash", nullable=True),
    }, ["url", "available", "availability", "price", "currency", "options", "options_count",
        "matched_option", "quantity_ok", "ship_to_ok", "eligibility_notes", "evidence", "source",
        "javascript_rendered", "confidence", "checked_at"]),

    "market.stock": _obj({
        "symbol": _s("Ticker.", "AAPL"),
        "name": _s("Company or instrument name.", "Apple Inc.", nullable=True),
        "exchange": _s("Listing exchange as the source names it.", "NASDAQ-GS", nullable=True),
        "asset_class": _s("stocks, etf, equity...", "stocks", nullable=True),
        "currency": _s("Quote currency.", "USD", nullable=True),
        "price": _n("Last price.", 341.07),
        "change": _n("Change on the day.", 5.15, nullable=True),
        "change_pct": _n("Change on the day, percent.", 1.53, nullable=True),
        "previous_close": _n("Previous close.", 335.92, nullable=True),
        "day_high": _n("Day high.", 341.67, nullable=True),
        "day_low": _n("Day low.", 334.53, nullable=True),
        "volume": _n("Volume.", 30002768, nullable=True),
        "market_state": _s("Open, Closed, Pre-Market... as the source reports it.", "Closed", nullable=True),
        "as_of": _s("The source's own timestamp for the price (ISO 8601; a date, or a time with its zone).",
                    "2026-09-25T20:00:01Z", nullable=True),
        "delayed_minutes": _i("How delayed the source says the quote is; null when it does not say.", 15, nullable=True),
        "history_range": _s("The range the history covers.", "1mo", nullable=True),
        "history": _arr(_OHLCV, "Daily bars, oldest first; empty when include_history is false."),
        "history_rows": _i("Bars returned.", 22),
        "source": _s("The provider that answered.", "nasdaq-data-api", nullable=True),
        "checked_at": _CHECKED_AT,
    }, ["symbol", "price", "as_of", "delayed_minutes", "history", "history_rows", "source", "checked_at"]),

    "market.fundamentals": _obj({
        "symbol": _s("Ticker as given.", "AAPL", nullable=True),
        "cik": _i("SEC Central Index Key.", 320193),
        "entity_name": _s("Registrant name.", "Apple Inc.", nullable=True),
        "concepts": _obj({}, [], "One entry per concept found, keyed by the name asked for.",
                         additionalProperties=_XBRL_CONCEPT, examples=[{"Revenues": _XBRL_CONCEPT_EXAMPLE}]),
        "concepts_missing": _arr(_s("A concept the filer has not reported.", "Liabilities"),
                                 "Concepts asked for that the filer has no facts for.", []),
        "periods": _i("Periods per concept requested.", 8),
        "forms": {"type": ["array", "null"], "items": {"type": "string"},
                  "description": "Form filter applied, or null.", "examples": [["10-K", "10-Q"]]},
        "as_of": _s("Latest filing date among the values returned.", "2026-07-31", nullable=True),
        "source": _const("sec-edgar-xbrl-companyfacts", "Data source."),
        "checked_at": _CHECKED_AT,
    }, ["cik", "concepts", "concepts_missing", "periods", "as_of", "source", "checked_at"]),

    "finance.analytics": _obj({
        "source": _obj({
            "type": _enum(["prices", "symbol"], "Inline prices or a live-fetched series.", "prices"),
            "symbol": _s("Ticker, with type symbol.", "AAPL", nullable=True),
            "range": _s("Range fetched, with type symbol.", "1y", nullable=True),
            "provider": _s("Provider that served the series.", "nasdaq-data-api", nullable=True),
            "benchmark_symbol": _s("Benchmark ticker when fetched.", "SPY", nullable=True),
            "benchmark_n": _i("Benchmark prices used.", 250, nullable=True),
        }, ["type"], "Where the series came from."),
        "n": _i("Prices in the series.", 22),
        "n_returns": _i("Period returns (n - 1).", 21),
        "first_date": _s("First date when dates were given.", "2026-08-26", nullable=True),
        "last_date": _s("Last date when dates were given.", "2026-09-25", nullable=True),
        "periods_per_year": _i("Annualization basis.", 252),
        "risk_free_rate": _n("Annual risk-free rate used.", 0.04),
        "metrics_computed": _arr(_s("A metric name.", "returns"), "Metrics present (non-null) in this result.",
                                 ["returns", "volatility", "sharpe"]),
        "returns": _nobj({
            "n_returns": _i("Period returns.", 21),
            "first_price": _n("First price.", 100.0), "last_price": _n("Last price.", 120.0),
            "total_return": _n("last/first - 1.", 0.2),
            "cagr": _n("Compound annual growth rate over n_returns/periods_per_year years.", 7.9, nullable=True),
            "mean_period_return": _n("Mean simple return per period.", 0.0088),
            "mean_log_return": _n("Mean log return per period.", 0.0087),
            "annualized_mean_return": _n("mean_period_return * periods_per_year.", 2.2),
            "best_period": _n("Largest period return.", 0.0227), "worst_period": _n("Smallest period return.", -0.0167),
        }, ["n_returns", "first_price", "last_price", "total_return", "mean_period_return"], "Return statistics."),
        "volatility": _nobj({
            "period_std": _n("Sample standard deviation of period returns.", 0.0137, nullable=True),
            "annualized": _n("period_std * sqrt(periods_per_year).", 0.217, nullable=True),
            "log_return_std": _n("Sample standard deviation of log returns.", 0.0136, nullable=True),
            "downside_deviation_annualized": _n("Annualized downside deviation vs the risk-free rate.", 0.09, nullable=True),
        }, ["period_std", "annualized"], "Dispersion of returns."),
        "sharpe": _n("Annualized Sharpe ratio; null when undefined.", 9.8, nullable=True),
        "sortino": _n("Annualized Sortino ratio; null when undefined.", 23.6, nullable=True),
        "drawdown": _nobj({
            "max_drawdown": _n("Largest peak-to-trough fall as a fraction (negative or 0).", -0.0167),
            "peak_index": _i("Index of the peak.", 3), "trough_index": _i("Index of the trough.", 4),
            "recovery_index": _i("First index back at the peak; null when not recovered.", 6, nullable=True),
            "duration_periods": _i("Periods from peak to trough.", 1),
        }, ["max_drawdown", "peak_index", "trough_index", "recovery_index", "duration_periods"], "Maximum drawdown."),
        "var": _nobj({
            "alpha": _n("Tail probability.", 0.05), "horizon_periods": _i("Horizon in periods.", 1),
            "historical_var": _n("Loss not exceeded with probability 1-alpha, as a positive fraction.", 0.0158),
            "historical_cvar": _n("Mean loss beyond the VaR.", 0.0167, nullable=True),
            "parametric_var": _n("Normal-model VaR, -(mean + z*std).", 0.0137, nullable=True),
            "parametric_z": _n("z at alpha.", -1.6449),
        }, ["alpha", "horizon_periods", "historical_var", "parametric_z"], "Value at risk, one period."),
        "beta": _nobj({
            "n": _i("Aligned returns used.", 21), "beta": _n("cov/var vs the benchmark.", 1.12, nullable=True),
            "alpha_annualized": _n("Annualized Jensen's alpha.", 0.31, nullable=True),
            "correlation": _n("Pearson correlation of period returns.", 0.83, nullable=True),
            "benchmark_annualized_volatility": _n("Benchmark volatility, annualized.", 0.18, nullable=True),
        }, ["n", "beta"], "Against the benchmark; null unless one was given."),
        "moving_averages": _nobj({
            "sma": _BY_WINDOW, "ema": _BY_WINDOW, "last_price": _n("Last price, for comparison.", 120.0),
        }, ["sma", "ema", "last_price"], "Simple and exponential moving averages at the last price."),
        "rsi": _nobj({"window": _i("Window.", 14), "value": _n("Wilder RSI 0-100; null when too few prices.", 71.2, nullable=True)},
                     ["window", "value"], "Relative strength index."),
        "bollinger": _nobj({
            "window": _i("Window.", 20), "k": _n("Band width in standard deviations.", 2.0),
            "middle": _n("SMA.", 113.5), "upper": _n("Upper band.", 121.1), "lower": _n("Lower band.", 105.9),
            "bandwidth": _n("(upper-lower)/middle.", 0.134, nullable=True),
            "percent_b": _n("Where the last price sits in the band (0 lower, 1 upper).", 0.93, nullable=True),
        }, ["window", "k", "middle", "upper", "lower"], "Bollinger bands at the last price."),
        "black_scholes": _nobj({
            "type": _enum(["call", "put"], "Option type.", "call"),
            "price": _n("Option value.", 4.83), "delta": _n("dV/dS.", 0.54), "gamma": _n("d2V/dS2.", 0.024),
            "vega": _n("dV/dsigma per 1.0 of volatility.", 33.1), "theta": _n("dV/dt per year.", -6.9),
            "rho": _n("dV/dr per 1.0 of rate.", 29.8), "d1": _n("d1.", 0.11), "d2": _n("d2.", -0.04),
            "inputs": _obj({"spot": _n("S.", 120.0), "strike": _n("K.", 120.0), "rate": _n("r.", 0.04),
                            "volatility": _n("sigma.", 0.217), "time_to_expiry_years": _n("T.", 0.5),
                            "dividend_yield": _n("q.", 0.0)},
                           ["spot", "strike", "rate", "volatility", "time_to_expiry_years", "dividend_yield"],
                           "The inputs used, including any defaulted from the series."),
        }, ["type", "price", "delta", "gamma", "vega", "theta", "rho", "inputs"], "Black-Scholes-Merton."),
        "kelly": _nobj({
            "win_probability": _n("p.", 0.55), "win_loss_ratio": _n("b.", 1.5),
            "fraction": _n("Kelly fraction p - (1-p)/b.", 0.25), "half_kelly": _n("Half Kelly.", 0.125),
            "bet": _b("Whether the fraction is positive.", True),
        }, ["win_probability", "win_loss_ratio", "fraction", "half_kelly", "bet"], "Kelly criterion."),
        "notes": _arr(_s("A caveat.", "rsi needs at least 15 prices."), "Caveats about undefined or aligned metrics.", []),
        "method": _s("Every formula used, in words.", "simple returns p_t/p_{t-1}-1 and log returns..."),
        "as_of": _s("Date or timestamp of the last price (from the dates given or the source).", "2026-09-25", nullable=True),
        "checked_at": _CHECKED_AT,
    }, ["source", "n", "n_returns", "periods_per_year", "risk_free_rate", "metrics_computed", "returns",
        "volatility", "sharpe", "sortino", "drawdown", "var", "beta", "moving_averages", "rsi", "bollinger",
        "black_scholes", "kelly", "notes", "method", "as_of", "checked_at"]),

    "opendata.search": _obj({
        "query": _s("The query as given.", "人口"),
        "region": _s("Region searched.", "jp"),
        "portals_searched": _arr(_s("Portal id.", "data.e-gov.go.jp"), "Portals the query was sent to.",
                                 ["data.e-gov.go.jp", "catalog.data.metro.tokyo.lg.jp"]),
        "portals_ok": _arr(_s("Portal id.", "data.e-gov.go.jp"), "Portals that answered.",
                           ["data.e-gov.go.jp", "catalog.data.metro.tokyo.lg.jp"]),
        "portals_failed": _arr(_obj({"portal": _s("Portal id.", "data.gov.hk"),
                                     "reason": _s("Why it did not answer this call.", "data.gov.hk timed out")},
                                    ["portal", "reason"], "A portal that failed for this call."),
                               "Portals that did not answer, with the reason; never silently dropped.", []),
        "total_matches": _obj({}, [], "Total matches per portal that answered, keyed by portal id.",
                              additionalProperties={"type": "integer"},
                              examples=[{"data.e-gov.go.jp": 4699, "catalog.data.metro.tokyo.lg.jp": 1523}]),
        "results": _arr(_obj({
            "portal": _s("Portal id.", "data.e-gov.go.jp"),
            "region": _s("Region code.", "jp"),
            "country": _s("ISO 3166-1 alpha-2.", "JP"),
            "id": _s("Dataset id on the portal.", "5b2f1c7e-8a3d-4c1e-9f2a-1b2c3d4e5f60"),
            "name": _s("Dataset slug.", "population-census-2020", nullable=True),
            "title": _s("Dataset title, in the portal's language.", "令和2年国勢調査 人口等基本集計"),
            "description": _s("Description, up to 1000 characters.", "国勢調査の人口等基本集計...", nullable=True),
            "organization": _s("Publishing organization.", "総務省", nullable=True),
            "license": _s("Licence title or id.", "CC-BY-4.0", nullable=True),
            "updated_at": _s("Portal metadata modification time.", "2026-03-01T09:12:44.512345", nullable=True),
            "tags": _arr(_s("A tag.", "人口"), "Portal tags.", ["人口", "国勢調査"]),
            "landing_url": _s("Dataset page or source URL when the portal gives one.",
                              "https://www.e-stat.go.jp/stat-search/files?toukei=00200521", nullable=True),
            "resources": _arr(_obj({
                "url": _s("Download URL.", "https://www.e-stat.go.jp/stat-search/file-download?statInfId=000032143614"),
                "format": _s("CSV, JSON, XLS, HTML...", "CSV", nullable=True),
                "name": _s("Resource name.", "人口等基本集計 全国結果", nullable=True),
                "last_modified": _s("Resource modification time.", "2026-02-20T00:00:00", nullable=True),
                "size_bytes": _i("Size when the portal states it.", 1048576, nullable=True),
            }, ["url"], "One downloadable resource."), "Up to 25 resources, ready for fetch.raw."),
            "resource_count": _i("Resources the dataset has in total.", 3),
        }, ["portal", "region", "country", "id", "title", "tags", "resources", "resource_count"], "One dataset."),
            "Datasets found, most recently updated first."),
        "result_count": _i("Datasets returned.", 10),
        "limit_per_portal": _i("Results requested per portal.", 5),
        "checked_at": _CHECKED_AT,
    }, ["query", "region", "portals_searched", "portals_ok", "portals_failed", "total_matches", "results",
        "result_count", "limit_per_portal", "checked_at"]),

    "news.search": _obj({
        "query": _s("Topic searched, as given; null when only a symbol was asked.", "半導体", nullable=True),
        "symbol": _s("Ticker whose own feed was read; null when none.", "7203.T", nullable=True),
        "language": _s("BCP-47 language the edition was chosen for.", "ja"),
        "edition": _nobj({"hl": _s("Google News language code.", "ja"), "gl": _s("Country edition.", "JP"),
                          "ceid": _s("Edition id.", "JP:ja"),
                          "verified": _b("True for the 71 editions verified to answer; false for a pair tried on request.", True)},
                         ["hl", "gl", "ceid", "verified"], "The Google News edition used; null when no query was given."),
        "sources_searched": _arr(_s("Feed id.", "google_news:JP:ja"), "Feeds this call asked.", ["google_news:JP:ja"]),
        "sources_ok": _arr(_s("Feed id.", "google_news:JP:ja"), "Feeds that answered.", ["google_news:JP:ja"]),
        "sources_failed": _arr(_obj({"source": _s("Feed id.", "yahoo_finance:7203.T"),
                                     "reason": _s("Why it did not answer this call.", "Yahoo Finance RSS (7203.T) timed out")},
                                    ["source", "reason"], "A feed that failed for this call."),
                               "Feeds that did not answer, with the reason; never silently dropped.", []),
        "articles": _arr(_obj({
            "title": _s("Headline, publisher suffix removed.", "半導体株が反発、関税協議の進展で"),
            "url": _s("Article link (Google News links resolve to the publisher).", "https://news.google.com/rss/articles/CBMi..."),
            "source_name": _s("Publisher.", "日本経済新聞", nullable=True),
            "source_url": _s("Publisher site.", "https://www.nikkei.com", nullable=True),
            "published_at": _s("Publication time (UTC).", "2026-09-27T02:32:00Z", nullable=True),
            "summary": _s("Plain-text summary when the feed carries one.", "半導体関連株が...", nullable=True),
            "feed": _enum(["google_news", "yahoo_finance"], "Which feed it came from.", "google_news"),
        }, ["title", "url", "source_name", "source_url", "published_at", "summary", "feed"], "One article."),
            "Articles, newest first, duplicates by title removed."),
        "article_count": _i("Articles returned.", 20),
        "limit": _i("Most articles asked for.", 20),
        "since_hours": _i("Window applied, in hours; null when none.", 24, nullable=True),
        "notes": _arr(_s("A caveat.", "Edition XX:yy returned nothing for this query; results are from the en/US edition."),
                      "Caveats; empty when none.", []),
        "checked_at": _CHECKED_AT,
    }, ["query", "symbol", "language", "edition", "sources_searched", "sources_ok", "sources_failed", "articles",
        "article_count", "limit", "since_hours", "notes", "checked_at"]),

    "data.macro": _obj({
        "indicator": _obj({"alias": _s("Alias used; null for a raw code.", "inflation", nullable=True),
                           "code": _s("World Bank indicator code; null for Eurostat monthly series.", "FP.CPI.TOTL.ZG", nullable=True),
                           "name": _s("The source's name for the series.", "Inflation, consumer prices (annual %)", nullable=True),
                           "unit": _s("Unit when the source states it.", "% change on a year earlier (HICP)", nullable=True)},
                          ["alias", "code", "name", "unit"], "What was measured."),
        "country": _obj({"code": _s("Country code as the source spells it.", "JP"),
                         "name": _s("Country name from the source.", "Japan", nullable=True)}, ["code", "name"], "Where."),
        "frequency": _enum(["annual", "monthly"], "Period of the observations.", "annual"),
        "source": _enum(["world-bank", "eurostat"], "Which official source answered.", "world-bank"),
        "observations": _arr(_obj({"period": _s("Year (2025) or month (2026-08).", "2025"),
                                   "value": _n("Value; null when not yet published.", 3.17, nullable=True)},
                                  ["period", "value"], "One observation."), "Oldest first."),
        "observation_count": _i("Observations returned.", 5),
        "latest": _nobj({"period": _s("Period.", "2025"), "value": _n("Value.", 3.17)}, ["period", "value"],
                        "Most recent published value; null when none in the window."),
        "previous": _nobj({"period": _s("Period.", "2024"), "value": _n("Value.", 2.74)}, ["period", "value"],
                          "The published value before it; null when none."),
        "change": _nobj({"absolute": _n("latest minus previous.", 0.43),
                         "pct": _n("Change as a percentage of previous; null when previous is zero.", 15.69, nullable=True)},
                        ["absolute", "pct"], "Latest versus previous; null when either is missing."),
        "as_of": _s("The source's own update stamp.", "2026-07-13", nullable=True),
        "source_url": _s("Where the numbers came from.", "https://api.worldbank.org/v2/country/JP/indicator/FP.CPI.TOTL.ZG?format=json", nullable=True),
        "notes": _arr(_s("A caveat.", "1 of 5 periods have no published value yet."), "Caveats; empty when none.", []),
        "checked_at": _CHECKED_AT,
    }, ["indicator", "country", "frequency", "source", "observations", "observation_count", "latest", "previous",
        "change", "as_of", "source_url", "notes", "checked_at"]),

    "opendata.table": _obj({
        "source_url": _s("The URL as given.", "https://www.data.go.kr/data/15005995/fileData.do"),
        "resolved_from": _s("The dataset page that was resolved to a file link; null when a file link was given.",
                            "https://www.data.go.kr/data/15005995/fileData.do", nullable=True),
        "file_url": _s("The file actually fetched.", "https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId=FILE_000000003522968&fileDetailSn=1&insertDataPrcus=N"),
        "final_url": _s("After redirects.", "https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId=FILE_000000003522968&fileDetailSn=1&insertDataPrcus=N", nullable=True),
        "filename": _s("File name from the server or the URL.", "울산광역시_인구 현황(20251112).csv", nullable=True),
        "content_type": _s("Server content type.", "application/octet-stream", nullable=True),
        "bytes": _i("File size in bytes.", 12331),
        "format": _enum(["csv", "tsv", "xlsx", "json"], "Parsed format.", "csv"),
        "encoding": _s("Text encoding used; null for XLSX.", "cp949", nullable=True),
        "sheet": _s("XLSX sheet read; null otherwise.", "Sheet1", nullable=True),
        "sheets": _arr(_s("Sheet name.", "Sheet1"), "All sheet names for an XLSX; empty otherwise.", []),
        "columns": _arr(_s("Column name from the header row.", "행정구역명"), "Columns in file order.",
                        ["행정구역코드", "행정구역명", "성별", "내국인"]),
        "column_count": _i("Number of columns.", 14),
        "rows": _arr(_obj({}, [], "One row keyed by column; numbers typed, blanks null.", additionalProperties=True,
                          examples=[{"행정구역코드": 31, "행정구역명": "울산광역시", "성별": "남자", "내국인": 564888}]),
                     "Rows in file order, up to max_rows."),
        "row_count": _i("Rows returned.", 5),
        "total_rows": _i("Data rows in the file (excluding the header).", 45),
        "truncated": _b("True when total_rows exceeds max_rows.", True),
        "notes": _arr(_s("A caveat.", "45 data rows in the file; the first 5 are returned (max_rows)."), "Caveats; empty when none.", []),
        "checked_at": _CHECKED_AT,
    }, ["source_url", "resolved_from", "file_url", "final_url", "filename", "content_type", "bytes", "format", "encoding",
        "sheet", "sheets", "columns", "column_count", "rows", "row_count", "total_rows", "truncated", "notes", "checked_at"]),

    "travel.flights": _obj({
        "origin": _PLACE,
        "destination": _PLACE,
        "departure_date": _s("Outbound date, YYYY-MM-DD.", "2026-11-10"),
        "return_date": _s("Return date when a return leg was searched.", "2026-11-17", nullable=True),
        "passengers": _obj({"adults": _i("Adults searched for.", 1),
                            "children_ages": _arr(_i("A child's age.", 8), "Children by age.", [])},
                           ["adults", "children_ages"], "Who the offers are priced for."),
        "cabin_class": _enum(["economy", "premium_economy", "business", "first"], "Cabin searched.", "economy"),
        "sort": _enum(["price", "duration"], "Sort order applied.", "price"),
        "offers": _arr(_obj({
            "id": _s("Provider offer id (quote it to book).", "off_0000BAq4P2Ku1h9eps72O0"),
            "airline": _CARRIER,
            "total_amount": _n("Total price including tax.", 218.93, nullable=True),
            "base_amount": _n("Fare before tax.", 185.53, nullable=True),
            "tax_amount": _n("Tax and fees.", 33.40, nullable=True),
            "currency": _s("ISO 4217 currency of the amounts.", "USD", nullable=True),
            "expires_at": _s("When the airline withdraws this offer (UTC).", "2026-09-27T16:18:23Z", nullable=True),
            "slices": _arr(_obj({
                "origin": _AIRPORT, "destination": _AIRPORT,
                "duration_seconds": _i("Door-to-door duration of this leg.", 28680, nullable=True),
                "fare_brand": _s("Airline fare brand.", "Basic", nullable=True),
                "segments": _arr(_obj({
                    "carrier": _CARRIER,
                    "flight_number": _s("Marketing flight number.", "9368", nullable=True),
                    "operating_carrier": _CARRIER,
                    "origin": _AIRPORT, "destination": _AIRPORT,
                    "departing_at": _s("Local departure time.", "2026-11-10T06:58:00", nullable=True),
                    "arriving_at": _s("Local arrival time.", "2026-11-10T09:56:00", nullable=True),
                    "duration_seconds": _i("Flight time.", 28680, nullable=True),
                    "aircraft": _s("Aircraft name when stated.", "Boeing 777-300", nullable=True),
                    "distance_km": _n("Great-circle distance when stated.", 5539.8, nullable=True),
                }, ["carrier", "flight_number", "operating_carrier", "origin", "destination", "departing_at",
                    "arriving_at", "duration_seconds", "aircraft", "distance_km"], "One flight."), "Flights in order."),
            }, ["origin", "destination", "duration_seconds", "fare_brand", "segments"], "One leg of the journey."),
                "Outbound leg, then the return leg when searched."),
            "stops": _i("Most connections on any leg (0 = non-stop).", 0),
            "total_duration_seconds": _i("All legs' durations added.", 28680, nullable=True),
            "refund_before_departure": _CONDITION,
            "change_before_departure": _CONDITION,
            "emissions_kg": _n("Estimated CO2 for all passengers.", 637.0, nullable=True),
            "instant_payment_required": _b("True when the airline demands payment at booking time.", False),
            "price_guarantee_expires_at": _s("Until when the price is held once an order is started.",
                                             "2026-09-29T15:48:23Z", nullable=True),
            "identity_documents_required": _b("True when passport details are needed to book.", False),
        }, ["id", "airline", "total_amount", "base_amount", "tax_amount", "currency", "expires_at", "slices",
            "stops", "total_duration_seconds", "refund_before_departure", "change_before_departure",
            "emissions_kg", "instant_payment_required", "price_guarantee_expires_at",
            "identity_documents_required"], "One airline offer."), "Offers in the requested order, up to max_offers."),
        "offer_count": _i("Offers returned.", 3),
        "offers_available": _i("Offers the airlines returned before max_offers was applied.", 198),
        "cheapest_total": _nobj({"amount": _n("Lowest total price.", 218.93), "currency": _s("Its currency.", "USD")},
                                ["amount", "currency"], "Cheapest offer's total; null when no offer was priced."),
        "fastest_duration_seconds": _i("Shortest total duration across offers.", 28680, nullable=True),
        "live_mode": _b("True: bookable airline offers. False: the provider's practice data, never sold on the public node.", True),
        "offer_request_id": _s("Provider search id.", "orq_0000BAq4P2Ku1h9eps72NZ", nullable=True),
        "source": _const("duffel", "Data source."),
        "notes": _arr(_s("A caveat.", "No airline returned an offer for this search."), "Caveats; empty when none.", []),
        "checked_at": _CHECKED_AT,
    }, ["origin", "destination", "departure_date", "return_date", "passengers", "cabin_class", "sort", "offers",
        "offer_count", "offers_available", "cheapest_total", "fastest_duration_seconds", "live_mode",
        "offer_request_id", "source", "notes", "checked_at"]),

    "traffic.route": _obj({
        "origin": _s("Origin as given.", "Ferry Building, San Francisco, CA"),
        "destination": _s("Destination as given.", "Oakland City Hall, Oakland, CA"),
        "travel_mode": _enum(["DRIVE", "TWO_WHEELER", "WALK", "BICYCLE", "TRANSIT"], "Travel mode used.", "DRIVE"),
        "traffic": _enum(["aware", "optimal", "none"],
                         "Traffic handling used: aware (live traffic), optimal (live traffic, best route quality), none (no traffic; always none for WALK, BICYCLE and TRANSIT).",
                         "aware"),
        "departure_time": _s("Departure time sent to Google (RFC 3339, UTC); null when the trip starts now.",
                             "2026-09-27T16:30:00Z", nullable=True),
        "distance_meters": _i("Route length in metres.", 17011),
        "duration_seconds": _i("Travel time in seconds, with traffic when `traffic` is aware or optimal.", 1152),
        "static_duration_seconds": _i("Travel time in seconds without traffic; null when Google does not state it.",
                                      1281, nullable=True),
        "delay_seconds": _i("duration_seconds minus static_duration_seconds (negative when traffic is lighter than typical); null when either is unknown.",
                            -129, nullable=True),
        "duration_text": _s("Google's localized travel time.", "19 mins", nullable=True),
        "static_duration_text": _s("Google's localized travel time without traffic.", "21 mins", nullable=True),
        "distance_text": _s("Google's localized distance (metric).", "17.0 km", nullable=True),
        "description": _s("Route description as Google names it (main roads); null when not given.",
                          "I-80 E and I-580 E", nullable=True),
        "warnings": _arr(_s("A warning Google attaches to the route.", "This route has tolls."),
                         "Google's warnings for the route, in the order given; empty when none.", []),
        "advisory": _nobj({}, [], "Google's travelAdvisory object for the route (tolls, speed reading intervals, fuel) exactly as returned, {} when it has nothing to say; null when absent."),
        "source": _const("google-routes", "Data source: Google Routes API computeRoutes."),
        "as_of": _s("The departure time the answer was computed for: `departure_time`, else `checked_at`.",
                    "2026-09-27T16:30:00Z"),
        "checked_at": _CHECKED_AT,
    }, ["origin", "destination", "travel_mode", "traffic", "departure_time", "distance_meters", "duration_seconds",
        "static_duration_seconds", "delay_seconds", "duration_text", "static_duration_text", "distance_text",
        "description", "warnings", "advisory", "source", "as_of", "checked_at"]),

    "video.youtube": _obj({
        "query": _s("The query as given; null when video_ids were looked up.", "open source licensing", nullable=True),
        "video_ids": {"type": ["array", "null"], "items": {"type": "string"},
                      "description": "The ids asked for, in order; null when a query was searched.",
                      "examples": [["IJhS_fv5Ktk"]]},
        "total_results": _i("YouTube's estimated total matches for the query (pageInfo.totalResults); null when not sent.",
                            19113, nullable=True),
        "items": _arr(_obj({
            "video_id": _s("YouTube video id.", "IJhS_fv5Ktk"),
            "url": _s("Watch URL.", "https://www.youtube.com/watch?v=IJhS_fv5Ktk"),
            "title": _s("Title.", "Open source licensing explained", nullable=True),
            "description": _s("Description, cut at 500 characters; null when empty.",
                              "A short intro to open source licences.", nullable=True),
            "channel_id": _s("Uploading channel id.", "UCoBPd2jgYzQ5m8k3f1nR9wA", nullable=True),
            "channel_title": _s("Uploading channel name.", "Coinbase Developer Platform", nullable=True),
            "published_at": _s("Upload time as YouTube states it (ISO 8601).", "2026-05-14T16:00:12Z", nullable=True),
            "duration_seconds": _i("Length in whole seconds from contentDetails.duration; 0 for a live or upcoming "
                                   "stream; null when YouTube did not return the video's details.", 754, nullable=True),
            "view_count": _i("Views; null when YouTube does not send it.", 52004, nullable=True),
            "like_count": _i("Likes; null when the uploader hides them.", 2225, nullable=True),
            "comment_count": _i("Comments; null when hidden or comments are off.", 112, nullable=True),
            "live": {"type": ["boolean", "null"],
                     "description": "Live or upcoming broadcast (liveBroadcastContent is not none); null when not stated.",
                     "examples": [False]},
            "thumbnail_url": _s("Thumbnail URL, the high size when YouTube offers it.",
                                "https://i.ytimg.com/vi/IJhS_fv5Ktk/hqdefault.jpg", nullable=True),
        }, ["video_id", "url", "title", "description", "channel_id", "channel_title", "published_at",
            "duration_seconds", "view_count", "like_count", "comment_count", "live", "thumbnail_url"],
            "One video, with its live statistics."),
            "Videos, in YouTube's search order (or the order of the ids asked); ids YouTube does not know are absent."),
        "item_count": _i("Videos returned.", 3),
        "source": _const("youtube-data-api", "Data source."),
        "as_of": _s("The same instant as checked_at: YouTube does not stamp its statistics.", "2026-09-27T12:00:00Z"),
        "checked_at": _CHECKED_AT,
    }, ["query", "video_ids", "total_results", "items", "item_count", "source", "as_of", "checked_at"]),

    "social.bluesky": _obj({
        "mode": _enum(["profile", "search_actors", "thread"], "Mode run.", "profile"),
        "actor": _s("Actor asked for (profile mode).", "bsky.app", nullable=True),
        "query": _s("Query asked for (search_actors mode).", "coinbase", nullable=True),
        "uri": _s("Post AT-URI asked for (thread mode).", "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.post/3mw2cdr44fc2a", nullable=True),
        "profile": _nobj(_BSKY_PROFILE["properties"], _BSKY_PROFILE["required"], "The account, in profile mode; null otherwise."),
        "posts": _arr(_BSKY_POST, "Latest posts in profile mode, newest first; empty otherwise."),
        "actors": _arr(_BSKY_ACTOR, "Accounts found in search_actors mode; empty otherwise."),
        "thread": _nobj({
            "post": _BSKY_POST,
            "replies": _arr(_BSKY_POST, "Replies flattened to the requested depth, in thread order."),
            "reply_count": _i("Replies returned.", 2),
        }, ["post", "replies", "reply_count"], "The thread, in thread mode; null otherwise."),
        "post_count": _i("Posts returned.", 3),
        "actor_count": _i("Accounts returned.", 0),
        "source": _const("bluesky-public-appview", "Data source."),
        "as_of": _s("Indexing time of the newest post returned, else checked_at.", "2026-09-26T20:00:00.000Z"),
        "checked_at": _CHECKED_AT,
    }, ["mode", "actor", "query", "uri", "profile", "posts", "actors", "thread", "post_count", "actor_count",
        "source", "as_of", "checked_at"]),

    "social.mastodon": _obj({
        "mode": _enum(["hashtag", "account", "search", "trends"], "Mode run.", "hashtag"),
        "instance": _s("Instance queried.", "mastodon.social"),
        "tag": _s("Hashtag asked for (hashtag mode).", "opensource", nullable=True),
        "acct": _s("Account asked for (account mode).", "Gargron", nullable=True),
        "query": _s("Query asked for (search mode).", "opensource", nullable=True),
        "kind": _s("accounts or hashtags (search mode).", "accounts", nullable=True),
        "account": _nobj(_MASTO_ACCOUNT["properties"], _MASTO_ACCOUNT["required"], "The account, in account mode; null otherwise."),
        "statuses": _arr(_MASTO_STATUS, "Statuses, newest first (hashtag and account modes); empty otherwise."),
        "accounts": _arr(_MASTO_ACCOUNT, "Accounts found (search mode, kind accounts); empty otherwise."),
        "hashtags": _arr(_MASTO_TAG, "Hashtags (search mode, kind hashtags, or trends); empty otherwise."),
        "status_count": _i("Statuses returned.", 3),
        "source": _const("mastodon-public-api", "Data source."),
        "as_of": _s("Creation time of the newest status returned, else checked_at.", "2026-09-27T01:00:00.000Z"),
        "checked_at": _CHECKED_AT,
    }, ["mode", "instance", "tag", "acct", "query", "kind", "account", "statuses", "accounts", "hashtags",
        "status_count", "source", "as_of", "checked_at"]),

    "social.x_pulse": _obj({
        "query": _s("The query as given.", "open source"),
        "effective_query": _s("The query sent to X (lang: and -is:retweet added unless given).", "open source lang:en -is:retweet"),
        "window": _obj({"start": _s("Window start (RFC 3339).", "2026-09-24T09:00:00Z"),
                        "end": _s("Window end (RFC 3339), a few seconds before the call.", "2026-09-27T08:59:45Z"),
                        "days": _i("Days covered.", 3)}, ["start", "end", "days"], "The window analysed."),
        "granularity": _enum(["hour", "day"], "Volume bucket size.", "day"),
        "volume": _obj({
            "total": _i("Posts matching the query in the window.", 21636),
            "buckets": _arr(_obj({"start": _s("Bucket start.", "2026-09-26T00:00:00.000Z", nullable=True),
                                  "end": _s("Bucket end.", "2026-09-27T00:00:00.000Z", nullable=True),
                                  "count": _i("Posts in the bucket.", 7200)}, ["start", "end", "count"], "One bucket."),
                            "Counts per bucket, oldest first."),
            "bucket_count": _i("Buckets returned.", 4),
        }, ["total", "buckets", "bucket_count"], "Post volume from X's recent-counts endpoint."),
        "sample": _obj({"requested": _i("Posts asked to read.", 20), "read": _i("Posts actually read.", 20),
                        "pages": _i("Search pages fetched.", 1),
                        "x_cost_usd": _n("What X bills for the posts read, at $0.005 each.", 0.1)},
                       ["requested", "read", "pages", "x_cost_usd"], "The sample the engagement figures rest on."),
        "engagement": _nobj({
            "posts_read": _i("Posts in the sample.", 20),
            "likes_total": _i("Likes summed.", 340, nullable=True), "reposts_total": _i("Reposts summed.", 41, nullable=True),
            "replies_total": _i("Replies summed.", 27, nullable=True), "quotes_total": _i("Quotes summed.", 5, nullable=True),
            "impressions_total": _i("Impressions summed.", 51200, nullable=True),
            "bookmarks_total": _i("Bookmarks summed.", 12, nullable=True),
            "mean_likes": _n("Mean likes per post.", 17.0, nullable=True),
            "mean_impressions": _n("Mean impressions per post.", 2560.0, nullable=True),
            "max_likes": _i("Most-liked post's likes.", 120, nullable=True),
            "max_impressions": _i("Most-seen post's impressions.", 18000, nullable=True),
            "engagement_rate_pct": _n("(likes+reposts+replies+quotes)/impressions, percent.", 0.807, nullable=True),
            "languages": _obj({}, [], "Posts per language code.", additionalProperties={"type": "integer"}, examples=[{"en": 20}]),
            "top_hashtags": _arr(_obj({"tag": _s("Hashtag, lower-cased.", "opensource"), "posts": _i("Posts using it.", 4)},
                                      ["tag", "posts"], "One hashtag."), "Up to 10 most-used hashtags.", [{"tag": "opensource", "posts": 4}]),
            "with_hashtags_pct": _n("Share of posts with a hashtag.", 35.0, nullable=True),
            "with_links_pct": _n("Share of posts linking outside X.", 40.0, nullable=True),
            "with_media_pct": _n("Share of posts with photos or video.", 15.0, nullable=True),
            "possibly_sensitive_pct": _n("Share X flags as possibly sensitive.", 0.0, nullable=True),
        }, ["posts_read", "likes_total", "reposts_total", "replies_total", "quotes_total", "impressions_total",
            "bookmarks_total", "mean_likes", "mean_impressions", "max_likes", "max_impressions", "engagement_rate_pct",
            "languages", "top_hashtags", "with_hashtags_pct", "with_links_pct", "with_media_pct", "possibly_sensitive_pct"],
            "Engagement aggregates over the sample; null when sample is 0."),
        "source": _const("x-api-v2", "Data source."),
        "as_of": _s("End of the window analysed.", "2026-09-27T08:59:45Z"),
        "checked_at": _CHECKED_AT,
    }, ["query", "effective_query", "window", "granularity", "volume", "sample", "engagement", "source", "as_of", "checked_at"]),

    "security.mcp_inspect": _obj({
        "url": _s("The MCP endpoint inspected.", "https://mcp.example.com/mcp"),
        "reachable": _b("Whether the server answered the MCP initialize handshake.", True),
        "requires_auth": _b("Whether an unauthenticated initialize was refused.", False),
        "initialize_status": _i("HTTP status of the initialize call.", 200),
        "protocol_version": _s("MCP protocol version the server negotiated.", "2025-06-18", nullable=True),
        "server_name": _s("serverInfo.name.", "example-mcp", nullable=True),
        "server_version": _s("serverInfo.version.", "1.0.0", nullable=True),
        "tool_count": _i("Tools listed.", 3),
        "tools": _arr(_MCP_TOOL, "Every tool listed, with its annotations."),
        "tools_without_readonly_annotation": _arr(
            _s("A tool name.", "delete_records"),
            "Tools that do not declare readOnlyHint: the ones an agent should treat as able to change state.",
            ["delete_records"]),
        "tools_error": _s("Why tools/list failed, when it did.", None, nullable=True),
    }, ["url", "reachable", "requires_auth", "initialize_status", "tool_count", "tools",
        "tools_without_readonly_annotation"]),

    "maps.places": _obj({
        "query": _s("The place query.", "coffee near Union Square, San Francisco"),
        "result": _free("Places found, in Google Maps Grounding Lite's own response shape.",
                        {"places": [{"name": "Blue Bottle Coffee", "formattedAddress": "66 Mint St, San Francisco, CA"}]},
                        "object"),
    }, ["query", "result"]),

    "maps.route": _obj({
        "origin": _s("Origin as given.", "San Francisco, CA"),
        "destination": _s("Destination as given.", "Oakland, CA"),
        "travel_mode": _enum(["DRIVE", "WALK", "BICYCLE", "TRANSIT"], "Travel mode used.", "DRIVE"),
        "result": _free("The route, in Google Maps Grounding Lite's own response shape.",
                        {"routes": [{"distanceMeters": 19870, "duration": "1362s"}]}, "object"),
    }, ["origin", "destination", "travel_mode", "result"]),

    "maps.weather": _obj({
        "location": _s("Location as given.", "San Francisco, CA"),
        "result": _free("Current conditions, in Google Maps Grounding Lite's own response shape.",
                        {"temperature": {"degrees": 17.2, "unit": "CELSIUS"}, "weatherCondition": {"type": "PARTLY_CLOUDY"}},
                        "object"),
    }, ["location", "result"]),

    "research.brief": _obj({
        "url": _s("The page read.", "https://example.com"),
        "final_url": _s("The URL after redirects.", "https://example.com/", nullable=True),
        "title": _s("Page title.", "Example Domain", nullable=True),
        "question": _s("The question answered.", "What does this page offer, who is it for, and what is the pricing?"),
        "brief": _s("The answer, from the page only.", "The page is a placeholder domain reserved for documentation examples..."),
        "source": _PAGE_SOURCE,
        "model": _MODEL,
    }, ["url", "question", "brief", "source", "model"]),

    "research.page_facts": _obj({
        "url": _s("The page read.", "https://example.com"),
        "final_url": _s("The URL after redirects.", "https://example.com/", nullable=True),
        "title": _s("Page title.", "Example Domain", nullable=True),
        "fields": _obj({}, [], "Exactly the requested field names; null where the page does not state a value.",
                       additionalProperties=True,
                       examples=[{"title": "Example Domain", "pricing": None}]),
        "model": _MODEL,
    }, ["url", "fields", "model"]),

    "market.intel": _obj({
        "product_id": _s("Spot product read.", "BTC-USD"),
        "spot": _SPOT_QUOTE,
        "prediction_markets": _arr(_MARKET, "Matching prediction markets, highest volume first."),
        "news": _NEWS,
        "news_note": _NEWS_NOTE,
        "analysis": _s("What the two sources together do and do not support.",
                       "Spot is up 1.8% on the day while the top market prices a year-end close above $100k at 62%..."),
        "model": _MODEL,
        "disclaimer": _s("Not investment advice.",
                         "Market data and implied probabilities only. Not investment advice and not a forecast by HubVibe."),
    }, ["product_id", "spot", "prediction_markets", "news", "news_note", "analysis", "model", "disclaimer"]),

    "research.web": _obj({
        "question": _s("The question researched.", "What is the HTTP 402 status code for?"),
        "answer": _s("Cited answer; every claim carries [n].",
                     "HTTP 402 Payment Required is reserved for payments; machine-payment protocols use it to quote a price [1]..."),
        "sources": _arr(_CITED_SOURCE, "Sources read, numbered as cited."),
        "partial": _arr(_UNREAD_SOURCE, "Sources found but not read, with the reason.", []),
        "model": _MODEL,
    }, ["question", "answer", "sources", "partial", "model"]),

    "research.company": _obj({
        "company": _s("The company researched.", "Anthropic"),
        "report": _s("Cited brief: what it does, products, notable facts; thin or conflicting evidence is called out.",
                     "Anthropic is an AI safety company that builds the Claude model family [1]..."),
        "sources": _arr(_CITED_SOURCE, "Sources read, numbered as cited."),
        "partial": _arr(_UNREAD_SOURCE, "Sources found but not read, with the reason.", []),
        "news": _NEWS,
        "news_note": _NEWS_NOTE,
        "model": _MODEL,
    }, ["company", "report", "sources", "partial", "news", "news_note", "model"]),

    "verify.claims": _obj({
        "claims": _arr(_s("A claim, as given.", "HubVibe sells machine-payable site audits."),
                       "The claims checked, in order.", ["HubVibe sells machine-payable site audits."]),
        "verdicts": _arr(_VERDICT, "One verdict per claim, same order."),
        "sources_read": _arr(_CITED_SOURCE, "Sources read, numbered as cited in source_n."),
        "sources_unread": _arr(_UNREAD_SOURCE, "Sources that could not be read, with the reason.", []),
        "model": _MODEL,
    }, ["claims", "verdicts", "sources_read", "sources_unread", "model"]),
}


# --- the envelope every /work 200 is wrapped in (app/workers/router.py) ---------

_PROVENANCE = _obj({
    "steps": _arr(_obj({
        "step": _s("Step name inside the worker.", "quote"),
        "ok": _b("Whether the step succeeded.", True),
        "provider": _s("Provider that served the step.", "coinbase-advanced-trade-public"),
        "ms": _i("Milliseconds the step took.", 312),
        "reason": _s("Failure reason, on a failed step.", "provider_timeout"),
    }, ["step", "ok", "ms"], "One step of the job."), "Every step the job ran, in order."),
    "providers_used": _arr(_s("Provider name.", "coinbase-advanced-trade-public"),
                           "Providers that served the job.", ["coinbase-advanced-trade-public"]),
    "attempts": _i("Provider attempts made, including retries and failovers.", 1),
    "elapsed_ms": _i("Wall-clock milliseconds for the whole job.", 340),
}, ["steps", "providers_used", "attempts", "elapsed_ms"],
    "How the result was produced: which providers ran and how long each took.")

RESPONSE_ENVELOPE = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "description": (
        "The 200 body of every paid /work call. `result` is the worker's own "
        "output (its schema is per route); everything else is the same on every "
        "/work route. A receipt for the job is at `receipt_url`."),
    "properties": {
        "status": _const("ok", "Present only on a delivered result."),
        "worker": _s("Catalog name of the worker that ran.", "market.quote"),
        "price_usd": _n("What this call cost, in USD.", 0.02),
        "result": {"type": "object", "description": "The worker's output; see the route's own schema."},
        "provenance": _PROVENANCE,
        "receipt_id": _s("Receipt id for this job.", "rcpt_9f1c2b3a4d5e6f70"),
        "receipt_url": _s("Where to fetch the machine-readable receipt (free, no payment).",
                          "/work/receipts/rcpt_9f1c2b3a4d5e6f70"),
        "billing_warning": _s("Present only when the charge was recorded with a caveat.",
                              "settlement pending"),
    },
    "required": ["status", "worker", "price_usd", "result", "provenance", "receipt_id", "receipt_url"],
}


def response_schema(worker) -> dict:
    """The full 200 schema of one route: the envelope with this worker's
    result schema in place of the generic `result`."""
    schema = {k: v for k, v in RESPONSE_ENVELOPE.items() if k != "properties"}
    schema["properties"] = {**RESPONSE_ENVELOPE["properties"], "result": worker.output_schema}
    schema["title"] = f"{worker.name} response"
    return schema


# --- examples generated FROM the schemas, so they cannot drift ----------------

_FORMAT_EXAMPLES = {
    "uri": "https://example.com",
    "url": "https://example.com",
    "uri-reference": "https://example.com",
    "iri": "https://example.com",
    "email": "agent@example.com",
    "date": "2026-01-01",
    "date-time": "2026-01-01T00:00:00Z",
    "time": "00:00:00Z",
    "hostname": "example.com",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "uuid": "00000000-0000-4000-8000-000000000000",
}


def example_from_schema(schema: dict) -> Any:
    """A value that satisfies `schema`, preferring the `examples` it carries."""
    if "examples" in schema and schema["examples"]:
        return schema["examples"][0]
    if "const" in schema:
        return schema["const"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((t for t in kind if t != "null"), kind[0] if kind else None)
    if kind == "object" or (kind is None and "properties" in schema):
        return {name: example_from_schema(prop)
                for name, prop in (schema.get("properties") or {}).items()}
    if kind == "array":
        items = schema.get("items") or {}
        return [example_from_schema(items)] if items else []
    if kind == "string":
        # A strict validator checks `format`: the word "example" in a field
        # declared a URI made the audit records invalid to Coinbase's index,
        # which kept the old rows for /audit/wcag and /audit/bundle.
        return _FORMAT_EXAMPLES.get(schema.get("format"), "example")
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return True
    return None


def output_example(worker) -> dict:
    """An example `result` for this worker, generated from its schema."""
    return example_from_schema(worker.output_schema)


def response_example(worker) -> dict:
    """An example 200 body for this worker: the envelope around its result."""
    envelope = example_from_schema(RESPONSE_ENVELOPE)
    envelope.pop("billing_warning", None)  # only present on a caveated charge
    envelope["worker"] = worker.name
    envelope["price_usd"] = worker.price_usd
    envelope["result"] = output_example(worker)
    return envelope


# --- representative queries for /.well-known/ard.json ----------------------------
# 2-5 per worker, per the ARD spec. Phrased as the ask an agent would search
# for, not as our own route names.

REPRESENTATIVE_QUERIES = {
    "chain.network": [
        "what is the current block number on Base",
        "current gas price on Base mainnet",
        "is the Base chain live right now",
    ],
    "chain.address": [
        "ETH balance and transaction count of a Base address",
        "is this Base address a contract or a wallet",
        "look up an address on Base mainnet",
    ],
    "chain.transaction": [
        "did this Base transaction succeed",
        "look up a transaction hash on Base with its receipt",
        "how much gas did a Base transaction use",
    ],
    "chain.rpc": [
        "call eth_getLogs on Base for a contract",
        "raw JSON-RPC read against Base mainnet",
        "eth_call a view function on Base",
    ],
    "market.quote": [
        "current BTC-USD price on Coinbase",
        "24 hour price change and volume for ETH-USD",
        "spot price of a crypto pair",
    ],
    "market.prediction": [
        "what odds do prediction markets give on an event",
        "top Polymarket markets by volume",
        "implied probability of an outcome on Polymarket",
    ],
    "market.rates": [
        "exchange rates for USD against every currency",
        "how much is one EUR in BTC",
        "current fiat and crypto exchange rate table",
    ],
    "market.ticker": [
        "best bid and ask for BTC-USD",
        "live order book top of book for a Coinbase product",
        "bid ask spread on ETH-USD",
    ],
    "prediction.market": [
        "look up a Polymarket market by slug",
        "current probabilities for a specific prediction market",
    ],
    "prediction.events": [
        "list the biggest prediction market events right now",
        "which Polymarket events have the most volume",
    ],
    "extract.page": [
        "extract the readable text and links from a web page",
        "read a JavaScript-rendered page and return its content",
        "get the title, description and text of a URL",
    ],
    "fetch.raw": [
        "fetch a URL and return the raw status, headers and body",
        "what HTTP status and headers does this URL return",
        "download the raw HTML of a page without rendering",
    ],
    "search.web": [
        "search the web and answer with sources",
        "live web search with citations for a question",
        "find recent web results about a topic",
    ],
    "llm.analyze": [
        "answer a question about this text using only the text",
        "summarize the key points of a document",
        "analyze provided material without guessing beyond it",
    ],
    "llm.extract": [
        "extract named fields from text as JSON",
        "pull structured data out of unstructured text",
        "turn a document into a JSON object with these keys",
    ],
    "llm.generate": [
        "generate text from a prompt with a chosen model",
        "raw LLM completion with a system prompt",
        "run a prompt on Gemini or Claude and return the text",
    ],
    "code.execute": [
        "run this Python code and return the output",
        "execute a script in a sandbox",
        "compute a result by running code",
    ],
    "image.generate": [
        "generate an image from a text prompt",
        "create an illustration with Imagen",
        "text to image at a given aspect ratio",
    ],
    "speech.synthesize": [
        "convert text to speech audio",
        "generate an MP3 voice-over from text",
        "text to speech with a named voice",
    ],
    "speech.transcribe": [
        "transcribe a short audio clip to text",
        "speech to text for a base64 WAV file",
        "what is said in this audio",
    ],
    "video.generate": [
        "generate a short video clip from a text prompt",
        "text to video with Veo",
        "create a four second video from a description",
    ],
    "stats.probability": [
        "linear regression with p-values on a list of x y points",
        "fit a normal distribution and get the probability a value falls below a threshold",
        "is the correlation between x and y statistically significant at alpha 0.05",
        "predict y at a new x with a prediction interval",
        "regression statistics on two numeric columns of a BigQuery table",
    ],
    "data.query": [
        "run read-only SQL against a BigQuery public dataset",
        "query a BigQuery table and return rows",
        "execute a SELECT on BigQuery with a scan limit",
    ],
    "data.question": [
        "answer a question about a BigQuery table in plain language",
        "natural language to SQL against a dataset and explain the result",
        "ask a data question and get the figures with the SQL that produced them",
    ],
    "data.forecast": [
        "forecast a time series stored in BigQuery",
        "predict the next values of a daily metric with TimesFM",
        "time series forecast from a table with a date and value column",
    ],
    "data.anomalies": [
        "detect anomalies in a time series in BigQuery",
        "flag unusual values in a daily metric against its history",
        "anomaly detection on a table with a timestamp column",
    ],
    "monitor.snapshot": [
        "save a baseline of a web page to detect changes later",
        "start monitoring a URL for content changes",
    ],
    "monitor.check": [
        "has this web page changed since the last check",
        "summarize what changed on a monitored page",
        "diff a URL against its saved baseline",
    ],
    "traffic.route": ["how long is the drive from the Ferry Building to Oakland City Hall right now with traffic", "traffic-aware ETA and distance between two addresses leaving at 5pm tomorrow", "current traffic delay on the drive from SFO to downtown San Jose", "walking time and distance from 37.7955,-122.3937 to Union Square, San Francisco", "transit travel time between two places at a given departure time"],
    "video.youtube": ["search YouTube for videos about a topic with view counts", "how many views, likes and comments does this YouTube video have", "latest YouTube videos on a subject uploaded this month", "duration, channel and thumbnail of a YouTube video by id", "most viewed YouTube videos for a query in Japan"],
    "social.x_pulse": [
        "how much is X talking about a topic this week and how engaged is it",
        "post volume per day on X for a keyword",
        "engagement rate, top hashtags and language mix for a topic on Twitter",
        "is a brand trending on X right now, as numbers",
    ],
    "social.bluesky": [
        "latest posts from a Bluesky account with likes and reposts",
        "find Bluesky accounts matching a name",
        "read a Bluesky thread and its replies",
        "Bluesky profile follower count and recent posts",
    ],
    "social.mastodon": [
        "latest Mastodon posts under a hashtag",
        "a Mastodon account's recent statuses and follower count",
        "trending hashtags on mastodon.social right now",
        "search Mastodon for accounts or hashtags",
    ],
    "news.search": [
        "latest news about semiconductors in Japanese from Japanese publishers",
        "what is the Korean press saying about a company today",
        "headlines for a stock ticker in the last 24 hours",
        "current news on a topic in any language and country",
    ],
    "data.macro": [
        "Japan inflation rate, latest official figure and the change from last year",
        "GDP of South Korea in US dollars for the last ten years",
        "euro area unemployment, monthly, from Eurostat",
        "any World Bank indicator for a country as a series with the latest value",
    ],
    "travel.flights": [
        "flights from London to New York next month, cheapest first",
        "non-stop business class offers Tokyo to Seoul on a date",
        "what does it cost to fly two adults and a child from Sydney to Singapore",
        "live airfare with refund and change conditions and offer expiry",
    ],
    "opendata.table": [
        "read this Korean government CSV as JSON rows",
        "give me the rows of an e-Stat statistics file",
        "parse a government XLSX dataset, second sheet",
        "turn a dataset download link into typed JSON records",
    ],
    "opendata.search": [
        "find government datasets about population in Korea or Japan",
        "search UK, EU or Australian open data for rainfall CSV downloads",
        "Korean public data portal datasets on a topic, keyless",
        "government structured data on a topic, in the local language, 16 portals",
    ],
    "market.stock": [
        "live stock price and daily history for a ticker",
        "what is AAPL trading at right now and how has it moved this month",
        "OHLCV bars for a US stock over the last year",
    ],
    "market.fundamentals": [
        "revenue, net income and EPS a company reported to the SEC by quarter",
        "balance sheet items from the latest 10-K and 10-Q filings",
        "SEC XBRL company facts for a ticker",
    ],
    "finance.analytics": [
        "annualized volatility, Sharpe ratio and max drawdown of a price series",
        "value at risk and CVaR of a stock at 95 percent",
        "beta and correlation of a stock against SPY",
        "Black-Scholes price and Greeks for a call option",
        "RSI, moving averages and Bollinger bands at the last price",
    ],
    "commerce.availability": [
        "can I buy this product right now and at what price",
        "is this item in stock in size 10 and does it ship to Japan",
        "live availability, price and options on a product or booking page",
        "is this listing out of stock, on preorder or discontinued",
    ],
    "security.mcp_inspect": [
        "inspect an MCP server for auth and which tools can change state",
        "audit a remote MCP endpoint's tools and annotations",
        "does this MCP server require authentication",
    ],
    "maps.places": [
        "find places matching a query near a location",
        "search for restaurants or shops in a city",
        "look up a business on Google Maps",
    ],
    "maps.route": [
        "driving route and duration between two addresses",
        "how long does it take to walk from A to B",
        "transit directions between two places",
    ],
    "maps.weather": [
        "current weather at a location",
        "what is the temperature in a city right now",
    ],
    "research.brief": [
        "read a web page and write a brief answering a question about it",
        "what does this website offer, who is it for and what does it cost",
        "summarize a URL with the source disclosed",
    ],
    "research.page_facts": [
        "extract specific facts from a web page as JSON fields",
        "get the pricing, contact and product fields from a URL",
        "structured facts about a company from its website",
    ],
    "market.intel": [
        "what do spot and prediction markets together say about bitcoin",
        "market sentiment read combining Coinbase price and Polymarket odds",
        "reconcile a crypto price with prediction market probabilities",
    ],
    "research.web": [
        "research a question on the live web with numbered citations",
        "cited answer from several web sources",
        "find and read sources to answer a research question",
    ],
    "research.company": [
        "research a company from live web sources with citations",
        "what does this company do, what does it sell, anything notable",
        "company due diligence brief with sources",
    ],
    "verify.claims": [
        "fact-check these claims against these source URLs",
        "does this source support or contradict a statement",
        "verify claims with the quote each verdict rests on",
    ],
}
