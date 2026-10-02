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

ALWAYS CURRENT. Every call reads its source at call time; the node serves
no result from a cache. Two deliberate exceptions, both documented: a
repeated Idempotency-Key returns the stored result of that same request
(never charged twice), and monitor.check compares against the baseline
monitor.snapshot saved. The workers whose contracts carry `checked_at`
(UTC) -- commerce.availability, market.stock, market.fundamentals,
finance.analytics -- say when the answer was true; the market/finance
three also carry `as_of` when the source stamps its own data. A new
live-world worker should carry both.

LANGUAGE IS NOT A BARRIER. Every worker whose result contains prose takes
the optional `language` below (a BCP-47 tag) and answers in it; the rule is
implemented once, in skills/llm.py, and the composites inherit it.
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


_LANGUAGE = {
    "type": "string", "maxLength": 35,
    "pattern": "^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$",
    "description": "Optional answer language, BCP-47 (en, ja, pt-BR). Default: the input's.",
}
_URL_LANG = _obj({"url": _URL["properties"]["url"], "language": _LANGUAGE}, ["url"])


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


_SYMBOL = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9.\\-]{0,11}$",
           "description": "Ticker symbol, e.g. AAPL, BRK.B, RY-PC."}
_RANGES = ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"]
_PRICE_ITEM = {"oneOf": [
    {"type": "number", "exclusiveMinimum": 0, "description": "A price."},
    {"type": "object", "properties": {"date": {"type": "string"}, "close": {"type": "number", "exclusiveMinimum": 0}},
     "required": ["close"], "additionalProperties": True, "description": "{date, close}."},
]}
_PRICES = {"type": "array", "items": _PRICE_ITEM, "minItems": 2, "maxItems": 100000,
           "description": "Prices oldest first: numbers, or {date, close} objects. Use this OR `symbol`."}
_FINANCE_METRICS = ["returns", "volatility", "sharpe", "sortino", "drawdown", "var", "beta",
                    "moving_averages", "rsi", "bollinger", "black_scholes", "kelly"]
_WINDOW = {"oneOf": [{"type": "integer", "minimum": 2, "maximum": 5000},
                     {"type": "array", "items": {"type": "integer", "minimum": 2, "maximum": 5000},
                      "minItems": 1, "maxItems": 10}]}
_FINANCE_INPUT = dict(_obj({
    "prices": _PRICES,
    "symbol": dict(_SYMBOL, description="Fetch the series live instead of `prices` (daily closes). Use this OR `prices`."),
    "range": {"type": "string", "enum": _RANGES, "description": "History range with `symbol`, default 1y."},
    "benchmark_prices": dict(_PRICES, description="Benchmark series for beta/alpha/correlation, same order as `prices`."),
    "benchmark_symbol": dict(_SYMBOL, description="Benchmark fetched live (e.g. SPY) for beta/alpha/correlation."),
    "metrics": {"type": "array", "items": {"type": "string", "enum": _FINANCE_METRICS}, "minItems": 1,
                "uniqueItems": True,
                "description": ("Which results to compute. Default: returns, volatility, sharpe, sortino, drawdown, "
                                "var, moving_averages, rsi, bollinger, plus beta/black_scholes/kelly when their "
                                "inputs are given.")},
    "periods_per_year": {"type": "integer", "minimum": 1, "maximum": 100000,
                         "description": "Annualization basis: 252 trading days (default), 12 months, 365 days, 52 weeks."},
    "risk_free_rate": {"type": "number", "minimum": -1, "maximum": 10,
                       "description": "Annual risk-free rate as a fraction (0.04 = 4%). Default 0."},
    "alpha": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5,
              "description": "Tail probability for VaR/CVaR. Default 0.05."},
    "windows": {"type": "object", "properties": {"sma": _WINDOW, "ema": _WINDOW, "rsi": _WINDOW, "bollinger": _WINDOW},
                "additionalProperties": False,
                "description": "Indicator windows. Default sma [20,50,200], ema [12,26], rsi 14, bollinger 20."},
    "option": {"type": "object", "properties": {
        "type": {"type": "string", "enum": ["call", "put"]},
        "strike": {"type": "number", "exclusiveMinimum": 0},
        "spot": {"type": "number", "exclusiveMinimum": 0, "description": "Default: the last price of the series."},
        "rate": {"type": "number", "description": "Continuous risk-free rate; default risk_free_rate."},
        "volatility": {"type": "number", "exclusiveMinimum": 0,
                       "description": "Annual volatility as a fraction; default: the series' annualized log-return volatility."},
        "time_to_expiry_years": {"type": "number", "exclusiveMinimum": 0},
        "dividend_yield": {"type": "number", "minimum": 0, "description": "Continuous yield, default 0."},
    }, "required": ["type", "strike", "time_to_expiry_years"], "additionalProperties": False,
        "description": "European option to price with Black-Scholes-Merton (enables black_scholes)."},
    "kelly": {"type": "object", "properties": {
        "win_probability": {"type": "number", "minimum": 0, "maximum": 1},
        "win_loss_ratio": {"type": "number", "exclusiveMinimum": 0, "description": "Average win / average loss (b in b:1)."},
    }, "required": ["win_probability", "win_loss_ratio"], "additionalProperties": False,
        "description": "Bet parameters for the Kelly fraction (enables kelly)."},
}, []), oneOf=[{"required": ["prices"]}, {"required": ["symbol"]}])


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
            'Base network status and gas price: current block height and gas price (wei and '
            'gwei), live from a Base mainnet RPC node. Use it to check Base is moving, time '
            'an on-chain action or estimate gas before sending a transaction. Keyless, pay '
            'per call. No input needed.'),
        tags=["base", "gas-price", "block-height", "blockchain", "onchain", "rpc", "live"],
        input_schema=_obj({}, []),
        returns="block_number, gas_price_wei, gas_price_gwei.",
        skill="chain.network", max_seconds=30,
        pricing_basis="Provisional. Provider cost is zero (public RPC); price covers infrastructure.",
        requires=("base_rpc",)),
    Worker(
        name="chain.address", price_usd=0.05, tier="utility",
        title="Base address report",
        description=(
            'Base wallet lookup / address balance: ETH balance, transaction count (nonce) and'
            ' whether the address is a smart contract or an ordinary wallet (EOA), live from '
            'Base mainnet in one call. Use it to check a counterparty, deposit address or '
            'contract before paying it. Input: address (0x...). For token balances or any '
            'other read use chain.rpc.'),
        tags=["base", "wallet", "balance", "address-lookup", "onchain", "eoa", "contract"],
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
            'Base transaction lookup by hash: sender, recipient, ETH value, block, success or'
            ' failure, gas used and log count, with the receipt folded in, live from Base '
            'mainnet. Use it to confirm a payment or transfer landed on-chain. Input: hash '
            '(the 0x transaction hash). For the raw receipt use chain.rpc.'),
        tags=["base", "transaction", "tx-lookup", "receipt", "payment-confirmation", "onchain"],
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
            'Crypto price / live coin quote: spot price, 24-hour change and 24-hour volume '
            'for Bitcoin, Ethereum, Solana or any Coinbase product (BTC-USD, ETH-USD, '
            "SOL-USD...), read at call time from Coinbase's public market data. Use it when "
            'an agent needs a current cryptocurrency price. Input: product_id such as '
            'BTC-USD. Bid/ask: market.ticker. Whole rate table: market.rates.'),
        tags=["crypto-price", "bitcoin", "ethereum", "solana", "price", "quote", "market-data", "live"],
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
            'Prediction market odds: live Polymarket markets on any topic (elections, Fed '
            'rate cuts, crypto, sports, AI, geopolitics) with the probability each outcome '
            'implies as a percentage, plus volume, liquidity and end date. Search by topic '
            "words or tickers (BTC, 'fed rate cut') or take the biggest markets. A topic no "
            'open market matches is refused free, never sold empty. Input: optional query, '
            'limit. One market by slug: prediction.market.'),
        tags=["prediction-market", "polymarket", "odds", "probability", "forecast", "elections", "betting-odds"],
        input_schema=_obj({
            "query": {"type": "string", "description": "Topic words to match, e.g. 'fed rate cut' or 'BTC 150k' (optional)."},
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
            'Web scraping / web page to clean text: any URL rendered in a real browser '
            '(JavaScript pages included) and returned as readable text with title, '
            'description and links. Use it to read an article, docs page or product page '
            'before summarising or extracting. Input: url. Raw status, headers and body: '
            'fetch.raw. A question answered from the page: research.brief.'),
        tags=["web-scraping", "scrape", "extract", "html-to-text", "browser", "reader", "crawl"],
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
            'Question answering over your own text: ask a question about material you supply '
            'and get an answer grounded only in that text (Gemini). If the text does not '
            'contain the answer it says so instead of guessing. Use it for summaries, Q&A and'
            ' checks over documents, emails and pages. Input: text, question; optional '
            'language. Fixed JSON fields: llm.extract.'),
        tags=["llm", "summarize", "question-answering", "rag", "grounded", "gemini", "document-qa"],
        input_schema=_obj({
            "text": {"type": "string", "description": "Material to analyse."},
            "question": {"type": "string", "description": "What to answer. Optional."},
            "language": _LANGUAGE,
        }, ["text"]),
        returns="answer, model, tokens{prompt,output}.",
        skill="llm.analyze", max_seconds=150,
        pricing_basis="Provisional. Token usage is measured per call; set GEMINI_PRICE_PER_MTOK_IN/_OUT to measure cost.",
        requires=("gemini",)),
    Worker(
        name="llm.extract", price_usd=0.25, tier="standard",
        title="Extract fields as JSON",
        description=(
            'Structured data extraction / text to JSON: pull the fields you name out of any '
            'text into a JSON object with exactly those keys, null where the text does not '
            'state a value (Gemini, no invented values). Use it to turn emails, invoices, '
            'pages or documents into clean records. Input: text, fields[]. From a live web '
            'page: research.page_facts.'),
        tags=["extraction", "json", "structured-output", "parsing", "llm", "entity-extraction", "gemini"],
        input_schema=_obj({
            "text": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "Field names to extract."},
            "language": _LANGUAGE,
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
            "BigQuery SQL: run a read-only SQL query, including against Google's public "
            'datasets (Wikipedia, GitHub, blockchains, weather, census and more), and get '
            'columns and rows back as JSON with the bytes processed. Every query is dry-run '
            'first and refused above the scan ceiling, so cost is bounded before it runs. '
            'Input: sql; optional max_scan_gib. Ask in plain English instead: data.question.'),
        tags=["sql", "bigquery", "database", "public-datasets", "analytics", "data", "query"],
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
            'Ask a database in plain English (text to SQL): any BigQuery table, including '
            "Google's public datasets. The SQL is written for you, run under a byte ceiling, "
            'and the rows are read back into a written answer, returned with the SQL, columns'
            ' and rows it used. Use it for analytics questions without writing SQL. Input: '
            'question, table; optional columns.'),
        tags=["text-to-sql", "analytics", "bigquery", "natural-language-query", "data-analysis", "sql"],
        input_schema=_obj({
            "question": {"type": "string"},
            "table": {"type": "string",
                      "description": "Fully qualified: project.dataset.table."},
            "columns": {"type": "array", "items": {"type": "string"},
                        "description": "Optional column hints."},
            "language": _LANGUAGE,
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
            'Summarize and analyze a web page: the URL is rendered in a real browser, its '
            'content extracted, and a written brief answering your question returned, '
            'grounded in that page only and saying so when the page does not answer. Use it '
            'for competitor pages, docs, articles and listings. Input: url; optional '
            'question. Fixed JSON fields: research.page_facts. Needs a web search: '
            'research.web.'),
        tags=["summarize", "web-page", "research", "analysis", "browser", "brief", "grounded"],
        input_schema=_obj({
            "url": {"type": "string"},
            "question": {"type": "string", "description": "Optional; defaults to an overview."},
            "language": _LANGUAGE,
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
            'Structured facts from a web page / scrape to JSON: name the fields you need '
            '(pricing, plans, contact, product names, anything) and get exactly those keys '
            'back as JSON from the live page rendered in a real browser, null where the page '
            'does not say. Use it for price monitoring, lead data and catalog extraction. '
            'Input: url, fields[].'),
        tags=["web-scraping", "structured-data", "price-monitoring", "lead-generation", "json", "extraction"],
        input_schema=_obj({
            "url": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "string"}},
            "language": _LANGUAGE,
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
            'Crypto market sentiment in one call: live Coinbase spot price, live Polymarket '
            'probabilities and recent news headlines (GDELT) on the same subject, analysed '
            'together in a written summary that says what the numbers do and do not support. '
            'Use it for a quick read on Bitcoin, Ethereum or any coin. Input: optional '
            'product_id (BTC-USD...), query, question, limit. Market data only, not '
            'investment advice.'),
        tags=["crypto", "sentiment", "market-analysis", "bitcoin", "polymarket", "news", "research"],
        input_schema=_obj({
            "product_id": {"type": "string", "description": "e.g. BTC-USD. Default BTC-USD."},
            "query": {"type": "string", "description": "Prediction-market topic. Optional."},
            "question": {"type": "string", "description": "Optional analysis question."},
            "limit": {"type": "integer"},
            "language": _LANGUAGE,
        }, []),
        returns="spot{}, prediction_markets[], news[], news_note, analysis, disclaimer.",
        skill="market.intel", max_seconds=200,
        pricing_basis="Provisional, completed-work tier. Three provider calls; usage measured per call.",
        composes=["market.quote", "market.prediction", "news.search", "llm.analyze"],
        requires=("coinbase_market", "polymarket", "gemini")),
    # --- wave 1: bees on live Google credentials -----------------------------
    Worker(
        name="search.web", price_usd=0.10, tier="utility",
        title="Web search",
        description=(
            "Web search with an answer: real-time search of Brave's independent web index, "
            'answered from the current results only, with every source URL and title '
            "returned, never the model's memory. Use it when an agent needs up-to-date "
            'information, latest news, prices or facts from the web. Input: query; optional '
            'language. Raw result list (SERP): search.results. Sources read in full with '
            'citations: research.web.'),
        tags=["web-search", "search", "internet", "real-time", "brave", "answer", "sources"],
        input_schema=_obj({"query": {"type": "string"}, "language": _LANGUAGE}, ["query"]),
        returns="query, answer, sources[{url,title}], search_queries_used[], model.",
        skill="search.web", max_seconds=60,
        pricing_basis="Provisional. One Brave request ($0.005) plus one short model call, both measured.",
        requires=("web_answer",)),
    Worker(
        name="llm.generate", price_usd=0.25, tier="standard",
        title="Raw text completion",
        description=(
            'LLM text generation / chat completion: a raw completion from your prompt with '
            'optional system message, max_tokens and temperature; Gemini by default, other '
            'configured models by provider and model. Use it for drafting, rewriting, '
            'translation, classification or any prompt, paid per call with no API key or '
            'account. Input: prompt. Answer grounded in your own text: llm.analyze.'),
        tags=["llm", "text-generation", "chat-completion", "gemini", "ai", "inference", "prompt"],
        input_schema=_obj({
            "prompt": {"type": "string", "description": "The prompt."},
            "system": {"type": "string", "description": "Optional system message."},
            "max_tokens": {"type": "integer", "description": "1-4096, default 1024."},
            "temperature": {"type": "number", "description": "0-2, default 0.7."},
            "provider": {"type": "string", "description": "Optional: gemini or anthropic."},
            "model": {"type": "string", "description": "Optional: a specific model from that provider."},
            "language": _LANGUAGE,
        }, ["prompt"]),
        returns="text, model, provider, finish_reason, usage{input_tokens,output_tokens}.",
        skill="llm.generate", max_seconds=120,
        pricing_basis="Provisional. Token usage measured per call.",
        requires=("completion",)),
    Worker(
        name="code.execute", price_usd=0.25, tier="standard",
        title="Execute Python",
        description=(
            "Run Python code in a sandbox: execute Python in Google's hosted sandbox (not on "
            "this service's machines) and get the code, its printed output and the outcome "
            'back. Use it for calculations, data transforms and checks an agent should not do'
            ' in its head. Input: code. Questions over BigQuery data: data.query.'),
        tags=["python", "code-execution", "sandbox", "compute", "interpreter", "calculation"],
        input_schema=_obj({"code": {"type": "string"}}, ["code"]),
        returns="code, output, outcome, summary, model.",
        skill="code.execute", max_seconds=90,
        pricing_basis="Provisional. Token usage measured per call.",
        requires=("code_exec",)),
    Worker(
        name="image.generate", price_usd=0.50, tier="standard",
        title="Generate an image",
        description=(
            'AI image generation / text to image: one image from a text prompt with Google '
            'Imagen 4, returned as base64 image bytes with its MIME type. Use it for '
            'illustrations, thumbnails, product mockups and social images. Input: prompt; '
            'optional aspect_ratio.'),
        tags=["image-generation", "text-to-image", "imagen", "ai-art", "illustration", "picture"],
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
            'Text to speech (TTS): convert text to natural spoken MP3 audio with Google Cloud'
            ' Text-to-Speech, returned as base64. Use it for voice replies, narration, audio '
            'versions of text and accessibility. Input: text; optional voice name.'),
        tags=["text-to-speech", "tts", "voice", "audio", "speech", "mp3"],
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
            'Speech to text / audio transcription: transcribe up to 60 seconds or 10 MB of '
            'audio with Google Cloud Speech-to-Text and get the transcript with its '
            'confidence and detected language. Use it for voice notes, call snippets and '
            'clips. Input: audio_base64; optional language_code. Longer audio is refused '
            'before payment.'),
        tags=["speech-to-text", "transcription", "stt", "audio", "voice", "asr"],
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
            "Time-series forecasting: forecast any BigQuery table with Google's pretrained "
            'TimesFM model (AI.FORECAST), nothing to train. Point it at the table, timestamp '
            'column and value column and get forecast rows with bounds for the horizon you '
            'set, per series with id_cols. Use it for sales, traffic, demand or metric '
            'forecasts. Input: table, timestamp_col, data_col; optional horizon, id_cols.'),
        tags=["forecasting", "time-series", "prediction", "timesfm", "bigquery", "demand-forecast"],
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
            'Anomaly detection on a time series: score recent periods against history with '
            'BigQuery AI.DETECT_ANOMALIES (TimesFM). Give one table and its latest periods '
            'are scored against its own history, or a history and a target table. Returns '
            'every checked row with bounds, anomaly flag and probability, plus the anomaly '
            'count. Input: history_table, target_table, timestamp_col, data_col; optional '
            'target_last, threshold, id_cols.'),
        tags=["anomaly-detection", "outliers", "time-series", "monitoring", "timesfm", "bigquery"],
        input_schema=_obj({
            "history_table": {"type": "string", "description": "project.dataset.table"},
            "target_table": {"type": "string", "description": "project.dataset.table"},
            "timestamp_col": {"type": "string"},
            "data_col": {"type": "string"},
            "anomaly_prob_threshold": {"type": "number",
                                       "description": "0.5-0.999, default 0.95."},
            "id_cols": {"type": "array", "items": {"type": "string"},
                       "description": "Optional: one series per value of these columns."},
            "target_last": {"type": "integer", "minimum": 1, "maximum": 366,
                            "description": "Same table as history and target: score its latest N timestamps. Default 30."},
            "max_scan_gib": {"type": "number"},
        }, ["history_table", "target_table", "timestamp_col", "data_col"]),
        returns=("history_table, target_table, timestamp_col, data_col, anomaly_prob_threshold, mode, target_periods, "
                 "columns[], rows[], row_count, anomaly_count, gib_processed."),
        skill="data.anomalies", max_seconds=200,
        pricing_basis="Provisional, completed-work tier. Bytes scanned measured per call.",
        requires=("bigquery",)),
    Worker(
        name="stats.probability", price_usd=0.50, tier="standard",
        title="Predictive probability engine: regression, normal model, p-values",
        description=(
            'Predictive probability and regression engine: least-squares linear regression '
            'with standard errors, confidence and prediction intervals, a fitted normal '
            'distribution with quantiles and probability queries (P(y > t)), exact Student t '
            'and Jarque-Bera p-values. Points inline or from a BigQuery table. Deterministic '
            'arithmetic, no LLM: the same input always returns the same numbers. Input: '
            'points or table with x_column, y_column; optional metrics, predict_x, '
            'probability_queries.'),
        tags=["statistics", "regression", "probability", "prediction", "confidence-interval", "math", "deterministic"],
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
            'Fact check claims against sources: up to 10 claims checked against up to 4 '
            'source URLs you name; each comes back SUPPORTED, CONTRADICTED or UNSUPPORTED '
            'with the exact quote the verdict rests on. Use it to verify AI output, citations'
            ' or statements before publishing. Input: claims[], sources[]. A question but no '
            'sources yet: research.web.'),
        tags=["fact-check", "verification", "claims", "citations", "hallucination-check", "sources"],
        input_schema=_obj({
            "claims": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "string"}},
            "language": _LANGUAGE,
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
            'Deep web research with citations: search the live web for your question, read '
            'the top sources in full, and get a written answer with every claim tied to a '
            'numbered source. Use it when an agent needs a sourced answer, not a list of '
            'links. Input: question; optional max_sources. Quick answer with links: '
            'search.web.'),
        tags=["research", "deep-research", "citations", "web-search", "report", "sources"],
        input_schema=_obj({
            "question": {"type": "string"},
            "max_sources": {"type": "integer", "description": "1-4, default 3."},
            "language": _LANGUAGE,
        }, ["question"]),
        returns="question, answer, sources[{n,url,title}], partial[], model.",
        skill="research.web", max_seconds=220,
        pricing_basis="Provisional, completed-work tier. Up to five provider calls; usage measured per call.",
        composes=["search.web", "extract.page", "llm.analyze"],
        requires=("brave", "web", "gemini")),
    Worker(
        name="research.company", price_usd=10.00, tier="premium",
        title="Research and verify a company",
        description=(
            'Company research report: what a company does, its products and anything notable,'
            ' from the live web (official site and Wikipedia first) and recent GDELT news, '
            'every claim cited to a numbered source, thin or conflicting evidence disclosed. '
            'Use it for due diligence, sales prep and competitor research. Input: company '
            'name; optional max_sources. Structured firmographics in one cheap call: '
            'company.enrich.'),
        tags=["company-research", "due-diligence", "competitor-analysis", "sales-research", "report", "citations"],
        input_schema=_obj({
            "company": {"type": "string"},
            "max_sources": {"type": "integer", "description": "1-4, default 4."},
            "language": _LANGUAGE,
        }, ["company"]),
        returns="company, report, sources[{n,url,title}], partial[], news[], news_note, model.",
        skill="research.company", max_seconds=220,
        pricing_basis="Provisional, completed-work tier. Up to six provider calls; usage measured per call.",
        composes=["search.web", "extract.page", "news.search", "llm.analyze"],
        requires=("brave", "web", "gemini")),
    Worker(
        name="monitor.snapshot", price_usd=0.50, tier="standard",
        title="Save a monitoring baseline",
        description=(
            'Website change monitoring, step one: fetch a page and store it as the baseline '
            'to compare against later. Use it to watch pricing pages, terms, job boards or '
            'competitors. Input: url. Then call monitor.check to learn whether and how it '
            'changed.'),
        tags=["monitoring", "website-change", "change-detection", "watch", "diff"],
        input_schema=_URL,
        returns="url, title, text_chars, content_hash, note.",
        skill="monitor.snapshot", max_seconds=90,
        pricing_basis="Provisional. Runs on our own flat-rate browser; marginal provider cost near zero.",
        requires=("web",)),
    Worker(
        name="monitor.check", price_usd=0.50, tier="standard",
        title="Check a page against its baseline",
        description=(
            'Website change detection, step two: re-fetch a page saved with monitor.snapshot '
            'and get whether it changed, how old the baseline is, and a written summary of '
            'what changed. Use it to track price changes, terms updates, new job posts or '
            'competitor moves. Input: url; optional language.'),
        tags=["monitoring", "change-detection", "website-change", "diff", "alerts"],
        input_schema=_URL_LANG,
        returns="url, title, changed, baseline_age_seconds, change_summary, model.",
        skill="monitor.check", max_seconds=150,
        pricing_basis="Provisional. Browser fetch plus one inference call when something changed; usage measured.",
        requires=("web", "gemini")),
    # --- live transactional availability -----------------------------------
    Worker(
        name="commerce.availability", price_usd=0.50, tier="standard",
        title="Live availability: can this be bought or booked right now?",
        description=(
            'Product availability and price check: is it in stock and buyable right now, at '
            'what price, in which options (sizes, colours, dates, rooms), which option '
            'matches the variant you name, quantity and ship-to eligibility where the page '
            'states them, with evidence and a checked_at stamp. Reads any product or booking '
            'page (schema.org, Open Graph, Shopify JSON; a browser only when needed). Input: '
            'url; optional variant, quantity, ship_to.'),
        tags=["in-stock", "availability", "price-check", "ecommerce", "shopping", "product", "inventory"],
        input_schema=_obj({
            "url": {"type": "string", "description": "The product, listing or booking page."},
            "variant": {"type": "string", "maxLength": 200,
                        "description": "Optional: the option you want, in words (size 9 natural black; double room 12 Oct; economy)."},
            "quantity": {"type": "integer", "minimum": 1, "maximum": 100000,
                         "description": "Optional: how many you need."},
            "ship_to": {"type": "string", "minLength": 2, "maxLength": 2,
                        "description": "Optional: ISO 3166-1 alpha-2 country the order ships to."},
            "language": _LANGUAGE,
        }, ["url"]),
        returns=("available, availability, price, currency, options[], matched_option, "
                 "quantity_ok, ship_to_ok, shipping, eligibility_notes[], evidence[], "
                 "source, confidence, checked_at."),
        skill="commerce.availability", max_seconds=200,
        pricing_basis=("Provisional, standard tier. One or two plain fetches on most pages; "
                       "a browser render and one inference call only when the page has no "
                       "structured data; usage measured."),
        requires=("web", "gemini")),
    # --- markets and trading mathematics -----------------------------------
    Worker(
        name="market.stock", price_usd=0.05, tier="utility",
        title="Live stock quote and daily history",
        description=(
            'Stock price / live stock quote for any US ticker: last price, change, day range,'
            ' volume and market state, plus daily OHLCV history (1d to 5y), read at call time'
            " from Nasdaq's public data with Yahoo Finance as fallback. Every answer names "
            "its source, the source's timestamp, its delay in minutes and when it was read. "
            'Input: symbol (AAPL, NVDA...); optional range, include_history.'),
        tags=["stock-price", "stock-quote", "equities", "ohlcv", "market-data", "nasdaq", "ticker"],
        input_schema=_obj({
            "symbol": _SYMBOL,
            "range": {"type": "string", "enum": _RANGES, "description": "History range, default 1mo."},
            "include_history": {"type": "boolean", "description": "Default true."},
        }, ["symbol"]),
        returns=("symbol, name, exchange, currency, price, change, change_pct, previous_close, "
                 "day_high, day_low, volume, market_state, as_of, delayed_minutes, history[], "
                 "history_rows, source, checked_at."),
        skill="market.stock", max_seconds=45,
        pricing_basis="Provisional. Provider cost zero (public endpoints); two requests at most.",
        requires=("equities",)),
    Worker(
        name="market.fundamentals", price_usd=0.05, tier="utility",
        title="SEC-filed fundamentals (XBRL company facts)",
        description=(
            'SEC filings / company fundamentals: revenue, net income, diluted EPS, operating '
            'income, assets, liabilities, equity, cash and shares outstanding by quarter and '
            "year (10-K, 10-Q) as the company reported them to the SEC, from EDGAR's official"
            ' XBRL API at call time. Name any other XBRL line item; restatements resolve to '
            'the latest filing. Input: symbol (US ticker) or cik; optional concepts, periods,'
            ' forms.'),
        tags=["sec-filings", "fundamentals", "earnings", "financial-statements", "edgar", "xbrl", "10-k"],
        input_schema=dict(_obj({
            "symbol": dict(_SYMBOL, description="US ticker in the SEC's table, e.g. AAPL. Use this OR `cik`."),
            "cik": {"type": ["integer", "string"], "description": "SEC Central Index Key, e.g. 320193."},
            "concepts": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 25,
                         "description": ("XBRL concept names (us-gaap, ifrs-full or dei). Default: Revenues, "
                                         "NetIncomeLoss, EarningsPerShareDiluted, OperatingIncomeLoss, Assets, "
                                         "Liabilities, StockholdersEquity, CashAndCashEquivalentsAtCarryingValue, "
                                         "EntityCommonStockSharesOutstanding.")},
            "periods": {"type": "integer", "minimum": 1, "maximum": 40,
                        "description": "Most recent reporting periods per concept, default 8."},
            "forms": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                      "description": "Restrict to these forms, e.g. [\"10-K\"] for annual figures only."},
        }, []), anyOf=[{"required": ["symbol"]}, {"required": ["cik"]}]),
        returns=("symbol, cik, entity_name, concepts{name: {concept, taxonomy, label, unit, values[]}}, "
                 "concepts_missing[], periods, forms, as_of, source, checked_at."),
        skill="market.fundamentals", max_seconds=60,
        pricing_basis="Provisional. Provider cost zero (official public API); two requests at most.",
        requires=("sec_edgar",)),
    Worker(
        name="market.insiders", price_usd=0.10, tier="utility",
        title="Insider trades from SEC Form 4 filings",
        description=(
            "Insider trading (SEC Form 4): what a company's officers, directors and 10% "
            'owners bought and sold, parsed from their Form 4 filings at call time from '
            'EDGAR. Each trade: insider, role, date, code and meaning, shares, price, value, '
            'holdings after, 10b5-1 plan flag, filing link. Summary: open-market buys vs '
            'sells, net shares and dollars. Input: symbol (US ticker) or cik; optional days '
            '(default 90), codes such as ["P","S"].'),
        tags=["insider-trading", "form-4", "sec", "insider-buying", "stocks", "edgar", "signals"],
        input_schema=dict(_obj({
            "symbol": dict(_SYMBOL, description="US ticker in the SEC's table, e.g. NVDA. Use this OR `cik`."),
            "cik": {"type": ["integer", "string"], "description": "SEC Central Index Key of the issuer, e.g. 1045810."},
            "days": {"type": "integer", "minimum": 1, "maximum": 365,
                     "description": "Look back this many days by filing date. Default 90."},
            "codes": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                      "description": ("Keep only these Form 4 transaction codes, e.g. [\"P\"] for open-market "
                                      "buys, [\"P\", \"S\"] for buys and sells. Default: all.")},
            "max_filings": {"type": "integer", "minimum": 1, "maximum": 40,
                            "description": "Most recent Form 4 filings to read. Default 20."},
            "include_derivatives": {"type": "boolean",
                                    "description": "Include option and other derivative lines. Default true."},
        }, []), anyOf=[{"required": ["symbol"]}, {"required": ["cik"]}]),
        returns=("symbol, cik, issuer_name, window, codes, filings_read, transactions[] (insider, role, date, "
                 "code, code_meaning, shares, price, value, shares_after, rule_10b5_1, filing_url), "
                 "transaction_count, summary, as_of, notes[], source, checked_at."),
        skill="market.insiders", max_seconds=60,
        pricing_basis="Provisional. Provider cost zero (official public API); one filing list plus one small XML per Form 4.",
        requires=("sec_edgar",)),
    Worker(
        name="finance.analytics", price_usd=0.50, tier="standard",
        title="Trading mathematics: returns, risk, drawdown, VaR, beta, indicators, options, Kelly",
        description=(
            'Trading and portfolio math: returns and CAGR, volatility, Sharpe and Sortino, '
            'max drawdown, VaR/CVaR, beta, alpha and correlation vs a benchmark, SMA/EMA, '
            'RSI, Bollinger bands, Black-Scholes option price with Greeks, and the Kelly '
            'fraction, over prices you send or a ticker fetched live. Deterministic, every '
            'formula named, no LLM. Input: prices or symbol; optional metrics, benchmark, '
            'option, kelly.'),
        tags=["trading", "technical-analysis", "risk", "sharpe-ratio", "rsi", "options-pricing", "portfolio"],
        input_schema=_FINANCE_INPUT,
        returns=("source{}, n, n_returns, periods_per_year, metrics_computed[], returns{}, volatility{}, "
                 "sharpe, sortino, drawdown{}, var{}, beta{}, moving_averages{}, rsi{}, bollinger{}, "
                 "black_scholes{}, kelly{}, notes[], method, as_of, checked_at."),
        skill="finance.analytics", max_seconds=60,
        pricing_basis=("Provisional, standard tier. Inline prices cost nothing to serve; a symbol "
                       "adds one or two public market reads."),
        requires=()),
    # --- regional structured data -----------------------------------------
    Worker(
        name="opendata.search", price_usd=0.10, tier="utility",
        title="Search 16 government open-data portals for datasets",
        description=(
            'Government open data search across 16 national portals in one call: Korea '
            '(data.go.kr), Japan, Hong Kong, Australia, UK, Canada, the EU catalog (139,000+ '
            'datasets), Germany, Italy, Ireland, Switzerland, Netherlands, Israel and Chile. '
            'Query in any language; each match returns organization, update time and download'
            ' URLs ready for opendata.table. Portals that did not answer are listed. Input: '
            'query; optional region, limit, portals.'),
        tags=["open-data", "government-data", "datasets", "public-data", "statistics", "data-portal"],
        input_schema=_obj({
            "query": {"type": "string", "minLength": 1, "maxLength": 300,
                      "description": "Words to search for, in any language (인구, 人口, rainfall, Bevölkerung)."},
            "region": {"type": "string", "enum": ["all", "au", "ca", "ch", "cl", "de", "eu", "gb", "hk", "ie", "il", "it", "jp", "kr", "nl"],
                       "description": "Which portals to search. Default all (16 portals, concurrently)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                      "description": "Results per portal, default 10."},
            "portals": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                        "description": "Specific portal ids instead of a region, e.g. [\"data.go.kr\", \"data.bodik.jp\"]."},
        }, ["query"]),
        returns=("query, region, portals_searched[], portals_ok[], portals_failed[], total_matches{}, "
                 "results[{portal, title, description, organization, landing_url, resources[{url, format}]...}], "
                 "result_count, checked_at."),
        skill="opendata.search", max_seconds=60,
        pricing_basis="Provisional. Provider cost zero (public portals); one request per portal.",
        requires=("opendata",)),
    Worker(
        name="opendata.table", price_usd=0.10, tier="utility",
        title="Read a government dataset file as JSON rows",
        description=(
            'Dataset file to JSON rows: a CSV, TSV, XLSX or JSON link from any portal, an '
            'e-Stat (Japan) download or a data.go.kr (Korea) dataset page. Encoding detected '
            '(Korean, Japanese, Chinese, UTF and Western code pages), header row found, '
            'numbers typed, XLSX sheets selectable, large files capped and disclosed. Use it '
            'after opendata.search or on any data file URL. Input: url; optional max_rows, '
            'sheet, encoding, delimiter.'),
        tags=["csv-to-json", "xlsx", "data-parsing", "open-data", "spreadsheet", "dataset"],
        input_schema=_obj({
            "url": {"type": "string", "description": "File link, e-Stat file-download link, or a data.go.kr dataset page."},
            "max_rows": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "Rows to return, default 200."},
            "sheet": {"type": ["string", "integer"], "description": "XLSX sheet name or 0-based index. Default: first sheet."},
            "encoding": {"type": "string", "maxLength": 30, "description": "Force a codec (cp949, shift_jis, big5). Default: detected."},
            "delimiter": {"type": "string", "minLength": 1, "maxLength": 1, "description": "Force a CSV delimiter. Default: sniffed."},
        }, ["url"]),
        returns=("source_url, resolved_from, file_url, final_url, filename, content_type, bytes, format, encoding, "
                 "sheet, sheets[], columns[], column_count, rows[{...}], row_count, total_rows, truncated, notes[], checked_at."),
        skill="opendata.table", max_seconds=90,
        pricing_basis="Provisional. Provider cost zero (one file fetch; one page fetch first for a data.go.kr page).",
        requires=("tabular",)),
    Worker(
        name="search.results", price_usd=0.25, tier="standard",
        title="Web and news results in any language and country (Brave index)",
        description=(
            "Web search API / SERP results: raw web and news results from Brave's independent"
            ' index in any language and country, with title, URL, description, age and extra '
            'snippets (headline, source and age for news). Filter by country, language and '
            'freshness (day, week, month, year). Results, not an answer: pick what to read '
            'next with extract.page. Input: query; optional country, language, count, '
            'freshness, news.'),
        tags=["serp", "web-search-api", "search-results", "news-search", "brave", "multilingual"],
        input_schema=_obj({
            "query": {"type": "string", "minLength": 1, "maxLength": 400, "description": "Words to search for, in any language."},
            "country": {"type": "string", "pattern": "^[A-Za-z]{2}$", "description": "Market, ISO 3166-1 alpha-2 (US, JP, DE). Default: the engine's."},
            "language": _LANGUAGE,
            "count": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Results per block, default 10."},
            "freshness": {"type": "string", "enum": ["day", "week", "month", "year"], "description": "Only results from this window."},
            "news": {"type": "boolean", "description": "Include news results. Default true."},
            "safesearch": {"type": "string", "enum": ["off", "moderate", "strict"], "description": "Default moderate."},
        }, ["query"]),
        returns=("query, country, language, freshness, web[{title, url, description, age, page_age, language, site_name, hostname, "
                 "extra_snippets[]}], web_count, news[{title, url, description, age, page_age, source, breaking}], news_count, "
                 "more_results_available, source, notes[], checked_at."),
        skill="search.results", max_seconds=40,
        pricing_basis="Provisional. Brave Search plan bills $0.005 per request; one request, two when news needs its own.",
        requires=("brave",)),
    # --- news and official numbers, keyless ----------------------------------
    Worker(
        name="news.search", price_usd=0.25, tier="standard",
        title="Current news on any topic, in any language",
        description=(
            'News search API: current news on any topic in any language from the GDELT '
            "Project, which reads the world's press every 15 minutes. Titles matched in their"
            ' original script, newest first, with publisher, language and link; filter to one'
            " language; a US stock ticker searches the company's name. Window up to 30 days "
            '(default 72 hours). Input: query and/or symbol; optional language, limit, '
            'since_hours.'),
        tags=["news", "news-api", "headlines", "current-events", "gdelt", "multilingual", "media-monitoring"],
        input_schema=_obj({
            "query": {"type": "string", "minLength": 1, "maxLength": 300,
                      "description": "Topic words in any language (半導体, 반도체, tarifs douaniers)."},
            "symbol": {"type": "string", "pattern": "^[A-Za-z0-9.\\-=^]{1,12}$",
                       "description": "Optional US ticker (AAPL, MSFT): searches the company's name from the SEC ticker table."},
            "language": _LANGUAGE,
            "region": {"type": "string", "pattern": "^[A-Za-z]{2}$",
                       "description": "Accepted for compatibility; GDELT does not record a publisher's country, so use language."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 20."},
            "since_hours": {"type": "integer", "minimum": 1, "maximum": 720,
                            "description": "Only articles published within this many hours."},
        }, []),
        returns=("query, symbol, language, edition{}, sources_searched[], sources_ok[], sources_failed[], "
                 "articles[{title, url, source_name, source_url, published_at, summary, feed}], article_count, "
                 "limit, since_hours, notes[], checked_at."),
        skill="news.search", max_seconds=45,
        pricing_basis="Provisional. BigQuery scan of GDELT's public table, about 100 MB per day searched (under $0.001).",
        requires=("news",)),
    Worker(
        name="data.macro", price_usd=0.10, tier="utility",
        title="Official economic statistics for a country",
        description=(
            'Economic data / macro indicators for any country, live: GDP, growth, GDP per '
            'capita, inflation (CPI), unemployment, population, interest rates, exchange '
            'rate, debt, trade, FDI or any World Bank indicator code. Annual for 200+ '
            'economies from the World Bank; monthly EU inflation and unemployment from '
            'Eurostat. Returns the series, latest and previous values, the change and the '
            "source's update stamp. Input: indicator, country; optional last, frequency."),
        tags=["economic-data", "macroeconomics", "gdp", "inflation", "unemployment", "world-bank", "statistics"],
        input_schema=_obj({
            "indicator": {"type": "string", "minLength": 3, "maxLength": 40,
                          "description": ("Alias (gdp_usd, gdp_growth, gdp_per_capita_usd, inflation, unemployment, population, "
                                          "population_growth, current_account_gdp, reserves_usd, government_debt_gdp, exports_gdp, "
                                          "imports_gdp, fdi_inflows_usd, exchange_rate_per_usd, real_interest_rate, lending_rate, "
                                          "trade_gdp, internet_users_pct, life_expectancy, co2_per_capita) or a World Bank code (NY.GDP.MKTP.CD).")},
            "country": {"type": "string", "minLength": 2, "maxLength": 9,
                        "description": "ISO 3166 alpha-2 or alpha-3 (JP, KOR, DE), or an EU aggregate (EA20, EU27_2020) for monthly series."},
            "last": {"type": "integer", "minimum": 1, "maximum": 60, "description": "Observations to return, newest last. Default 10."},
            "frequency": {"type": "string", "enum": ["auto", "annual", "monthly"],
                          "description": "auto picks monthly where Eurostat publishes it, else annual."},
        }, ["indicator", "country"]),
        returns=("indicator{alias, code, name, unit}, country{code, name}, frequency, source, observations[{period, value}], "
                 "observation_count, latest{}, previous{}, change{absolute, pct}, as_of, source_url, notes[], checked_at."),
        skill="data.macro", max_seconds=40,
        pricing_basis="Provisional. Provider cost zero (public statistical APIs); one request per call.",
        requires=("macro",)),
    Worker(
        name="email.verify", price_usd=0.02, tier="utility",
        title="Verify an email address before you send",
        description=(
            'Email verification / email validation: will mail to this address arrive? Checked'
            " live at the source: syntax, the domain's DNS (MX, null MX, no such domain) and "
            'its own mail server, asked whether the mailbox exists and whether it accepts any'
            ' address (catch-all). No message sent. Flags disposable domains, role accounts '
            '(info@), free providers and typos (gmial.com). Verdict: deliverable, '
            'undeliverable, risky or unknown, with the reason. Input: email.'),
        tags=["email-verification", "email-validation", "deliverability", "disposable-email", "lead-validation", "smtp"],
        input_schema=_obj({
            "email": {"type": "string", "minLength": 3, "maxLength": 320,
                      "description": "One email address, such as jane@example.com."},
        }, ["email"]),
        returns=("email, normalized, local_part, domain, domain_ascii, verdict, reason, syntax_valid, domain_exists, "
                 "accepts_mail, mx[{host, priority}], mail_provider, mailbox{checked, exists, catch_all, smtp_code, "
                 "smtp_message, mx_host}, disposable, role_account, free_provider, did_you_mean, disposable_list_as_of, "
                 "notes[], checked_at."),
        skill="email.verify", max_seconds=60,
        pricing_basis=("Provisional, utility tier. Provider cost zero: DNS and one SMTP conversation from this node, "
                       "no third-party verification service."),
        requires=("mailcheck",)),
    Worker(
        name="property.context", price_usd=0.25, tier="standard",
        title="Everything the public record says about a US address",
        description=(
            'Real estate data for any US address: FEMA flood and 17 other hazard risk ratings'
            ' with expected annual loss, median home value, rent, income, ownership and '
            'vacancy (Census), price trend (FHFA), fair market rents (HUD), nearby public '
            'schools (NCES), walkability (EPA) and Superfund sites nearby, plus tract, '
            'county, ZIP and districts, all from federal sources in one call. Input: address,'
            ' or lat and lng.'),
        tags=["real-estate", "property-data", "flood-risk", "home-value", "neighborhood", "schools", "census"],
        input_schema=dict(_obj({
            "address": {"type": "string", "minLength": 5, "maxLength": 200,
                        "description": "US street address with city and state or ZIP."},
            "lat": {"type": "number", "minimum": 17, "maximum": 72, "description": "Latitude, instead of an address."},
            "lng": {"type": "number", "minimum": -180, "maximum": -64, "description": "Longitude, instead of an address."},
            "school_radius_km": {"type": "number", "minimum": 0.1, "maximum": 5, "description": "Schools within this radius. Default 1.5."},
            "superfund_radius_km": {"type": "number", "minimum": 0.1, "maximum": 15,
                                    "description": "Superfund sites within this radius. Default 5."},
        }, []), anyOf=[{"required": ["address"]}, {"required": ["lat", "lng"]}]),
        returns=("input{}, matched_address, lat, lng, geography{state, county, tract, zip, metro, congressional_district, "
                 "school_district...}, flood{zone, special_flood_hazard_area...}, hazard_risk{overall_rating, hazards{}, "
                 "notice...}, housing{median_home_value, median_gross_rent...}, price_trend{}, fair_market_rent{zip{}...}, "
                 "schools[], walkability{}, superfund_sites[], sources[], sections_failed[], notes[], checked_at."),
        skill="property.context", max_seconds=60,
        pricing_basis=("Provisional, standard tier. Provider cost zero: public federal services plus two annual tract "
                       "tables shipped with the node."),
        requires=("property_data",)),
    Worker(
        name="travel.flight_status", price_usd=0.25, tier="standard",
        title="Flight and airport status right now",
        description=(
            'Flight status and airport delays, live: by airport code, FAA ground stops and '
            'delay programs with average and max delay (US), live departure and arrival '
            'boards with gates and status (Norway), current METAR and TAF weather (any ICAO '
            "airport) and aircraft overhead; by callsign, the aircraft's live position, "
            'altitude and speed. Input: airport (IATA or ICAO) or callsign; optional flight, '
            'direction, hours_ahead.'),
        tags=["flight-status", "airport-delays", "flight-tracker", "aviation", "metar", "travel"],
        input_schema=dict(_obj({
            "airport": {"type": "string", "minLength": 3, "maxLength": 4, "description": "IATA (OSL, JFK) or ICAO (ENGM, KJFK) code."},
            "callsign": {"type": "string", "minLength": 2, "maxLength": 8,
                         "description": "Callsign as broadcast (SAS1411, UAL123): the aircraft's live position."},
            "flight": {"type": "string", "minLength": 2, "maxLength": 8, "description": "Flight number to pick from the board (SK344)."},
            "direction": {"type": "string", "enum": ["both", "departures", "arrivals"], "description": "Board direction. Default both."},
            "hours_ahead": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Board window ahead, hours. Default 3."},
        }, []), anyOf=[{"required": ["airport"]}, {"required": ["callsign"]}]),
        returns=("query{}, airport{iata, icao, name, city, country, lat, lon}, delays{status, ground_stop, ground_delay, "
                 "closure, arrival_delay, departure_delay, runways, notices}, board{departures[], arrivals[], attribution}, "
                 "weather{metar, taf, flight_category...}, aircraft[], aircraft_scope, coverage{}, attribution[], "
                 "sections_failed[], notes[], checked_at."),
        skill="travel.flight_status", max_seconds=45,
        pricing_basis=("Provisional, standard tier. Provider cost zero: FAA, NOAA and Avinor public feeds and adsb.lol "
                       "(ODbL), shared per their own refresh pace."),
        requires=("aviation",)),
    Worker(
        name="company.enrich", price_usd=0.05, tier="utility",
        title="Company profile from a domain or a name",
        description=(
            'Company enrichment / firmographics by domain or name: legal and common name, '
            'founded, headcount with date, industries, headquarters, parent, CEO and '
            'founders, stock listings, LEI, SEC CIK, registration number, legal form and '
            'status, official social accounts and logo. Sources: Wikidata, the global LEI '
            "register, SEC EDGAR and the company's own site; every field names its source. "
            'Input: domain or name.'),
        tags=["company-enrichment", "firmographics", "company-data", "lead-enrichment", "b2b", "lei", "org-lookup"],
        input_schema=dict(_obj({
            "domain": {"type": "string", "minLength": 4, "maxLength": 253, "description": "Company domain (stripe.com) or its URL."},
            "name": {"type": "string", "minLength": 2, "maxLength": 120, "description": "Company name (Toyota), instead of a domain."},
        }, []), anyOf=[{"required": ["domain"]}, {"required": ["name"]}]),
        returns=("query{}, company{name, legal_name, description, website, domain, founded, employees{count, as_of, source}, "
                 "industries[], headquarters{city, country, country_code, address}, parent, ceo, founders[], logo_url, status, "
                 "legal_form, jurisdiction}, identifiers{wikidata, lei, cik, registration_number, listings[], sec_tickers[], sic...}, "
                 "socials{x, linkedin, facebook, instagram, github, youtube}, sources[], field_sources{}, notes[], checked_at."),
        skill="company.enrich", max_seconds=45,
        pricing_basis="Provisional, utility tier. Provider cost zero: Wikidata, GLEIF and SEC public data, one homepage read.",
        requires=("company_data",)),
    Worker(
        name="sanctions.screen", price_usd=0.05, tier="utility",
        title="Screen a name against US, UK and EU sanctions lists",
        description=(
            'Sanctions screening (AML/KYC): screen a person, company, vessel or aircraft '
            'against the OFAC SDN and consolidated lists, the UK Sanctions List and the EU '
            "financial sanctions list, read from the governments' own files with each list's "
            'date returned. Fuzzy and word-order free: aliases and typos match, a different '
            'surname does not. Each hit: list, programmes, countries, birth dates, UN '
            'reference. Input: name; optional type, country, birth_year.'),
        tags=["sanctions-screening", "aml", "kyc", "ofac", "compliance", "watchlist", "due-diligence"],
        input_schema=_obj({
            "name": {"type": "string", "minLength": 2, "maxLength": 200,
                     "description": "The person, company, vessel or aircraft name, in any word order."},
            "type": {"type": "string", "enum": ["any", "person", "entity", "vessel", "aircraft"],
                     "description": "Only match this kind of listing. Default any."},
            "country": {"type": "string", "maxLength": 60,
                        "description": "Optional country (Iran, Russia): a listing that records only other countries is dropped."},
            "birth_year": {"type": "integer", "minimum": 1880, "maximum": 2030,
                           "description": "Optional, for people: a listing whose recorded birth years are all more than a year away is dropped."},
            "threshold": {"type": "number", "minimum": 0.6, "maximum": 1,
                          "description": "Minimum match score, 0.6-1. Default 0.85."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Most matches returned, 1-50. Default 10."},
            "lists": {"type": "array", "items": {"type": "string", "enum": ["ofac_sdn", "ofac_consolidated", "uk", "eu"]},
                      "description": "Optional subset of lists to screen. Default all four."},
        }, ["name"]),
        returns=("query{}, verdict (potential_match | no_match), match_count, matches[{list, list_name, id, name, matched_name, "
                 "score, type, programs[], countries[], birth_dates[], un_reference, listed_on, source_url}], "
                 "lists[{list, name, publisher, license, source_url, as_of, entries, loaded}], notes[], checked_at."),
        skill="sanctions.screen", max_seconds=180,
        pricing_basis=("Provisional, utility tier. Provider cost zero: the governments' own list files, "
                       "downloaded at most every six hours and matched on this node."),
        requires=("sanctions",)),
    Worker(
        name="phone.parse", price_usd=0.02, tier="utility",
        title="Phone number intelligence: valid, where, which network, what type",
        description=(
            'Phone number validation and lookup: valid or not, country and area, the carrier '
            'the range was issued to, line type (mobile, landline, toll-free, VoIP, premium),'
            " time zones and E.164, international and national formats, from Google's "
            'libphonenumber numbering plans. Nothing is dialled and the owner is never looked'
            ' up. Input: number; optional region for a national-format number.'),
        tags=["phone-validation", "phone-lookup", "carrier-lookup", "e164", "line-type", "kyc"],
        input_schema=_obj({
            "number": {"type": "string", "minLength": 3, "maxLength": 40,
                       "description": "One phone number, international (+44 20 7946 0958) or national with `region`."},
            "region": {"type": "string", "pattern": "^[A-Za-z]{2}$",
                       "description": "Two-letter country code for a national-format number (GB, US)."},
        }, ["number"]),
        returns=("input{}, valid, possible, e164, international, national, country_code, region, country, location, carrier, "
                 "line_type, time_zones[], metadata_version, notes[], checked_at."),
        skill="phone.parse", max_seconds=15,
        pricing_basis="Provisional, utility tier. Provider cost zero: libphonenumber metadata on this node (Apache-2.0).",
        requires=("phone",)),
    Worker(
        name="ip.lookup", price_usd=0.02, tier="utility",
        title="IP address lookup: location, network owner, reverse DNS",
        description=(
            'IP geolocation / IP address lookup: city, region, country and coordinates, the '
            "network's ASN and organisation, reverse DNS, and whether the address is public, "
            'private or reserved. IPv4 and IPv6. Location data from DB-IP Lite (CC BY 4.0, '
            "credited), updated monthly; location is the network's registered place, never a "
            'street address. Input: ip.'),
        tags=["ip-geolocation", "ip-lookup", "asn", "geoip", "fraud-detection", "network"],
        input_schema=_obj({
            "ip": {"type": "string", "minLength": 2, "maxLength": 45, "description": "One IPv4 or IPv6 address, such as 8.8.8.8."},
        }, ["ip"]),
        returns=("ip, version, scope, reverse_dns[], location{city, region, country, country_code, continent, latitude, "
                 "longitude}, network{asn, organization}, database_month, attribution{text, url, license}, notes[], checked_at."),
        skill="ip.lookup", max_seconds=120,
        pricing_basis="Provisional, utility tier. Provider cost zero: DB-IP Lite files on this node plus one DNS query.",
        requires=("iplookup",)),
    Worker(
        name="domain.dns", price_usd=0.05, tier="utility",
        title="Domain DNS, email security and TLS certificate",
        description=(
            'DNS lookup and domain health check: A, AAAA, CNAME, MX, NS, TXT, CAA and SOA '
            'records asked of the source, SPF and DMARC email policy parsed, and the HTTPS '
            'certificate verified like a browser (issuer, names, expiry, days left, TLS '
            'version). Flags missing SPF or DMARC, duplicate SPF and certificates near '
            'expiry. Input: domain (a URL is accepted).'),
        tags=["dns-lookup", "domain", "ssl-certificate", "spf", "dmarc", "email-security", "mx-records"],
        input_schema=_obj({
            "domain": {"type": "string", "minLength": 3, "maxLength": 253, "description": "A domain (github.com) or a URL on it."},
            "tls": {"type": "boolean", "description": "Also check the HTTPS certificate. Default true."},
        }, ["domain"]),
        returns=("domain, domain_ascii, exists, records{a[], aaaa[], cname[], mx[], ns[], txt[], caa[], soa[]}, "
                 "email_security{spf{record, all, multiple}, dmarc{record, policy, subdomain_policy, percent, reports_to}}, "
                 "tls{reachable, valid, error, ip, protocol, subject, issuer, names[], not_before, not_after, days_remaining}, "
                 "notes[], checked_at."),
        skill="domain.dns", max_seconds=45,
        pricing_basis="Provisional, utility tier. Provider cost zero: DNS queries and one TLS handshake from this node.",
        requires=("dnsintel",)),
    Worker(
        name="identity.check", price_usd=0.10, tier="utility",
        title="Identity check: sanctions, email, phone and IP in one call",
        description=(
            'Identity verification / KYC check in one call: a name screened against OFAC, UK '
            "and EU sanctions; an email's mailbox, disposable and role checks; a phone's "
            "validity, country and line type; an IP's country and network. Returns each "
            'result, fraud flags (sanctions hit, disposable email, VoIP, countries that '
            'disagree) and one risk level: low, review or high. Use it before onboarding a '
            'user or counterparty. Input: any of name, email, phone, ip; optional country, '
            'birth_year.'),
        tags=["identity-verification", "kyc", "fraud-detection", "onboarding", "aml", "risk-score", "sanctions"],
        input_schema=dict(_obj({
            "name": {"type": "string", "minLength": 2, "maxLength": 200,
                     "description": "Person or company name to screen against sanctions lists."},
            "email": {"type": "string", "minLength": 3, "maxLength": 320, "description": "Email address to verify."},
            "phone": {"type": "string", "minLength": 3, "maxLength": 40,
                      "description": "Phone number, international (+44 20 7946 0958) or national with `phone_region`."},
            "phone_region": {"type": "string", "pattern": "^[A-Za-z]{2}$",
                             "description": "Two-letter country for a national-format phone number."},
            "ip": {"type": "string", "minLength": 2, "maxLength": 45, "description": "The client's IPv4 or IPv6 address."},
            "country": {"type": "string", "minLength": 2, "maxLength": 60,
                        "description": "Country the person says they are in (US or United States), compared with phone and IP."},
            "birth_year": {"type": "integer", "minimum": 1880, "maximum": 2030,
                           "description": "Birth year, to narrow sanctions matches for a person."},
        }, []), anyOf=[{"required": ["name"]}, {"required": ["email"]}, {"required": ["phone"]}, {"required": ["ip"]}]),
        returns=("input{}, risk (low | review | high), flags[{code, severity, detail}], checks{sanctions, email, phone, "
                 "ip}, checks_run[], checks_failed[], notes[], checked_at."),
        skill="identity.check", max_seconds=90,
        pricing_basis=("Provisional, utility tier: under the four checks bought one by one ($0.11). Provider cost zero: "
                       "government list files, DNS/SMTP, libphonenumber and DB-IP data on this node."),
        composes=["sanctions.screen", "email.verify", "phone.parse", "ip.lookup"],
        requires=("sanctions", "mailcheck", "phone", "iplookup")),
    Worker(
        name="commerce.shipping", price_usd=0.50, tier="standard",
        title="Shipping options, eligibility and cart total for a product",
        description=(
            'Shipping cost and delivery check for any Shopify store: the variant goes into a '
            'real cart session and the store quotes its shipping options to your destination.'
            " Returns options with prices and days, eligibility (or the store's reason, or "
            'the address fields still needed), subtotal and total before tax. No order is '
            'placed. Input: url, ship_to{country, province, postal_code}; optional variant, '
            'quantity.'),
        tags=["shipping", "shipping-cost", "shopify", "ecommerce", "checkout", "delivery"],
        input_schema=_obj({
            "url": {"type": "string", "description": "Product page on the store (…/products/<handle>)."},
            "variant": {"type": "string", "minLength": 1, "maxLength": 200,
                        "description": "Free-text option to buy (size 10 natural black). Default: first available."},
            "quantity": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Default 1."},
            "ship_to": _obj({
                "country": {"type": "string", "minLength": 2, "maxLength": 60, "description": "ISO 3166-1 alpha-2 (US, JP) or the country name."},
                "province": {"type": "string", "minLength": 1, "maxLength": 80, "description": "State / province / prefecture, code or name, when the country needs one."},
                "postal_code": {"type": "string", "minLength": 1, "maxLength": 20},
            }, ["country"]),
        }, ["url", "ship_to"]),
        returns=("url, store{platform, domain, currency}, product{id, title, handle}, variant{id, name, sku, price, available, "
                 "requires_shipping}, quantity, ship_to{}, cart{subtotal, total, currency, item_count, requires_shipping}, eligible, "
                 "eligibility_reasons[], missing_fields[], shipping_options[{name, price, currency, delivery_days_min, "
                 "delivery_days_max, description, carrier}], cheapest_shipping{}, estimated_total{}, taxes_note, source, notes[], checked_at."),
        skill="commerce.shipping", max_seconds=60,
        pricing_basis="Provisional. Provider cost zero (the store's own storefront endpoints); three requests in one session.",
        composes=["commerce.availability"],
        requires=("shopify_cart",)),
    # --- travel: live flight offers (Duffel) --------------------------------
    Worker(
        name="travel.flights", price_usd=0.50, tier="standard",
        title="Live flight offers between two places",
        description=(
            'Flight search / live airfare: offers from the airlines for a route and date with'
            ' price, tax, currency, each segment (carrier, flight number, times, aircraft), '
            'stops, refund and change conditions, emissions and when the offer expires. '
            'Places as IATA codes or names in any language. One-way or return, up to 9 adults'
            ' plus children, any cabin, sorted by price or duration. Input: origin, '
            'destination, departure_date or days_ahead.'),
        tags=["flight-search", "airfare", "flights", "travel", "booking", "airlines"],
        input_schema=_obj({
            "origin": {"type": "string", "minLength": 2, "maxLength": 80,
                       "description": "IATA code (LHR) or a city/airport name in any language."},
            "destination": {"type": "string", "minLength": 2, "maxLength": 80,
                            "description": "IATA code (JFK) or a city/airport name in any language."},
            "departure_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                               "description": "YYYY-MM-DD, today or later. Or give days_ahead."},
            "days_ahead": {"type": "integer", "minimum": 0, "maximum": 365,
                           "description": "Departure this many days from today (UTC), instead of departure_date."},
            "return_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                            "description": "Optional return leg, YYYY-MM-DD."},
            "adults": {"type": "integer", "minimum": 1, "maximum": 9, "description": "Default 1."},
            "children_ages": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 17},
                              "maxItems": 8, "description": "One age per child; under 2 travel as infants."},
            "cabin_class": {"type": "string", "enum": ["economy", "premium_economy", "business", "first"],
                            "description": "Default economy."},
            "max_connections": {"type": "integer", "minimum": 0, "maximum": 2,
                                "description": "0 for non-stop only. Default: any."},
            "max_offers": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 10."},
            "sort": {"type": "string", "enum": ["price", "duration"], "description": "Default price."},
        }, ["origin", "destination"]),
        returns=("origin{}, destination{}, departure_date, return_date, passengers{}, cabin_class, sort, "
                 "offers[{id, airline{}, total_amount, base_amount, tax_amount, currency, expires_at, "
                 "slices[{segments[]}], stops, total_duration_seconds, refund_before_departure{}, "
                 "change_before_departure{}, emissions_kg, instant_payment_required, ...}], offer_count, "
                 "offers_available, cheapest_total{}, fastest_duration_seconds, live_mode, source, notes[], checked_at."),
        skill="travel.flights", max_seconds=90,
        pricing_basis=("Provisional. Provider search is free within Duffel's search-to-book ratio; "
                       "one place lookup per named place plus one offer request."),
        requires=("duffel",)),
    Worker(
        name="travel.hotels", price_usd=0.50, tier="standard",
        title="Live hotel availability and room rates",
        description=(
            'Hotel search / live room rates for a place and dates across 2M+ properties: '
            'stars, rating, address, cheapest bookable rate and room options with board, '
            'occupancy, total, taxes and fees, refundability, cancellation deadlines and the '
            'offer id to book. Place in any language, city + country, hotel name or '
            'coordinates. Input: place, city+country_code, hotel_name or latitude+longitude; '
            'optional check_in, nights, adults, currency.'),
        tags=["hotel-search", "hotels", "room-rates", "travel", "booking", "accommodation"],
        input_schema=_obj({
            "place": {"type": "string", "minLength": 2, "maxLength": 200,
                      "description": "Free-text place in any language (hotel near Shibuya station Tokyo, 명동 서울)."},
            "city": {"type": "string", "minLength": 2, "maxLength": 80, "description": "English city name; needs country_code."},
            "country_code": {"type": "string", "pattern": "^[A-Za-z]{2}$", "description": "ISO 3166-1 alpha-2."},
            "hotel_name": {"type": "string", "minLength": 2, "maxLength": 120, "description": "A hotel or chain name."},
            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "longitude": {"type": "number", "minimum": -180, "maximum": 180},
            "radius_m": {"type": "integer", "minimum": 100, "maximum": 50000, "description": "With coordinates. Default 5000."},
            "check_in": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "description": "YYYY-MM-DD; or give days_ahead."},
            "days_ahead": {"type": "integer", "minimum": 0, "maximum": 365, "description": "Check-in this many days from today. Default 30."},
            "check_out": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "description": "YYYY-MM-DD; or give nights."},
            "nights": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Default 2."},
            "adults": {"type": "integer", "minimum": 1, "maximum": 8, "description": "Default 2."},
            "children_ages": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 17}, "maxItems": 6},
            "currency": {"type": "string", "pattern": "^[A-Za-z]{3}$", "description": "ISO 4217, default USD."},
            "guest_nationality": {"type": "string", "pattern": "^[A-Za-z]{2}$", "description": "Default US; some rates depend on it."},
            "max_hotels": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Default 10."},
            "sort": {"type": "string", "enum": ["price", "rating"], "description": "Default price."},
        }, []),
        returns=("query{}, check_in, check_out, nights, guests{}, currency, guest_nationality, sort, hotels[{id, name, stars, "
                 "rating, review_count, address, city, country_code, latitude, longitude, cheapest{}, rooms[{name, board, "
                 "max_occupancy, total, currency, taxes_and_fees[], refundable, cancel_free_until, cancellation[], offer_id, "
                 "rate_id}], room_options_available}], hotel_count, hotels_matched, hotels_without_rates, cheapest_total{}, "
                 "live_mode, source, notes[], checked_at."),
        skill="travel.hotels", max_seconds=90,
        pricing_basis="Provisional. Provider search and rates are free (LiteAPI bills bookings only); two provider calls.",
        requires=("liteapi",)),
    # --- traffic-aware routing (Google Routes API) ---------------------------
    Worker(
        name="traffic.route", price_usd=0.10, tier="utility",
        title="Traffic-aware travel time and distance between two places",
        description=(
            'Travel time with live traffic between two places, from the Google Routes API at '
            'call time: duration with traffic, duration without, the delay, distance, route '
            "description, warnings and advisories. Places as addresses, names or 'lat,lng'. "
            'Use it for ETAs, delivery windows and trip planning. Input: origin, destination;'
            ' optional travel_mode, departure_time, traffic.'),
        tags=["traffic", "eta", "travel-time", "routing", "commute", "logistics"],
        input_schema={
            "type": "object",
            "properties": {
                "origin": {"type": "string", "minLength": 1, "maxLength": 300,
                           "description": "Where the trip starts: an address, a place name, or \"lat,lng\"."},
                "destination": {"type": "string", "minLength": 1, "maxLength": 300,
                                "description": "Where the trip ends: an address, a place name, or \"lat,lng\"."},
                "travel_mode": {"type": "string", "enum": ["DRIVE", "TWO_WHEELER", "WALK", "BICYCLE", "TRANSIT"],
                                "description": "Default DRIVE. Traffic applies to DRIVE and TWO_WHEELER only."},
                "departure_time": {"type": "string", "format": "date-time",
                                   "description": "RFC 3339 departure time in the future (2026-09-27T16:30:00Z); omit to leave now."},
                "traffic": {"type": "string", "enum": ["aware", "optimal", "none"],
                            "description": "aware (default): live traffic; optimal: live traffic with the best route quality, slower; none: no traffic."},
            },
            "required": ["origin", "destination"],
            "additionalProperties": False,
        },
        returns=("origin, destination, travel_mode, traffic, departure_time, distance_meters, "
                 "duration_seconds, static_duration_seconds, delay_seconds, duration_text, "
                 "static_duration_text, distance_text, description, warnings[], advisory, "
                 "source, as_of, checked_at."),
        skill="traffic.route", max_seconds=60,
        pricing_basis=("Provisional. One Google Routes computeRoutes request per call, billed by "
                       "Google per request; the per-call cost is not yet measured here."),
        requires=("google_routes",)),
    Worker(
        name="video.youtube", price_usd=0.05, tier="utility",
        title="YouTube search and video statistics, live",
        description=(
            'YouTube search and video stats, live: search YouTube or look up videos by id, '
            'each with title, channel, upload time, duration, views, likes, comments, live '
            'flag and thumbnail from the YouTube Data API at call time. Sort by relevance, '
            'date, views or rating; filter by upload date, country and language. Input: query'
            ' or video_ids; optional max_results, order, published_after, region_code.'),
        tags=["youtube", "video-search", "video-stats", "views", "social-media", "creators"],
        input_schema=dict(_obj({
            "query": {"type": "string", "minLength": 1, "maxLength": 300,
                      "description": "Words to search YouTube for, in any language. Use this OR `video_ids`."},
            "video_ids": {"type": "array", "items": {"type": "string", "pattern": "^[A-Za-z0-9_-]{11}$"},
                          "minItems": 1, "maxItems": 25,
                          "description": "YouTube video ids (11 characters) to look up directly; skips the search."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 25,
                            "description": "With query: videos to return, default 10."},
            "order": {"type": "string", "enum": ["relevance", "date", "viewCount", "rating", "title"],
                      "description": "With query: YouTube's sort, default relevance."},
            "published_after": {"type": "string", "format": "date-time",
                                "description": "With query: only videos uploaded at or after this RFC 3339 time, e.g. 2026-01-01T00:00:00Z."},
            "region_code": {"type": "string", "minLength": 2, "maxLength": 2,
                            "description": "With query: ISO 3166-1 alpha-2 country whose results to prefer, e.g. US, JP."},
            "language": {"type": "string", "pattern": "^[A-Za-z]{2,3}$",
                         "description": "With query: ISO 639 language code of the results to prefer, e.g. en, ja."},
        }, []), oneOf=[{"required": ["query"]}, {"required": ["video_ids"]}]),
        returns=("query, video_ids, total_results, items[{video_id, url, title, description, channel_id, "
                 "channel_title, published_at, duration_seconds, view_count, like_count, comment_count, live, "
                 "thumbnail_url}], item_count, source, as_of, checked_at."),
        skill="video.youtube", max_seconds=60,
        pricing_basis=("Provisional. No monetary provider cost: the YouTube Data API is quota-metered "
                       "(100 units per search, 1 per details call, of a 10,000-unit daily default); two "
                       "requests at most."),
        requires=("youtube",)),
    # --- social platforms, keyless -------------------------------------------
    Worker(
        name="social.bluesky", price_usd=0.05, tier="utility",
        title="Bluesky profile, posts, account search or thread, live",
        description=(
            'Bluesky data, live and keyless: a profile with its latest posts and engagement '
            '(likes, reposts, replies, quotes), an account search, or a post thread with its '
            'replies, from the public AppView. Use it for social listening and influencer '
            'checks on Bluesky. Input: mode (profile, search_actors, thread) with actor, '
            'query or uri; optional posts, limit, depth.'),
        tags=["bluesky", "social-media", "social-listening", "posts", "profiles", "atproto"],
        input_schema=_obj({
            "mode": {"type": "string", "enum": ["profile", "search_actors", "thread"],
                     "description": "profile: an account and its latest posts; search_actors: accounts matching a query; thread: a post and its replies."},
            "actor": {"type": "string", "maxLength": 253, "description": "Handle (bsky.app) or DID (did:plc:...); profile mode."},
            "posts": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Latest posts to return in profile mode, default 10."},
            "query": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Words to match accounts on; search_actors mode."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "description": "Accounts to return in search_actors mode, default 10."},
            "uri": {"type": "string", "description": "Post AT-URI, at://did:plc:.../app.bsky.feed.post/<rkey>; thread mode."},
            "depth": {"type": "integer", "minimum": 1, "maximum": 6, "description": "Reply depth to flatten in thread mode, default 2."},
        }, ["mode"]),
        returns=("mode, actor, query, uri, profile{}, posts[], actors[], thread{}, post_count, "
                 "actor_count, source, as_of, checked_at."),
        skill="social.bluesky", max_seconds=45,
        pricing_basis="Provisional. Provider cost zero (public AppView); two requests at most.",
        requires=("bluesky",)),
    Worker(
        name="social.mastodon", price_usd=0.05, tier="utility",
        title="Mastodon hashtag timeline, account, search or trends, live",
        description=(
            'Mastodon data, live and keyless (any public instance): a hashtag timeline, an '
            'account with its latest posts, account or hashtag search, or trending hashtags, '
            'each post with text, counts, media and tags. Use it for social listening and '
            'trend tracking in the fediverse. Input: mode (hashtag, account, search, trends) '
            'with tag, acct or query; optional instance, limit.'),
        tags=["mastodon", "fediverse", "social-media", "trending", "hashtags", "social-listening"],
        input_schema=_obj({
            "mode": {"type": "string", "enum": ["hashtag", "account", "search", "trends"],
                     "description": "hashtag: latest statuses under a tag; account: an account and its statuses; search: accounts or hashtags matching a query; trends: trending hashtags."},
            "instance": {"type": "string", "maxLength": 253, "description": "Mastodon server hostname, default mastodon.social."},
            "tag": {"type": "string", "minLength": 1, "maxLength": 100, "description": "Hashtag without #; hashtag mode."},
            "acct": {"type": "string", "minLength": 1, "maxLength": 320, "description": "Username, optionally @domain (Gargron or Gargron@mastodon.social); account mode."},
            "statuses": {"type": "integer", "minimum": 1, "maximum": 40, "description": "Latest statuses to return in account mode, default 10."},
            "query": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Search words; search mode."},
            "kind": {"type": "string", "enum": ["accounts", "hashtags"], "description": "What to search for, default accounts."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 40, "description": "Items to return (hashtag up to 40, search up to 25, trends up to 20), default 10."},
        }, ["mode"]),
        returns=("mode, instance, tag, acct, query, kind, account{}, statuses[], accounts[], hashtags[], "
                 "status_count, source, as_of, checked_at."),
        skill="social.mastodon", max_seconds=45,
        pricing_basis="Provisional. Provider cost zero (public instance API); two requests at most.",
        requires=("mastodon",)),
    Worker(
        name="social.x_pulse", price_usd=0.50, tier="standard",
        title="X (Twitter) topic pulse: volume and engagement as aggregates",
        description=(
            'Twitter / X sentiment and volume for any topic, as numbers, live: post volume '
            'per hour or day over the last 1-7 days, and over a sample of recent posts the '
            'engagement totals and rate (likes, reposts, replies, quotes, impressions), '
            'language mix, top hashtags and link/media share. Aggregates only: no post text '
            'or authors. Input: query (X operators allowed); optional days, granularity, '
            'sample, lang.'),
        tags=["twitter", "x", "social-listening", "trend-analysis", "engagement", "buzz", "social-media"],
        input_schema=_obj({
            "query": {"type": "string", "minLength": 1, "maxLength": 400,
                      "description": "Words or X search operators (\"machine learning\" from:nasa has:links)."},
            "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "Window ending now, default 7 (X's recent limit)."},
            "granularity": {"type": "string", "enum": ["hour", "day"], "description": "Volume buckets, default day."},
            "sample": {"type": "integer", "minimum": 0, "maximum": 100,
                       "description": "Recent posts read for the engagement aggregates, default 40; 0 = volume only."},
            "lang": {"type": "string", "minLength": 2, "maxLength": 2, "description": "Restrict to a language (en, ja...)."},
            "include_retweets": {"type": "boolean", "description": "Default false: reposts are excluded from volume and sample."},
        }, ["query"]),
        returns=("query, effective_query, window{}, granularity, volume{total, buckets[]}, sample{requested, read, "
                 "pages, x_cost_usd}, engagement{likes_total, ..., languages{}, top_hashtags[]}, source, as_of, checked_at."),
        skill="social.x_pulse", max_seconds=60,
        pricing_basis=("Provisional, standard tier. X bills $0.005 per post read (the sample, max $0.50) and "
                       "nothing for recent counts; measured per call."),
        requires=("x_api",)),
    Worker(
        name="security.mcp_inspect", price_usd=5.00, tier="advanced",
        title="Inspect an MCP endpoint",
        description=(
            "MCP server security scan: probe any MCP endpoint's initialize handshake and "
            'tools/list and report whether it requires authentication, which protocol version'
            ' it speaks, how many tools it exposes and which are not marked read-only. Use it'
            ' before connecting an agent to an unknown MCP server. Input: url of the MCP '
            'endpoint.'),
        tags=["mcp", "security", "audit", "agent-safety", "scanner", "tools"],
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
            'HTTP fetch / GET any URL: exactly what the server sent, status code, headers and'
            ' body, plus the final URL after redirects. Every status is a result, so a 404 or'
            ' 500 from the target is delivered, not failed. Use it for API checks, uptime '
            'probes and raw HTML. Input: url. Readable text from a rendered page: '
            'extract.page.'),
        tags=["http", "fetch", "curl", "headers", "uptime", "raw-html", "proxy"],
        input_schema=_URL,
        returns="url, final_url, status, content_type, bytes, text, truncated, headers{}.",
        skill="fetch.raw", max_seconds=60,
        pricing_basis="Provisional. Provider cost zero (public HTTP).",
        requires=("web",)),
    Worker(
        name="chain.rpc", price_usd=0.05, tier="utility",
        title="Base RPC passthrough",
        description=(
            'Base RPC endpoint, pay per call: any allowlisted read-only JSON-RPC method on '
            'Base mainnet (eth_call, eth_getBalance, eth_getLogs, eth_getBlockByNumber, '
            'eth_getTransactionReceipt and more) with your own params, no API key. Revert '
            'reasons come back as the result. Input: method; optional params.'),
        tags=["rpc", "base", "json-rpc", "eth_call", "onchain", "web3", "node"],
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
            "Exchange rates / currency conversion table: Coinbase's full rate table for one "
            'base currency against every crypto and fiat currency it quotes (USD to BTC, ETH,'
            ' EUR, GBP, JPY...). Use it to convert amounts between currencies. Input: '
            'currency code such as USD.'),
        tags=["exchange-rates", "currency-conversion", "forex", "crypto", "fiat", "rates"],
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
            'Crypto ticker / bid and ask: live best bid, best ask, last price and volume for '
            "one Coinbase product from Coinbase's Exchange data host, an independent second "
            'source to market.quote. Input: product_id such as BTC-USD.'),
        tags=["crypto-ticker", "bid-ask", "order-book", "bitcoin", "market-data", "live"],
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
            "One prediction market by slug: a Polymarket market's question and the current "
            'probability of each outcome as a percentage. Use it to track a market you '
            'already know. Input: slug. Search markets by topic: market.prediction.'),
        tags=["prediction-market", "polymarket", "odds", "probability", "market-lookup"],
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
            'Top prediction market events: Polymarket events (groups of related markets) '
            'ranked by trading volume, with title, slug, end date and market count. Use it to'
            ' see what the biggest prediction markets are right now. Input: optional limit.'),
        tags=["prediction-market", "polymarket", "events", "trending", "odds"],
        input_schema=_obj({"limit": {"type": "integer", "description": "1-50, default 10."}}, []),
        returns="events[{id,title,slug,volume,end_date,market_count}], count.",
        skill="prediction.events", max_seconds=20,
        pricing_basis="Provisional. Provider cost zero (public API).",
        requires=("polymarket",)),
    # --- wave 2b: fail-closed until the operator enables one Google product -
    Worker(
        name="maps.places", price_usd=0.10, tier="utility",
        title="Find places near a location",
        description=(
            "Places near me / local search anywhere: 'coffee near the Ferry Building, San "
            "Francisco', 'ramen near Shibuya Station', 'pharmacy' at coordinates. From "
            'Overture Maps open places data (80M+ places): name, category, distance, address,'
            ' website, phone, brand, open status and confidence, nearest first; the area '
            'widens when nothing is close. Input: query; optional near, lat and lng, '
            'radius_m, limit.'),
        tags=["places", "local-search", "nearby", "poi", "restaurants", "maps", "business-listings"],
        input_schema=_obj({
            "query": {"type": "string", "minLength": 1, "maxLength": 300,
                      "description": "What and where: 'coffee near the Ferry Building, San Francisco'."},
            "near": {"type": "string", "maxLength": 200, "description": "Where, if not in the query: a place, US address or airport code."},
            "lat": {"type": "number", "minimum": -90, "maximum": 90, "description": "Latitude of the center."},
            "lng": {"type": "number", "minimum": -180, "maximum": 180, "description": "Longitude of the center."},
            "radius_m": {"type": "number", "minimum": 50, "maximum": 10000, "description": "Search radius in metres. Default 1000."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Most places to return. Default 10."},
            "region_code": {"type": "string", "description": "Accepted for compatibility; the location decides the area."},
        }, ["query"]),
        returns=("query, what, near{name, lat, lon, source}, radius_m, places[{id, name, category, distance_m, lat, lon, "
                 "address, country, website, phone, brand, operating_status, confidence, sources[]}], place_count, "
                 "attribution[], notes[], checked_at."),
        skill="maps.places", max_seconds=40,
        pricing_basis="Provisional. BigQuery read of Overture's public places table, about 10 MB billed per search.",
        requires=("places",)),
    Worker(
        name="maps.route", price_usd=0.10, tier="utility",
        title="Compute a route",
        description=(
            'Directions between two places: route and travel time by car, walking, bicycle or'
            ' transit through Google Maps Grounding Lite. Use it for trip planning and ETAs. '
            'Input: origin, destination; optional travel_mode (DRIVE, WALK, BICYCLE, TRANSIT;'
            ' default DRIVE).'),
        tags=["directions", "routing", "maps", "travel-time", "navigation"],
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
        title="Weather at a location, now and for seven days",
        description=(
            'Weather forecast anywhere on Earth: current conditions, the next 24 hours and a '
            'seven-day forecast (temperature, precipitation, wind, humidity, cloud, pressure,'
            " condition) from MET Norway's forecast model, plus active US National Weather "
            'Service alerts for US locations. Input: location as a place name, US street '
            'address or airport code, or lat and lng.'),
        tags=["weather", "forecast", "weather-api", "temperature", "rain", "alerts"],
        input_schema=dict(_obj({
            "location": {"type": "string", "minLength": 2, "maxLength": 200,
                         "description": "Place name (Kyoto), US street address, or airport code (JFK)."},
            "lat": {"type": "number", "minimum": -90, "maximum": 90, "description": "Latitude, instead of a location."},
            "lng": {"type": "number", "minimum": -180, "maximum": 180, "description": "Longitude, instead of a location."},
        }, []), anyOf=[{"required": ["location"]}, {"required": ["lat", "lng"]}]),
        returns=("location, place{name, lat, lon, country, source}, current{time, temperature_c, wind_speed_ms, wind_direction_deg, "
                 "humidity_pct, cloud_cover_pct, pressure_hpa, condition, precipitation_mm}, hourly[24], daily[7]{date, min_c, "
                 "max_c, precipitation_mm, condition}, alerts[], alerts_covered, units{}, forecast_updated_at, attribution[], "
                 "sections_failed[], notes[], checked_at."),
        skill="maps.weather", max_seconds=40,
        pricing_basis="Provisional. Provider cost zero: MET Norway and NWS public services, shared until their own Expires.",
        requires=("weather",)),
    Worker(
        name="video.generate", price_usd=10.00, tier="premium",
        title="Generate a video",
        description=(
            'AI video generation / text to video: a short clip from a text prompt with Google'
            ' Veo, returned as a download link (video_url, valid 48 hours) with its MIME '
            'type, optionally with generated audio. Input: prompt; optional aspect_ratio '
            '(16:9 or 9:16), duration_seconds (4, 6 or 8), generate_audio, inline.'),
        tags=["video-generation", "text-to-video", "veo", "ai-video", "clips"],
        input_schema=_obj({
            "prompt": {"type": "string"},
            "aspect_ratio": {"type": "string", "description": "16:9 or 9:16. Default 16:9."},
            "duration_seconds": {"type": "integer", "description": "4, 6 or 8. Default 6."},
            "generate_audio": {"type": "boolean", "description": "Default false."},
            "inline": {"type": "boolean", "description": "Also return the bytes as video_base64. Default false."},
        }, ["prompt"]),
        returns="prompt, aspect_ratio, duration_seconds, video_url (48 h), video_base64 when inline, mime_type, model.",
        skill="video.generate", max_seconds=MAX_WORKER_SECONDS,
        pricing_basis="Provisional. Flat per-second rate once measured; one attempt only (a retry would re-bill the vendor).",
        requires=("veo",)),
]

BY_PATH = {worker.path: worker for worker in CATALOG}
# No language barrier: every worker takes an optional BCP-47 `language`.
# The prose-producing workers below honour it natively (their answer is
# written in that language); every other worker's human-readable strings
# (notes, details, reasons...) are translated on the job by
# skills/localize.py. Both sets advertise the same property.
NATIVE_LANGUAGE_WORKERS = frozenset({
    "llm.generate", "llm.analyze", "llm.extract", "search.web", "research.brief", "research.page_facts",
    "research.web", "research.company", "verify.claims", "monitor.check", "market.intel", "data.question",
    "commerce.availability",
    # news.search's `language` picks the publishers' edition, so its articles already arrive in that language.
    "news.search",
    # search.results' `language` is the search language: results come back in it.
    "search.results",
})
_TRANSLATED_LANGUAGE = {
    "type": "string", "maxLength": 35,
    "pattern": "^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$",
    "description": ("Optional BCP-47 language tag (en, ja, ko, pt-BR): the human-readable strings "
                    "of the result (notes, details, reasons) are translated into this language; "
                    "data, codes, numbers and URLs are untouched. Default: as produced."),
}
LOCALIZED_WORKERS = frozenset(w.name for w in CATALOG if w.name not in NATIVE_LANGUAGE_WORKERS)
for _w in CATALOG:
    _props = _w.input_schema.setdefault("properties", {})
    if "language" not in _props:
        _props["language"] = dict(_TRANSLATED_LANGUAGE)

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
    "symbol": "AAPL",
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
    "travel.flights": {"origin": "LHR", "destination": "JFK", "days_ahead": 30, "max_offers": 3},
    "travel.hotels": {"place": "hotel near Shibuya station Tokyo", "days_ahead": 30, "nights": 2, "max_hotels": 3},
    "news.search": {"query": "半導体", "language": "ja", "limit": 5},
    "search.results": {"query": "東京 天気予報", "country": "JP", "language": "ja", "count": 5},
    "data.macro": {"indicator": "inflation", "country": "JP", "last": 5},
    # A role address on a domain that publishes MX records, so the example
    # exercises DNS, SMTP and the role flag without naming a person.
    "email.verify": {"email": "support@github.com"},
    # The Census Bureau's own address, so the example geocodes on the first try.
    "property.context": {"address": "4600 Silver Hill Rd, Washington, DC 20233"},
    # Oslo: a live Avinor board, a METAR and aircraft overhead in one example.
    "travel.flight_status": {"airport": "OSL", "hours_ahead": 2},
    "company.enrich": {"domain": "stripe.com"},
    # A listed company, so the example always returns a match with its lists.
    "sanctions.screen": {"name": "Rosneft", "type": "entity"},
    # Google's published London number and public resolver: no private person.
    "phone.parse": {"number": "+44 20 7031 3000"},
    "ip.lookup": {"ip": "8.8.8.8"},
    "domain.dns": {"domain": "github.com"},
    "identity.check": {"name": "Jane Smith", "email": "jane@stripe.com", "phone": "+44 20 7031 3000",
                       "ip": "8.8.8.8", "country": "GB"},
    "traffic.route": {"origin": "Ferry Building, San Francisco, CA", "destination": "Oakland City Hall, Oakland, CA", "travel_mode": "DRIVE", "traffic": "aware"},
    "video.youtube": {"query": "open source licensing", "max_results": 3},
    "social.bluesky": {"mode": "profile", "actor": "bsky.app", "posts": 3},
    "social.x_pulse": {"query": "open source", "days": 3, "sample": 20, "lang": "en"},
    "social.mastodon": {"mode": "hashtag", "tag": "opensource", "limit": 3},
    "opendata.search": {"query": "인구", "region": "kr", "limit": 5},
    "opendata.table": {"url": "https://www.data.go.kr/data/15005995/fileData.do", "max_rows": 5},
    "market.stock": {"symbol": "AAPL", "range": "1mo"},
    # Either symbol or cik is valid, so nothing is `required`.
    "market.fundamentals": {"symbol": "AAPL", "periods": 4, "forms": ["10-K", "10-Q"]},
    "market.insiders": {"symbol": "NVDA", "days": 90, "codes": ["P", "S"]},
    # Inline prices with an option and a bet, so the example computes offline.
    "finance.analytics": {
        "prices": [100, 101.5, 99.8, 102.2, 103.9, 103.1, 105.4, 104.2, 106.8, 108.0, 107.1,
                   109.5, 111.2, 110.4, 112.9, 114.3, 113.0, 115.8, 117.1, 116.2, 118.6, 120.0],
        "risk_free_rate": 0.04, "windows": {"sma": [5, 10], "ema": [5], "rsi": 14, "bollinger": 10},
        "option": {"type": "call", "strike": 120, "rate": 0.04, "time_to_expiry_years": 0.5},
        "kelly": {"win_probability": 0.55, "win_loss_ratio": 1.5}},
    # A live Shopify product page that publishes JSON-LD variants, so the
    # example runs and returns per-size availability.
    "commerce.availability": {"url": "https://www.allbirds.com/products/mens-wool-runners",
                              "variant": "size 10"},
    "commerce.shipping": {"url": "https://www.allbirds.com/products/mens-wool-runners", "variant": "size 10",
                          "ship_to": {"country": "US", "province": "NY", "postal_code": "10001"}},
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
    "maps.weather": {"location": "Kyoto"},
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
