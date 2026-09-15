"""One row per sellable worker: price, schemas, description, skill, deadline.

MIRRORS _CATALOG, DOES NOT TOUCH IT. The audit catalog in app/main.py is the
source of truth for the five audits and stays exactly as it is. This is the
same idea for the worker network, in its own file, so a new worker can never
change what an audit route charges or advertises.

PRICES ARE PROVISIONAL AND SAY SO. Every row carries `pricing_basis`, and the
ledger records the measured provider cost of every call. The honest sequence
is: publish a provisional price, measure the real cost, then revise. Nothing
here claims a margin -- `scripts/worker-ledger.sh` computes margin only for
calls whose cost was actually measured.

`max_seconds` on every row is bounded by the x402 challenge's
maxTimeoutSeconds (300, verified live 2026-09-15). A job that cannot finish
inside the payment window is not sellable synchronously, so nothing here
exceeds 240s.
"""

from typing import Optional

# The ceiling every worker deadline must respect, from the live 402 challenge.
PAYMENT_WINDOW_SECONDS = 300
MAX_WORKER_SECONDS = 240

_URL = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "http(s) URL to fetch."}},
    "required": ["url"],
    "additionalProperties": False,
}


def _obj(properties: dict, required: list) -> dict:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


class Worker:
    """A sellable unit of completed work."""

    __slots__ = ("name", "path", "price_usd", "tier", "title", "description",
                 "tags", "input_schema", "output_schema", "returns", "skill",
                 "max_seconds", "pricing_basis", "composes", "requires")

    def __init__(self, name, price_usd, tier, title, description, tags,
                 input_schema, output_schema, returns, skill, max_seconds,
                 pricing_basis, composes=(), requires=()):
        self.name = name
        self.path = f"/work/{name.replace('.', '/')}"
        self.price_usd = price_usd
        self.tier = tier
        self.title = title
        self.description = description
        self.tags = list(tags)
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.returns = returns
        self.skill = skill
        self.max_seconds = min(max_seconds, MAX_WORKER_SECONDS)
        self.pricing_basis = pricing_basis
        self.composes = list(composes)
        # Provider modules this worker cannot run without. Drives `available()`
        # below, which decides whether the worker is ADVERTISED at all.
        self.requires = list(requires)

    def available(self) -> bool:
        """Can this deployment actually deliver this worker right now?

        A worker whose provider has no credential here is not a worker, it is a
        promise we cannot keep -- and a paid route that takes money for a
        capability it cannot run is the single worst thing this service could
        do. So availability gates the advertisement (the /work index, the
        manifest) AND the 402 itself: an unavailable worker answers 503 before
        any payment instrument is touched, rather than quoting a price.

        This is the same fail-closed rule the payment rails already follow:
        /.well-known/agent.json lists only rails that can genuinely settle.
        """
        from . import providers

        for module_name in self.requires:
            module = getattr(providers, module_name, None)
            if module is None:  # pragma: no cover - guarded by a catalog test
                return False
            if not any(p.available() for p in module.PROVIDERS):
                return False
        return True

    def unavailable_reason(self) -> str:
        from . import providers

        for module_name in self.requires:
            module = getattr(providers, module_name, None)
            if module is None:  # pragma: no cover
                return f"unknown provider module {module_name}"
            if not any(p.available() for p in module.PROVIDERS):
                return module.PROVIDERS[0].unavailable_reason() if module.PROVIDERS \
                    else f"{module_name} has no providers"
        return ""

    @property
    def tool_name(self) -> str:
        """MCP tool name. `work_` prefixed so it can never collide with the
        existing audit_* tools."""
        return "work_" + self.name.replace(".", "_")


_RESULT = {"type": "object", "description": "Worker result; see `returns`."}

CATALOG = [
    # --- utilities: one live read, priced near cost -------------------------
    Worker(
        name="chain.network", price_usd=0.02, tier="utility",
        title="Base network state",
        description=(
            "Current Base mainnet block height and gas price, read live from a "
            "Base RPC node. Use to check chain liveness or time an on-chain action."),
        tags=["base", "blockchain", "rpc", "gas", "network"],
        input_schema=_obj({}, []),
        output_schema=_RESULT,
        returns="block_number, gas_price_wei, gas_price_gwei.",
        skill="chain.network", max_seconds=30,
        pricing_basis="Provisional. Provider cost is zero (public RPC); price covers infrastructure.",
        requires=("base_rpc",)),
    Worker(
        name="chain.address", price_usd=0.05, tier="utility",
        title="Base address report",
        description=(
            "Everything about one Base mainnet address in a single call: ETH balance, "
            "transaction count, and whether it is a contract or an externally owned "
            "account. Three chain reads folded into one answer."),
        tags=["base", "blockchain", "address", "wallet", "contract"],
        input_schema=_obj(
            {"address": {"type": "string", "description": "0x-prefixed Base address."}},
            ["address"]),
        output_schema=_RESULT,
        returns="balance_wei, balance_eth, transaction_count, is_contract, code_size_bytes.",
        skill="chain.address", max_seconds=45,
        pricing_basis="Provisional. Provider cost zero (public RPC); three reads per call.",
        requires=("base_rpc",)),
    Worker(
        name="chain.transaction", price_usd=0.05, tier="utility",
        title="Base transaction lookup",
        description=(
            "One Base mainnet transaction with its receipt folded in: sender, recipient, "
            "value, block, success or failure, and gas actually used."),
        tags=["base", "blockchain", "transaction", "receipt", "onchain"],
        input_schema=_obj(
            {"hash": {"type": "string", "description": "0x-prefixed transaction hash."}},
            ["hash"]),
        output_schema=_RESULT,
        returns="from, to, value_eth, block_number, status, gas_used, log_count.",
        skill="chain.transaction", max_seconds=45,
        pricing_basis="Provisional. Provider cost zero (public RPC).",
        requires=("base_rpc",)),
    Worker(
        name="market.quote", price_usd=0.02, tier="utility",
        title="Crypto spot quote",
        description=(
            "Live spot price and 24-hour change for any Coinbase product (BTC-USD, "
            "ETH-USD and the rest), from Coinbase's public market data."),
        tags=["market", "price", "crypto", "coinbase", "quote"],
        input_schema=_obj(
            {"product_id": {"type": "string", "description": "e.g. BTC-USD."}},
            ["product_id"]),
        output_schema=_RESULT,
        returns="price, price_change_24h_pct, volume_24h, base/quote currency.",
        skill="market.quote", max_seconds=30,
        pricing_basis="Provisional. Provider cost zero (public endpoint).",
        requires=("coinbase_market",)),
    Worker(
        name="market.prediction", price_usd=0.05, tier="utility",
        title="Prediction market probabilities",
        description=(
            "Live prediction markets and the probabilities they currently imply, from "
            "Polymarket. Search by topic or take the highest-volume markets. Returns "
            "each outcome as a percentage, not a raw price."),
        tags=["prediction", "forecast", "probability", "polymarket", "odds"],
        input_schema=_obj({
            "query": {"type": "string", "description": "Topic to match (optional)."},
            "limit": {"type": "integer", "description": "1-50, default 10."},
        }, []),
        output_schema=_RESULT,
        returns="markets[] with question, implied_probabilities[], volume, end_date.",
        skill="market.prediction", max_seconds=30,
        pricing_basis="Provisional. Provider cost zero (public API).",
        requires=("polymarket",)),
    Worker(
        name="extract.page", price_usd=0.10, tier="utility",
        title="Web page extraction",
        description=(
            "Fetch any web page and return its readable text, title, description and "
            "links, rendered in a real browser so JavaScript-built pages extract "
            "correctly. Falls back to direct fetch when rendering is unavailable."),
        tags=["extract", "scrape", "web", "content", "browser"],
        input_schema=_URL, output_schema=_RESULT,
        returns="title, description, text, text_chars, links[], javascript_rendered.",
        skill="extract.page", max_seconds=90,
        pricing_basis="Provisional. Runs on our own flat-rate browser; marginal provider cost zero.",
        requires=("web",)),

    # --- inference ----------------------------------------------------------
    Worker(
        name="llm.analyze", price_usd=0.25, tier="standard",
        title="Analyse text",
        description=(
            "Answer a specific question about text you supply, using Gemini. Answers "
            "only from the material given and says so explicitly when the material "
            "does not contain the answer, rather than guessing."),
        tags=["llm", "analysis", "gemini", "summarize", "reasoning"],
        input_schema=_obj({
            "text": {"type": "string", "description": "Material to analyse."},
            "question": {"type": "string", "description": "What to answer. Optional."},
        }, ["text"]),
        output_schema=_RESULT,
        returns="answer, model, tokens{prompt,output}.",
        skill="llm.analyze", max_seconds=150,
        pricing_basis="Provisional. Token usage is measured per call; set GEMINI_PRICE_PER_MTOK_IN/_OUT to measure cost.",
        requires=("gemini",)),
    Worker(
        name="llm.extract", price_usd=0.25, tier="standard",
        title="Extract fields as JSON",
        description=(
            "Pull named fields out of text and return them as a JSON object with "
            "exactly the keys you asked for. Fields the text does not state come back "
            "as null instead of being invented."),
        tags=["extract", "structured", "json", "fields", "parsing"],
        input_schema=_obj({
            "text": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "Field names to extract."},
        }, ["text", "fields"]),
        output_schema=_RESULT,
        returns="fields{} with one key per requested field.",
        skill="llm.extract", max_seconds=150,
        pricing_basis="Provisional. Token usage measured per call.",
        requires=("gemini",)),

    # --- data ---------------------------------------------------------------
    Worker(
        name="data.query", price_usd=0.50, tier="standard",
        title="Run a BigQuery query",
        description=(
            "Execute a read-only BigQuery SQL query, including against Google's public "
            "datasets, and get the rows back as JSON. Every query is dry-run first and "
            "refused if it would scan more than the byte ceiling, so cost is bounded "
            "before anything runs."),
        tags=["bigquery", "sql", "data", "query", "analytics"],
        input_schema=_obj({
            "sql": {"type": "string", "description": "Read-only SELECT or WITH query."},
            "max_scan_gib": {"type": "number", "description": "Optional scan ceiling."},
        }, ["sql"]),
        output_schema=_RESULT,
        returns="columns[], rows[], row_count, gib_processed, cache_hit.",
        skill="data.query", max_seconds=180,
        pricing_basis="Provisional. Bytes scanned measured per call; set BQ_PRICE_PER_TIB to measure cost.",
        requires=("bigquery",)),
    Worker(
        name="data.question", price_usd=5.00, tier="advanced",
        title="Answer a question from a dataset",
        description=(
            "Ask a question in plain language about any BigQuery table, including "
            "Google's public datasets, and get a written answer with the SQL and the "
            "rows it came from. Writes the query, runs it under a byte ceiling, and "
            "reads the result back as an analysis."),
        tags=["bigquery", "analysis", "data", "question", "sql"],
        input_schema=_obj({
            "question": {"type": "string"},
            "table": {"type": "string",
                      "description": "Fully qualified: project.dataset.table."},
            "columns": {"type": "array", "items": {"type": "string"},
                        "description": "Optional column hints."},
        }, ["question", "table"]),
        output_schema=_RESULT,
        returns="answer, sql, columns[], rows[], gib_processed.",
        skill="data.question", max_seconds=240,
        pricing_basis="Provisional, completed-work tier. Three provider calls; usage measured per call.",
        composes=["llm.analyze", "data.query"],
        requires=("gemini", "bigquery")),

    # --- composites: completed work -----------------------------------------
    Worker(
        name="research.brief", price_usd=5.00, tier="advanced",
        title="Research brief on a URL",
        description=(
            "Read any web page and get a written answer to your question about it. "
            "Renders the page in a real browser, extracts the content, and analyses it "
            "in one paid call. Answers only from the page, and says when the page does "
            "not contain the answer."),
        tags=["research", "brief", "web", "analysis", "competitive"],
        input_schema=_obj({
            "url": {"type": "string"},
            "question": {"type": "string", "description": "Optional; defaults to an overview."},
        }, ["url"]),
        output_schema=_RESULT,
        returns="brief, title, final_url, source{}, model.",
        skill="research.brief", max_seconds=200,
        pricing_basis="Provisional, completed-work tier. Two provider calls; usage measured per call.",
        composes=["extract.page", "llm.analyze"],
        requires=("web", "gemini")),
    Worker(
        name="research.page_facts", price_usd=5.00, tier="advanced",
        title="Structured facts from a URL",
        description=(
            "Read any web page and return exactly the fields you name as JSON -- "
            "pricing, contact, product names, whatever you ask for. Fields the page "
            "does not state come back null rather than invented. Built for pipelines "
            "that need a fixed shape."),
        tags=["extract", "structured", "web", "facts", "enrichment"],
        input_schema=_obj({
            "url": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "string"}},
        }, ["url", "fields"]),
        output_schema=_RESULT,
        returns="fields{} with one key per requested field, plus title and final_url.",
        skill="research.page_facts", max_seconds=200,
        pricing_basis="Provisional, completed-work tier. Two provider calls; usage measured per call.",
        composes=["extract.page", "llm.extract"],
        requires=("web", "gemini")),
    Worker(
        name="market.intel", price_usd=5.00, tier="advanced",
        title="Market intelligence read",
        description=(
            "Combine a live crypto spot quote with live prediction-market "
            "probabilities on the same subject and get them analysed together in one "
            "call. Market data and implied probabilities only -- not investment advice."),
        tags=["market", "intelligence", "prediction", "crypto", "sentiment"],
        input_schema=_obj({
            "product_id": {"type": "string", "description": "e.g. BTC-USD. Default BTC-USD."},
            "query": {"type": "string", "description": "Prediction-market topic. Optional."},
            "question": {"type": "string", "description": "Optional analysis question."},
            "limit": {"type": "integer"},
        }, []),
        output_schema=_RESULT,
        returns="spot{}, prediction_markets[], analysis, disclaimer.",
        skill="market.intel", max_seconds=200,
        pricing_basis="Provisional, completed-work tier. Three provider calls; usage measured per call.",
        composes=["market.quote", "market.prediction", "llm.analyze"],
        requires=("coinbase_market", "polymarket", "gemini")),
]

BY_PATH = {worker.path: worker for worker in CATALOG}
BY_NAME = {worker.name: worker for worker in CATALOG}
BY_TOOL = {worker.tool_name: worker for worker in CATALOG}


def get(path: str) -> Optional[Worker]:
    return BY_PATH.get(path)


def price_of(path: str) -> Optional[float]:
    """The price, but only for a worker this deployment can actually deliver.

    Returning None for an unavailable worker is what stops the node quoting a
    402 for a capability it cannot run. The caller gets 503 from the route
    instead, which is the truthful answer and costs them nothing.
    """
    worker = BY_PATH.get(path)
    if worker is None or not worker.available():
        return None
    return worker.price_usd


def description_of(path: str) -> Optional[str]:
    worker = BY_PATH.get(path)
    return worker.description if worker else None


def live() -> list:
    """Only the workers this deployment can deliver. What gets advertised."""
    return [worker for worker in CATALOG if worker.available()]
