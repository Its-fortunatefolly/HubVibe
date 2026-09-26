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

try:
    from . import contract
except ImportError:
    # Loaded by file path rather than as part of the workers package (the
    # seed script's tests do this): load the sibling the same way.
    import importlib.util as _ilu
    from pathlib import Path as _Path

    _spec = _ilu.spec_from_file_location(
        "wcag_audit_engine_workers_contract", _Path(__file__).resolve().parent / "contract.py")
    contract = _ilu.module_from_spec(_spec)  # type: ignore
    _spec.loader.exec_module(contract)

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


_POINT = {"oneOf": [
    {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
     "description": "[x, y]"},
    {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
     "required": ["x", "y"], "additionalProperties": False},
]}
_STATS_METRICS = ["linear_regression", "normal_distribution", "p_values", "prediction"]
_STATS_INPUT = dict(_obj({
    "points": {"type": "array", "items": _POINT, "minItems": 2, "maxItems": 100000,
               "description": ("The data: [x, y] pairs (or {x, y} objects), 2 to 100000 "
                               "of them; at least 3 for a regression. Use this OR `table`.")},
    "table": {"type": "string",
              "description": ("BigQuery table to read instead of `points`: "
                              "project.dataset.table, readable by the node's service "
                              "account (public datasets are). Needs x_column and y_column.")},
    "x_column": {"type": "string", "description": "Numeric column for x, with `table`."},
    "y_column": {"type": "string", "description": "Numeric column for y, with `table`."},
    "max_rows": {"type": "integer", "minimum": 3, "maximum": 100000,
                 "description": ("Rows to read from `table`, default 10000. A larger table "
                                 "is reduced to this many rows by FARM_FINGERPRINT order, "
                                 "so the same table always yields the same rows.")},
    "metrics": {"type": "array", "items": {"type": "string", "enum": _STATS_METRICS},
                "minItems": 1, "uniqueItems": True,
                "description": ("Which results to compute. Default: linear_regression, "
                                "normal_distribution and p_values, plus prediction when "
                                "predict_x is given.")},
    "predict_x": {"type": "array", "items": {"type": "number"}, "minItems": 1, "maxItems": 100,
                  "description": "x values to predict y at, with mean and prediction intervals."},
    "alpha": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1,
              "description": "Significance level for p-value validation and intervals. Default 0.05."},
    "distribution_of": {"type": "string", "enum": ["y", "x", "residuals"],
                        "description": "Which values the normal model fits. Default y."},
    "probability_queries": {
        "type": "array", "maxItems": 50,
        "items": {"type": "object", "minProperties": 1, "maxProperties": 1,
                  "properties": {"below": {"type": "number"}, "above": {"type": "number"},
                                 "between": {"type": "array", "items": {"type": "number"},
                                             "minItems": 2, "maxItems": 2}},
                  "additionalProperties": False},
        "description": ("Probabilities to read off the fitted normal model: "
                        "{\"below\": v}, {\"above\": v} or {\"between\": [a, b]}.")},
}, []), oneOf=[{"required": ["points"]}, {"required": ["table", "x_column", "y_column"]}])


class Worker:
    """A sellable unit of completed work."""

    __slots__ = ("name", "path", "price_usd", "tier", "title", "description",
                 "tags", "input_schema", "output_schema", "returns", "skill",
                 "max_seconds", "pricing_basis", "composes", "requires")

    def __init__(self, name, price_usd, tier, title, description, tags,
                 input_schema, returns, skill, max_seconds,
                 pricing_basis, composes=(), requires=(), output_schema=None):
        self.name = name
        self.path = f"/work/{name.replace('.', '/')}"
        self.price_usd = price_usd
        self.tier = tier
        self.title = title
        self.description = description
        self.tags = list(tags)
        self.input_schema = input_schema
        # The JSON Schema of `result` in this worker's 200 body, from
        # workers/contract.py -- written from the skill's literal return
        # value, and the object openapi.json, agent.json and the Bazaar record
        # all publish. A worker with no entry there cannot be constructed,
        # which is the guard: a route is never advertised with a placeholder.
        self.output_schema = (output_schema if output_schema is not None
                              else contract.OUTPUT_SCHEMAS[name])
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


CATALOG = [
    # --- utilities: one live read, priced near cost -------------------------
    Worker(
        name="chain.network", price_usd=0.02, tier="utility",
        title="Base network state",
        description=(
            'Base chain status: current block height and gas price (wei and gwei), '
            'read live from a Base mainnet RPC node. No input needed. Use it to check '
            'the chain is live, time an on-chain action, or estimate gas before a '
            'chain.rpc call.'),
        tags=["base", "blockchain", "rpc", "gas", "network"],
        input_schema=_obj({}, []),
        returns="block_number, gas_price_wei, gas_price_gwei.",
        skill="chain.network", max_seconds=30,
        pricing_basis="Provisional. Provider cost is zero (public RPC); price covers infrastructure.",
        requires=("base_rpc",)),
    Worker(
        name="chain.address", price_usd=0.05, tier="utility",
        title="Base address report",
        description=(
            'Base wallet lookup: ETH balance, transaction count (nonce) and whether '
            'the address is a contract or an ordinary wallet, in one call for one '
            'Base mainnet address. Input: address. Three RPC reads folded into one '
            'answer; for a token balance or any other read, use chain.rpc.'),
        tags=["base", "blockchain", "address", "wallet", "contract"],
        input_schema=_obj(
            {"address": {"type": "string", "description": "0x-prefixed Base address."}},
            ["address"]),
        returns="balance_wei, balance_eth, transaction_count, is_contract, code_size_bytes.",
        skill="chain.address", max_seconds=45,
        pricing_basis="Provisional. Provider cost zero (public RPC); three reads per call.",
        requires=("base_rpc",)),
    Worker(
        name="chain.transaction", price_usd=0.05, tier="utility",
        title="Base transaction lookup",
        description=(
            'Base transaction lookup by hash: sender, recipient, ETH value, block, '
            'success or failure, gas used and log count, with the receipt folded in. '
            'Input: the 0x transaction hash. Use chain.rpc for the raw '
            'eth_getTransactionReceipt output.'),
        tags=["base", "blockchain", "transaction", "receipt", "onchain"],
        input_schema=_obj(
            {"hash": {"type": "string", "description": "0x-prefixed transaction hash."}},
            ["hash"]),
        returns="from, to, value_eth, block_number, status, gas_used, log_count.",
        skill="chain.transaction", max_seconds=45,
        pricing_basis="Provisional. Provider cost zero (public RPC).",
        requires=("base_rpc",)),
    Worker(
        name="market.quote", price_usd=0.02, tier="utility",
        title="Crypto spot quote",
        description=(
            'Crypto price: live spot price, 24-hour change and 24-hour volume for any '
            "Coinbase product such as BTC-USD or ETH-USD, from Coinbase's public "
            'market data. Input: product_id. Use market.ticker for bid and ask from a '
            'second source, market.rates for a whole exchange-rate table.'),
        tags=["market", "price", "crypto", "coinbase", "quote"],
        input_schema=_obj(
            {"product_id": {"type": "string", "description": "e.g. BTC-USD."}},
            ["product_id"]),
        returns="price, price_change_24h_pct, volume_24h, base/quote currency.",
        skill="market.quote", max_seconds=30,
        pricing_basis="Provisional. Provider cost zero (public endpoint).",
        requires=("coinbase_market",)),
    Worker(
        name="market.prediction", price_usd=0.05, tier="utility",
        title="Prediction market probabilities",
        description=(
            'Prediction market odds from Polymarket: live markets and the probability '
            'each outcome currently implies, as percentages rather than raw prices. '
            'Search by topic or take the highest-volume markets. Input: optional '
            'query and limit. Use prediction.market for one market by slug, '
            'prediction.events for event groupings.'),
        tags=["prediction", "forecast", "probability", "polymarket", "odds"],
        input_schema=_obj({
            "query": {"type": "string", "description": "Topic to match (optional)."},
            "limit": {"type": "integer", "description": "1-50, default 10."},
        }, []),
        returns="markets[] with question, implied_probabilities[], volume, end_date.",
        skill="market.prediction", max_seconds=30,
        pricing_basis="Provisional. Provider cost zero (public API).",
        requires=("polymarket",)),
    Worker(
        name="extract.page", price_usd=0.10, tier="utility",
        title="Web page extraction",
        description=(
            'Web page to clean text: fetch any URL in a real browser '
            '(JavaScript-rendered pages included) and return the readable text, '
            'title, description and links. Input: url. Use fetch.raw when you need '
            'the raw HTTP status, headers and body; research.brief when you want a '
            'question answered from the page.'),
        tags=["extract", "scrape", "web", "content", "browser"],
        input_schema=_URL,
        returns="title, description, text, text_chars, links[], javascript_rendered.",
        skill="extract.page", max_seconds=90,
        pricing_basis="Provisional. Runs on our own flat-rate browser; marginal provider cost zero.",
        requires=("web",)),

    # --- inference ----------------------------------------------------------
    Worker(
        name="llm.analyze", price_usd=0.25, tier="standard",
        title="Analyse text",
        description=(
            'Answer a question from text you supply, using Gemini. Grounded in the '
            'material only: if the text does not contain the answer it says so '
            'instead of guessing. Input: text and question. Use llm.extract for fixed '
            'JSON fields, llm.generate for a free-form completion.'),
        tags=["llm", "analysis", "gemini", "summarize", "reasoning"],
        input_schema=_obj({
            "text": {"type": "string", "description": "Material to analyse."},
            "question": {"type": "string", "description": "What to answer. Optional."},
        }, ["text"]),
        returns="answer, model, tokens{prompt,output}.",
        skill="llm.analyze", max_seconds=150,
        pricing_basis="Provisional. Token usage is measured per call; set GEMINI_PRICE_PER_MTOK_IN/_OUT to measure cost.",
        requires=("gemini",)),
    Worker(
        name="llm.extract", price_usd=0.25, tier="standard",
        title="Extract fields as JSON",
        description=(
            'Structured data extraction: pull the fields you name out of text and get '
            'a JSON object with exactly those keys, null for anything the text does '
            'not state. Input: text and a list of field names. For extraction from a '
            'live web page use research.page_facts.'),
        tags=["extract", "structured", "json", "fields", "parsing"],
        input_schema=_obj({
            "text": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "Field names to extract."},
        }, ["text", "fields"]),
        returns="fields{} with one key per requested field.",
        skill="llm.extract", max_seconds=150,
        pricing_basis="Provisional. Token usage measured per call.",
        requires=("gemini",)),

    # --- data ---------------------------------------------------------------
    Worker(
        name="data.query", price_usd=0.50, tier="standard",
        title="Run a BigQuery query",
        description=(
            "BigQuery SQL: run a read-only query, including against Google's public "
            'datasets, and get columns and rows back as JSON with the bytes '
            'processed. Every query is dry-run first and refused above the scan '
            'ceiling, so cost is bounded before it runs. Input: sql, optional '
            'max_scan_gib. Use data.question to ask in plain language instead.'),
        tags=["bigquery", "sql", "data", "query", "analytics"],
        input_schema=_obj({
            "sql": {"type": "string", "description": "Read-only SELECT or WITH query."},
            "max_scan_gib": {"type": "number", "description": "Optional scan ceiling."},
        }, ["sql"]),
        returns="columns[], rows[], row_count, gib_processed, cache_hit.",
        skill="data.query", max_seconds=180,
        pricing_basis="Provisional. Bytes scanned measured per call; set BQ_PRICE_PER_TIB to measure cost.",
        requires=("bigquery",)),
    Worker(
        name="data.question", price_usd=5.00, tier="advanced",
        title="Answer a question from a dataset",
        description=(
            'Plain-language question answered over any BigQuery table, including '
            "Google's public datasets: the SQL is written for you, run under a byte "
            'ceiling, and the rows read back into a written answer. Input: question '
            'and table, optional column list. Returns answer, sql, columns and rows. '
            'Use data.query when you already have the SQL.'),
        tags=["bigquery", "analysis", "data", "question", "sql"],
        input_schema=_obj({
            "question": {"type": "string"},
            "table": {"type": "string",
                      "description": "Fully qualified: project.dataset.table."},
            "columns": {"type": "array", "items": {"type": "string"},
                        "description": "Optional column hints."},
        }, ["question", "table"]),
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
            'Answer a question about one web page: the URL is rendered in a real '
            'browser, its content extracted and analysed, and a written brief '
            'returned that is grounded in the page only and says when the page does '
            'not answer. Input: url, optional question. Use research.page_facts for '
            'fixed JSON fields, research.web when the answer needs a web search.'),
        tags=["research", "brief", "web", "analysis", "competitive"],
        input_schema=_obj({
            "url": {"type": "string"},
            "question": {"type": "string", "description": "Optional; defaults to an overview."},
        }, ["url"]),
        returns="brief, title, final_url, source{}, model.",
        skill="research.brief", max_seconds=200,
        pricing_basis="Provisional, completed-work tier. Two provider calls; usage measured per call.",
        composes=["extract.page", "llm.analyze"],
        requires=("web", "gemini")),
    Worker(
        name="research.page_facts", price_usd=5.00, tier="advanced",
        title="Structured facts from a URL",
        description=(
            'Structured facts from a web page: name the fields you need (pricing, '
            'contact, product names, anything) and get exactly those keys back as '
            'JSON from the rendered page, null where the page does not say. Input: '
            'url and fields. Built for pipelines that need a fixed shape; use '
            'research.brief for a written answer.'),
        tags=["extract", "structured", "web", "facts", "enrichment"],
        input_schema=_obj({
            "url": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "string"}},
        }, ["url", "fields"]),
        returns="fields{} with one key per requested field, plus title and final_url.",
        skill="research.page_facts", max_seconds=200,
        pricing_basis="Provisional, completed-work tier. Two provider calls; usage measured per call.",
        composes=["extract.page", "llm.extract"],
        requires=("web", "gemini")),
    Worker(
        name="market.intel", price_usd=5.00, tier="advanced",
        title="Market intelligence read",
        description=(
            'Crypto market read in one call: a live Coinbase spot quote and live '
            'Polymarket probabilities on the same subject, analysed together in a '
            'written summary. Input: optional product_id, query, question and limit. '
            'Market data and implied probabilities only, not investment advice.'),
        tags=["market", "intelligence", "prediction", "crypto", "sentiment"],
        input_schema=_obj({
            "product_id": {"type": "string", "description": "e.g. BTC-USD. Default BTC-USD."},
            "query": {"type": "string", "description": "Prediction-market topic. Optional."},
            "question": {"type": "string", "description": "Optional analysis question."},
            "limit": {"type": "integer"},
        }, []),
        returns="spot{}, prediction_markets[], analysis, disclaimer.",
        skill="market.intel", max_seconds=200,
        pricing_basis="Provisional, completed-work tier. Three provider calls; usage measured per call.",
        composes=["market.quote", "market.prediction", "llm.analyze"],
        requires=("coinbase_market", "polymarket", "gemini")),
    # --- wave 1: bees on live Google credentials -----------------------------
    Worker(
        name="search.web", price_usd=0.10, tier="utility",
        title="Web search",
        description=(
            'Web search for agents: a live Google Search query answered from current '
            'results, returning a grounded answer plus the source URLs and titles it '
            "used, never the model's own memory. Input: query. Use research.web when "
            'you need the sources read in full and a cited brief.'),
        tags=["search", "web", "google", "grounding", "current"],
        input_schema=_obj({"query": {"type": "string"}}, ["query"]),
        returns="query, answer, sources[{url,title}], search_queries_used[], model.",
        skill="search.web", max_seconds=60,
        pricing_basis="Provisional. Token usage measured; Google's own search-grounding surcharge is not yet measured here.",
        requires=("search_grounding",)),
    Worker(
        name="llm.generate", price_usd=0.25, tier="standard",
        title="Raw text completion",
        description=(
            'LLM text generation: a raw completion from your prompt, with optional '
            'system message, max_tokens and temperature. Gemini by default; optional '
            'provider and model fields select another configured model. Input: '
            "prompt. Nothing is prescribed about the answer's shape; use llm.analyze "
            'for an answer grounded in text you supply.'),
        tags=["llm", "generate", "completion", "gemini", "claude", "inference"],
        input_schema=_obj({
            "prompt": {"type": "string", "description": "The prompt."},
            "system": {"type": "string", "description": "Optional system message."},
            "max_tokens": {"type": "integer", "description": "1-4096, default 1024."},
            "temperature": {"type": "number", "description": "0-2, default 0.7."},
            "provider": {"type": "string", "description": "Optional: gemini or anthropic."},
            "model": {"type": "string", "description": "Optional: a specific model from that provider."},
        }, ["prompt"]),
        returns="text, model, provider, finish_reason, usage{input_tokens,output_tokens}.",
        skill="llm.generate", max_seconds=120,
        pricing_basis="Provisional. Token usage measured per call.",
        requires=("completion",)),
    Worker(
        name="code.execute", price_usd=0.25, tier="standard",
        title="Execute Python",
        description=(
            "Run Python code in Google's hosted sandbox, not on this service's own "
            'machines, and get the code, its output and the outcome back. Input: '
            'code. For questions over BigQuery data use data.query instead.'),
        tags=["code", "execute", "sandbox", "python", "compute"],
        input_schema=_obj({"code": {"type": "string"}}, ["code"]),
        returns="code, output, outcome, summary, model.",
        skill="code.execute", max_seconds=90,
        pricing_basis="Provisional. Token usage measured per call.",
        requires=("code_exec",)),
    Worker(
        name="image.generate", price_usd=0.50, tier="standard",
        title="Generate an image",
        description=(
            'Image generation: one image from a text prompt with Imagen 4, returned '
            'as base64-encoded image bytes with its MIME type. Input: prompt, '
            'optional aspect_ratio.'),
        tags=["image", "generate", "imagen", "media", "visual"],
        input_schema=_obj({
            "prompt": {"type": "string"},
            "aspect_ratio": {"type": "string",
                             "description": "1:1, 3:4, 4:3, 16:9 or 9:16. Default 1:1."},
        }, ["prompt"]),
        returns="prompt, aspect_ratio, image_base64, mime_type, model.",
        skill="image.generate", max_seconds=100,
        pricing_basis="Provisional. Flat per-image rate; one attempt only (a retry would re-bill the vendor).",
        requires=("imagen",)),
    Worker(
        name="speech.synthesize", price_usd=0.25, tier="standard",
        title="Text to speech",
        description=(
            'Text to speech: convert text to spoken MP3 audio with Google Cloud '
            'Text-to-Speech, returned as base64. Input: text, optional voice name.'),
        tags=["speech", "tts", "voice", "audio", "media"],
        input_schema=_obj({
            "text": {"type": "string"},
            "voice": {"type": "string", "description": "e.g. en-US-Standard-C. Optional."},
        }, ["text"]),
        returns="text_chars, voice, audio_base64, mime_type.",
        skill="speech.synthesize", max_seconds=60,
        pricing_basis="Provisional. Priced per character by voice tier; one attempt only.",
        requires=("tts",)),
    Worker(
        name="speech.transcribe", price_usd=0.25, tier="standard",
        title="Speech to text",
        description=(
            'Speech to text: transcribe up to 60 seconds or 10 MB of audio with '
            'Google Cloud Speech-to-Text and get the transcript with its confidence '
            'and language. Input: audio_base64, optional language_code. Longer audio '
            'is refused before payment.'),
        tags=["speech", "stt", "transcribe", "audio", "media"],
        input_schema=_obj({
            "audio_base64": {"type": "string",
                             "description": "Base64-encoded audio, any common format."},
            "language_code": {"type": "string", "description": "BCP-47, default en-US."},
        }, ["audio_base64"]),
        returns="transcript, language_code, confidence, model.",
        skill="speech.transcribe", max_seconds=75,
        pricing_basis="Provisional. Cost per minute not yet measured (duration is not reported by the sync API).",
        requires=("stt",)),
    Worker(
        name="data.forecast", price_usd=10.00, tier="premium",
        title="Forecast a time series",
        description=(
            "Time-series forecast from a BigQuery table using Google's pretrained "
            'TimesFM model (AI.FORECAST), no model to train: point it at the table, '
            'timestamp column and value column and get forecast rows for the horizon '
            'you set. Input: table, timestamp_col, data_col, optional horizon and '
            'id_cols.'),
        tags=["bigquery", "forecast", "timeseries", "timesfm", "data"],
        input_schema=_obj({
            "table": {"type": "string", "description": "project.dataset.table"},
            "timestamp_col": {"type": "string"},
            "data_col": {"type": "string"},
            "horizon": {"type": "integer", "description": "Periods to forecast, default 10."},
            "id_cols": {"type": "array", "items": {"type": "string"},
                       "description": "Optional: forecast multiple series at once."},
            "max_scan_gib": {"type": "number"},
        }, ["table", "timestamp_col", "data_col"]),
        returns="table, timestamp_col, data_col, horizon, columns[], rows[], row_count, gib_processed.",
        skill="data.forecast", max_seconds=200,
        pricing_basis="Provisional, completed-work tier. Bytes scanned measured per call.",
        requires=("bigquery",)),
    Worker(
        name="data.anomalies", price_usd=10.00, tier="premium",
        title="Detect anomalies in a time series",
        description=(
            'Anomaly detection over a BigQuery time series with AI.DETECT_ANOMALIES '
            '(TimesFM): score a target table against a history table that shares its '
            'timestamp and value columns and get the anomalous rows with '
            'probabilities. Input: history_table, target_table, timestamp_col, '
            'data_col, optional threshold and id_cols.'),
        tags=["bigquery", "anomaly", "timeseries", "timesfm", "data"],
        input_schema=_obj({
            "history_table": {"type": "string", "description": "project.dataset.table"},
            "target_table": {"type": "string", "description": "project.dataset.table"},
            "timestamp_col": {"type": "string"},
            "data_col": {"type": "string"},
            "anomaly_prob_threshold": {"type": "number",
                                       "description": "0.5-0.999, default 0.95."},
            "id_cols": {"type": "array", "items": {"type": "string"},
                       "description": "Optional: one series per value of these columns."},
            "max_scan_gib": {"type": "number"},
        }, ["history_table", "target_table", "timestamp_col", "data_col"]),
        returns="history_table, target_table, timestamp_col, data_col, anomaly_prob_threshold, columns[], rows[], row_count, gib_processed.",
        skill="data.anomalies", max_seconds=200,
        pricing_basis="Provisional, completed-work tier. Bytes scanned measured per call.",
        requires=("bigquery",)),
    Worker(
        name="stats.probability", price_usd=0.50, tier="standard",
        title="Predictive probability engine: regression, normal model, p-values",
        description=(
            "Deterministic statistics over (x, y) points: ordinary least squares "
            "linear regression with standard errors and confidence intervals, a "
            "fitted normal distribution with quantiles and probability queries, "
            "exact Student t and Jarque-Bera p-values validated at your alpha, and "
            "predictions with prediction intervals. Points come inline or from a "
            "BigQuery table (two numeric columns; above max_rows the rows are "
            "chosen by fingerprint, never at random). Pure arithmetic with exactly "
            "rounded sums: the same input always returns the same numbers, and no "
            "LLM is anywhere in the path."),
        tags=["statistics", "regression", "probability", "p-value",
              "normal-distribution", "prediction", "bigquery", "deterministic"],
        input_schema=_STATS_INPUT,
        returns=("source{}, n, alpha, confidence_level, metrics[], linear_regression{}, "
                 "normal_distribution{}, p_values{}, prediction[], notes[], method."),
        skill="stats.probability", max_seconds=200,
        pricing_basis=("Provisional, standard tier. Inline points cost nothing to serve; a "
                       "table read is metered by bytes scanned under the 20 GiB ceiling."),
        requires=()),
    Worker(
        name="verify.claims", price_usd=5.00, tier="advanced",
        title="Verify claims against sources",
        description=(
            'Fact check: up to 10 claims checked against up to 4 source URLs you '
            'name; each claim comes back SUPPORTED, CONTRADICTED or UNSUPPORTED with '
            'the quote the verdict rests on. Input: claims and sources. Use '
            'research.web when you have a question but no sources yet.'),
        tags=["verify", "fact-check", "claims", "sources", "research"],
        input_schema=_obj({
            "claims": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "string"}},
        }, ["claims", "sources"]),
        returns="claims[], verdicts[{claim,verdict,quote,source_n}], sources_read[], sources_unread[], model.",
        skill="verify.claims", max_seconds=200,
        pricing_basis="Provisional, completed-work tier. Up to five provider calls; usage measured per call.",
        composes=["extract.page"],
        requires=("web", "gemini")),
    Worker(
        name="research.web", price_usd=5.00, tier="advanced",
        title="Research brief from live web search",
        description=(
            'Cited web research: search the live web for your question, read the top '
            'sources in full, and get a written answer with every claim tied to a '
            'numbered source. Input: question, optional max_sources. Use search.web '
            'for a quick grounded answer with links only.'),
        tags=["research", "search", "web", "citations", "brief"],
        input_schema=_obj({
            "question": {"type": "string"},
            "max_sources": {"type": "integer", "description": "1-4, default 3."},
        }, ["question"]),
        returns="question, answer, sources[{n,url,title}], partial[], model.",
        skill="research.web", max_seconds=220,
        pricing_basis="Provisional, completed-work tier. Up to five provider calls; usage measured per call.",
        composes=["search.web", "extract.page", "llm.analyze"],
        requires=("search_grounding", "web", "gemini")),
    Worker(
        name="research.company", price_usd=10.00, tier="premium",
        title="Research and verify a company",
        description=(
            'Company research brief from live web sources: what the company does, its '
            'products and anything notable, every claim cited to a numbered source, '
            'with thin or conflicting evidence disclosed rather than papered over. '
            'Input: company name, optional max_sources.'),
        tags=["research", "company", "kyb", "business-intelligence", "verification"],
        input_schema=_obj({
            "company": {"type": "string"},
            "max_sources": {"type": "integer", "description": "1-4, default 4."},
        }, ["company"]),
        returns="company, report, sources[{n,url,title}], partial[], model.",
        skill="research.company", max_seconds=220,
        pricing_basis="Provisional, completed-work tier. Up to six provider calls; usage measured per call.",
        composes=["search.web", "extract.page", "llm.analyze"],
        requires=("search_grounding", "web", "gemini")),
    Worker(
        name="monitor.snapshot", price_usd=0.50, tier="standard",
        title="Save a monitoring baseline",
        description=(
            'Website change monitoring, step one: fetch a page and store it as the '
            'baseline. Input: url. Call monitor.check later to learn whether and how '
            'it changed.'),
        tags=["monitor", "baseline", "change-detection", "web"],
        input_schema=_URL,
        returns="url, title, text_chars, content_hash, note.",
        skill="monitor.snapshot", max_seconds=90,
        pricing_basis="Provisional. Runs on our own flat-rate browser; marginal provider cost near zero.",
        requires=("web",)),
    Worker(
        name="monitor.check", price_usd=0.50, tier="standard",
        title="Check a page against its baseline",
        description=(
            'Website change monitoring, step two: re-fetch a page saved with '
            'monitor.snapshot and get whether it changed, how old the baseline is, '
            'and a written summary of the differences. Input: url.'),
        tags=["monitor", "change-detection", "diff", "web"],
        input_schema=_URL,
        returns="url, title, changed, baseline_age_seconds, change_summary, model.",
        skill="monitor.check", max_seconds=150,
        pricing_basis="Provisional. Browser fetch plus one inference call when something changed; usage measured.",
        requires=("web", "gemini")),
    Worker(
        name="security.mcp_inspect", price_usd=5.00, tier="advanced",
        title="Inspect an MCP endpoint",
        description=(
            "MCP server security check: probe any MCP endpoint's initialize handshake "
            'and tools/list and report whether it requires authentication, which '
            'protocol version it speaks, how many tools it exposes and which are not '
            'marked read-only. Input: url of the MCP endpoint.'),
        tags=["security", "mcp", "audit", "inspect", "tools"],
        input_schema=_URL,
        returns="reachable, requires_auth, protocol_version, tool_count, tools[], tools_without_readonly_annotation[].",
        skill="security.mcp_inspect", max_seconds=45,
        pricing_basis="Provisional, completed-work tier. Two lightweight requests to the target; provider cost zero.",
        requires=("mcp_probe",)),
    # --- wave 2a: keyless bees ported from the (now-removed) /svc catalog --
    Worker(
        name="fetch.raw", price_usd=0.10, tier="utility",
        title="Raw HTTP fetch",
        description=(
            'Raw HTTP fetch: GET any URL and get exactly what the server sent, status '
            'code, headers and body, plus the final URL after redirects. Every status '
            'is a result, so a 404 or 500 from the target is delivered, not failed. '
            'Input: url. Use extract.page for readable text from a rendered page.'),
        tags=["fetch", "http", "raw", "status", "headers"],
        input_schema=_URL,
        returns="url, final_url, status, content_type, bytes, text, truncated, headers{}.",
        skill="fetch.raw", max_seconds=60,
        pricing_basis="Provisional. Provider cost zero (public HTTP).",
        requires=("web",)),
    Worker(
        name="chain.rpc", price_usd=0.05, tier="utility",
        title="Base RPC passthrough",
        description=(
            'Base JSON-RPC passthrough: call any allowlisted read-only method on Base '
            'mainnet (eth_call, eth_getBalance, eth_getLogs, eth_getBlockByNumber and '
            'more) with your own params. A JSON-RPC error such as a revert reason '
            "comes back as the result, since it is the chain's own answer. Input: "
            'method, optional params.'),
        tags=["base", "blockchain", "rpc", "jsonrpc", "advanced"],
        input_schema=_obj({
            "method": {"type": "string", "description": "e.g. eth_call, eth_getLogs."},
            "params": {"type": "array", "description": "JSON-RPC positional params. Default []."},
        }, ["method"]),
        returns="method, result or error, endpoint.",
        skill="chain.rpc", max_seconds=30,
        pricing_basis="Provisional. Provider cost zero (public RPC).",
        requires=("base_rpc",)),
    Worker(
        name="market.rates", price_usd=0.02, tier="utility",
        title="Currency exchange rates",
        description=(
            "Currency exchange rates: Coinbase's full rate table for one base "
            'currency against every crypto and fiat currency it quotes, for example '
            'USD to BTC, ETH, EUR and GBP. Input: currency code.'),
        tags=["market", "rates", "currency", "exchange", "coinbase"],
        input_schema=_obj({"currency": {"type": "string", "description": "e.g. USD, ETH, BTC."}},
                          ["currency"]),
        returns="currency, rates{}.",
        skill="market.rates", max_seconds=20,
        pricing_basis="Provisional. Provider cost zero (public endpoint).",
        requires=("coinbase_market",)),
    Worker(
        name="market.ticker", price_usd=0.02, tier="utility",
        title="Crypto ticker",
        description=(
            'Crypto ticker: live best bid, best ask, last price and volume for one '
            "Coinbase product from Coinbase's Exchange data host, an independent "
            'second source to market.quote. Input: product_id such as BTC-USD.'),
        tags=["market", "ticker", "bid", "ask", "coinbase"],
        input_schema=_obj({"product_id": {"type": "string", "description": "e.g. BTC-USD."}},
                          ["product_id"]),
        returns="product_id, price, bid, ask, volume, time.",
        skill="market.ticker", max_seconds=20,
        pricing_basis="Provisional. Provider cost zero (public endpoint).",
        requires=("coinbase_market",)),
    Worker(
        name="prediction.market", price_usd=0.05, tier="utility",
        title="Prediction market by slug",
        description=(
            'One Polymarket prediction market by its exact slug, with its question '
            'and current implied probabilities. Input: slug. Use market.prediction to '
            'search markets by topic.'),
        tags=["prediction", "polymarket", "slug", "market", "odds"],
        input_schema=_obj({"slug": {"type": "string",
                                    "description": "The market's Polymarket slug."}},
                          ["slug"]),
        returns="slug, market{question,implied_probabilities[],...}.",
        skill="prediction.market", max_seconds=20,
        pricing_basis="Provisional. Provider cost zero (public API).",
        requires=("polymarket",)),
    Worker(
        name="prediction.events", price_usd=0.05, tier="utility",
        title="Prediction market events",
        description=(
            'Polymarket events, groupings of related prediction markets, ranked by '
            'trading volume with title, slug, end date and market count. Input: '
            'optional limit.'),
        tags=["prediction", "polymarket", "events", "market", "odds"],
        input_schema=_obj({"limit": {"type": "integer", "description": "1-50, default 10."}}, []),
        returns="events[{id,title,slug,volume,end_date,market_count}], count.",
        skill="prediction.events", max_seconds=20,
        pricing_basis="Provisional. Provider cost zero (public API).",
        requires=("polymarket",)),
    # --- wave 2b: fail-closed until the operator enables one Google product -
    Worker(
        name="maps.places", price_usd=0.10, tier="utility",
        title="Search places",
        description=(
            'Places search: find businesses, addresses and points of interest by '
            "free-text query through Google Maps Grounding Lite, for example 'coffee "
            "near the Ferry Building, San Francisco'. Input: query, optional "
            'region_code (ISO country code) to bias results.'),
        tags=["maps", "places", "search", "google", "geospatial"],
        input_schema=_obj({
            "query": {"type": "string", "description": "What to find, e.g. 'coffee near the Ferry Building'."},
            "region_code": {"type": "string", "description": "Optional ISO 3166-1 alpha-2 bias."},
        }, ["query"]),
        returns="query, result (places found, per Maps Grounding Lite's own shape).",
        skill="maps.places", max_seconds=40,
        pricing_basis="Provisional. Maps Grounding Lite's own billing is not yet measured here.",
        requires=("maps_grounding",)),
    Worker(
        name="maps.route", price_usd=0.10, tier="utility",
        title="Compute a route",
        description=(
            'Directions and travel time between two named places through Google Maps '
            'Grounding Lite. Input: origin, destination, optional travel_mode (DRIVE, '
            'WALK, BICYCLE or TRANSIT; default DRIVE).'),
        tags=["maps", "route", "directions", "google", "geospatial"],
        input_schema=_obj({
            "origin": {"type": "string"},
            "destination": {"type": "string"},
            "travel_mode": {"type": "string", "description": "DRIVE, WALK, BICYCLE or TRANSIT. Default DRIVE."},
        }, ["origin", "destination"]),
        returns="origin, destination, travel_mode, result (route, per Maps Grounding Lite's own shape).",
        skill="maps.route", max_seconds=40,
        pricing_basis="Provisional. Maps Grounding Lite's own billing is not yet measured here.",
        requires=("maps_grounding",)),
    Worker(
        name="maps.weather", price_usd=0.10, tier="utility",
        title="Weather at a location",
        description=(
            'Weather at a named place, current or forecast, through Google Maps '
            'Grounding Lite. Input: location as a place name or address.'),
        tags=["maps", "weather", "forecast", "google", "geospatial"],
        input_schema=_obj({"location": {"type": "string"}}, ["location"]),
        returns="location, result (weather, per Maps Grounding Lite's own shape).",
        skill="maps.weather", max_seconds=40,
        pricing_basis="Provisional. Maps Grounding Lite's own billing is not yet measured here.",
        requires=("maps_grounding",)),
    Worker(
        name="video.generate", price_usd=10.00, tier="premium",
        title="Generate a video",
        description=(
            'Video generation: a short clip from a text prompt with Google Veo, '
            'returned as base64 video bytes (or a GCS URI when large) with its MIME '
            'type. Input: prompt, optional aspect_ratio (16:9 or 9:16), '
            'duration_seconds (4, 6 or 8) and generate_audio.'),
        tags=["video", "generate", "veo", "media", "visual"],
        input_schema=_obj({
            "prompt": {"type": "string"},
            "aspect_ratio": {"type": "string", "description": "16:9 or 9:16. Default 16:9."},
            "duration_seconds": {"type": "integer", "description": "4, 6 or 8. Default 6."},
            "generate_audio": {"type": "boolean", "description": "Default false."},
        }, ["prompt"]),
        returns="prompt, aspect_ratio, duration_seconds, video_base64 or gcs_uri, mime_type, model.",
        skill="video.generate", max_seconds=MAX_WORKER_SECONDS,
        pricing_basis="Provisional. Flat per-second rate once measured; one attempt only (a retry would re-bill the vendor).",
        requires=("veo",)),
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


# One realistic value per input field name, used to build each worker's
# request example for openapi.json. Keyed by field name rather than per row so
# a new worker reusing a field gets a valid example for free; a guard test
# fails if a required field has no value here.
_EXAMPLE_VALUES = {
    "url": "https://example.com",
    "address": "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd",
    # A real Base transaction (a settled HubVibe sale), so the example runs.
    "hash": "0x9e61e3fce3efad669a236b8d6a0351162c572808026d1a5ffdededd31caad113",
    "product_id": "BTC-USD",
    "text": "HubVibe sells machine-payable site audits at $0.05 per call.",
    "question": "What does it sell, and at what price?",
    "fields": ["title", "pricing"],
    "sql": ("SELECT name, SUM(number) AS n FROM "
            "`bigquery-public-data.usa_names.usa_1910_2013` "
            "GROUP BY name ORDER BY n DESC LIMIT 5"),
    "table": "bigquery-public-data.usa_names.usa_1910_2013",
    "history_table": "bigquery-public-data.usa_names.usa_1910_2013",
    "target_table": "bigquery-public-data.usa_names.usa_1910_2013",
    "timestamp_col": "year",
    "data_col": "number",
    "query": "x402 payment protocol",
    "code": "print(sum(range(10)))",
    "prompt": "A beehive built from circuit boards, isometric illustration",
    "audio_base64": "aGVsbG8=",
    "claims": ["HubVibe sells machine-payable site audits."],
    "sources": ["https://example.com"],
    "company": "Anthropic",
    "method": "eth_blockNumber",
    "currency": "USD",
    "slug": "example-prediction-market-slug",
    "origin": "San Francisco, CA",
    "destination": "Oakland, CA",
    "location": "San Francisco, CA",
}

_DAILY_SERIES = "bigquery-public-data.covid19_nyt.us_states"

_EXAMPLE_OVERRIDES = {
    # Either source is valid, so nothing is `required`; the example shows the
    # inline form with a prediction and a probability query.
    "stats.probability": {"points": [[1, 2.1], [2, 3.9], [3, 6.2], [4, 7.8], [5, 10.1]],
                          "predict_x": [6], "probability_queries": [{"below": 8}]},
    # llm.generate and image.generate both take a required "prompt", but
    # sharing one example would make one of the two look like a mistake.
    "image.generate": {"prompt": "A beehive built from circuit boards, isometric illustration"},
    "video.generate": {"prompt": "A single bee landing on a circuit-board flower, slow motion"},
    # maps.places shares "query" with search.web; a place search needs a place.
    "maps.places": {"query": "coffee near the Ferry Building, San Francisco"},
    # AI.FORECAST / AI.DETECT_ANOMALIES need a DATE/TIMESTAMP column (the
    # usa_names `year` is INT64 and is refused); this is a real daily series.
    "data.forecast": {"table": _DAILY_SERIES, "timestamp_col": "date",
                      "data_col": "confirmed_cases", "id_cols": ["state_name"]},
    "data.anomalies": {"history_table": _DAILY_SERIES, "target_table": _DAILY_SERIES,
                       "timestamp_col": "date", "data_col": "confirmed_cases",
                       "id_cols": ["state_name"]},
}


def example_for(worker: "Worker") -> dict:
    """A request body carrying every required field of this worker."""
    required = worker.input_schema.get("required") or []
    example = {field: _EXAMPLE_VALUES[field] for field in required if field in _EXAMPLE_VALUES}
    example.update(_EXAMPLE_OVERRIDES.get(worker.name, {}))
    return example


# Both official x402 client libraries (Python >= 2.22, TypeScript >= 2.26)
# refuse any single payment over $1.00 unless the BUYER raises its spend cap
# -- a wallet guard, and rightly the buyer's to lift. A stock agent hits that
# guard locally, before we ever see it, and reads "no matching requirements".
# So every surface that quotes a price above $1 also says, in the agent's own
# library's words, exactly how to lift it. Nothing here changes the price.
SPEND_CAP_USD = 1.00


def buyer_note(worker: "Worker") -> Optional[str]:
    """How a stock x402 client buys this worker, or None when the default
    $1 per-payment cap already covers it."""
    if worker.price_usd <= SPEND_CAP_USD:
        return None
    return (
        f"This call costs ${worker.price_usd:.2f}, above the $1.00 per-payment "
        "default spend cap in the x402 client libraries. Raise the cap before "
        "paying -- Python: client.set_spend_controls({'max_amount_per_payment': "
        f"'${worker.price_usd:.2f}'}}); TypeScript: new x402Client({{ spendControls: "
        f"{{ maxAmountPerPayment: '${worker.price_usd:.2f}' }} }}). "
        "Coinbase's x402_pay tool takes max_amount per call instead."
    )


def response_schema(worker: "Worker") -> dict:
    """The full 200 schema of this worker's route (envelope + its result)."""
    return contract.response_schema(worker)


def response_example(worker: "Worker") -> dict:
    """An example 200 body, generated from the schema so it cannot drift."""
    return contract.response_example(worker)


def output_example(worker: "Worker") -> dict:
    """An example `result` for this worker, generated from its schema."""
    return contract.output_example(worker)


def description_of(path: str) -> Optional[str]:
    worker = BY_PATH.get(path)
    return worker.description if worker else None


def live() -> list:
    """Only the workers this deployment can deliver. What gets advertised."""
    return [worker for worker in CATALOG if worker.available()]
