"""The /work/* routes.

THE ONE THING THIS FILE IS FOR: reusing the existing payment gate rather than
building a second one. A worker route does exactly what an audit route does,
in the same order, through the same functions --

    authorize + rate limit  (refuses before any payment instrument is touched)
    run the job
    _bill                   (settles x402 only after a result exists)
    _deliver                (receipt headers, refused-settle withholding)
    failure -> _failed_audit_response  (502, nothing charged)

-- and those four are INJECTED from app.main at startup. This module never
imports x402_payments, never constructs a challenge, and has no idea how
settlement works. There is one payment implementation in this service and it
is the one that already takes real money.

TWO DELIBERATE DIFFERENCES FROM THE AUDIT ROUTES

1. These handlers are `async def`. The audit routes are sync, so FastAPI runs
   them in anyio's threadpool, which app.main caps at MAX_CONCURRENT_AUDITS
   (2 on the production box) because each audit holds a Chromium context. A
   slow worker sharing that pool would starve the paid audits. So workers run
   on the event loop, the blocking payment gate goes to a dedicated executor,
   and worker concurrency is bounded by its own semaphore.

2. They accept an Idempotency-Key. See ledger.claim_idempotency: it closes
   the duplicate-charge hole the x402 nonce guard cannot see, which is a
   caller that timed out and re-signed.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from . import catalog, ledger, media_store, runtime
from .context import JobContext
from .providers import health as provider_health
from .skills import PRECHECKS, REGISTRY, localize
from .skills.llm import validate_language

log = logging.getLogger("hubvibe.workers.router")

router = APIRouter()

# Injected by app.main at import time. Nothing here works until configure()
# runs, which is deliberate: a worker that could serve a request without the
# payment gate would be a free capability accidentally exposed.
_authorize: Optional[Callable] = None
_bill: Optional[Callable] = None
_deliver: Optional[Callable] = None
_failed: Optional[Callable] = None
# What the core does for a paid request that is answered WITHOUT new work (a
# replayed Idempotency-Key, a duplicate of a running request): hand back
# whatever the gate took. And, for a job handed back to collect later, the
# three halves of its prepaid hold (see billing.hold_prepaid). All optional:
# a core that injects none simply has no rail that takes money at the gate.
_not_charged: Optional[Callable] = None
_hold_payment: Optional[Callable] = None
_close_hold: Optional[Callable] = None
_refund_hold: Optional[Callable] = None
# Puts on a body the prepaid key a top-up bought with this very request.
_attach_key: Optional[Callable] = None
_configured = False
# The node's SERVICE_VERSION, stamped on every ledger row so a receipt names
# the build that ran the job. Injected because this module cannot import
# app.main.
_node_version: Optional[str] = None
# For a job paid on the MPP `evm` hash rail: a callable mapping the
# credential to the on-chain facts its verification recorded (payer, amount,
# asset, network, tx). Injected like the gate itself; None means the ledger
# simply has no payment facts for MPP-paid jobs.
_mpp_payment_facts: Optional[Callable] = None

MAX_CONCURRENT_WORKERS = int(os.environ.get("MAX_CONCURRENT_WORKERS", "8"))
_semaphore: Optional[asyncio.Semaphore] = None

# Separate from anyio's threadpool ON PURPOSE (see module docstring): this one
# exists so the blocking payment gate and the blocking browser never consume
# the slots the audit routes need.
_executor: Optional[ThreadPoolExecutor] = None


def configure(authorize_and_rate_limit, bill, deliver, failed_response,
              with_page=None, goto_guarded=None, blocked_target_reason=None,
              node_version=None, mpp_payment_facts=None, not_charged=None,
              hold_payment=None, close_hold=None, refund_hold=None,
              attach_key=None) -> None:
    """Hand the worker network the core's payment gate and browser pool."""
    global _authorize, _bill, _deliver, _failed, _executor, _semaphore, _configured
    global _node_version, _mpp_payment_facts
    global _not_charged, _hold_payment, _close_hold, _refund_hold, _attach_key
    _attach_key = attach_key
    _not_charged = not_charged
    _hold_payment = hold_payment
    _close_hold = close_hold
    _refund_hold = refund_hold
    _node_version = node_version
    _mpp_payment_facts = mpp_payment_facts
    _authorize = authorize_and_rate_limit
    _bill = bill
    _deliver = deliver
    _failed = failed_response
    _executor = ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT_WORKERS + 2, thread_name_prefix="hubvibe-worker")
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT_WORKERS)

    from .providers import google_auth, web

    web.configure(with_page=with_page, executor=_executor, goto_guarded=goto_guarded,
                  blocked_target_reason=blocked_target_reason)
    # Resolve Google credentials in the background now, so the one-off cost
    # (and its timeout, if the metadata probe is wedged) lands at startup
    # rather than inside the first agent's paid request.
    google_auth.prime()
    _configured = True
    log.info("worker network configured: %d workers, concurrency %d",
             len(catalog.CATALOG), MAX_CONCURRENT_WORKERS)


def is_configured() -> bool:
    return _configured


def _error_status(reason: str) -> int:
    """HTTP status for a failure reason.

    502 for "the provider failed us", 400 for "your request was wrong", 503
    for "this capability is not configured here". The distinction matters to
    an autonomous caller deciding whether to retry, switch endpoint, or fix
    its own body.
    """
    if reason == runtime.InvalidRequest.reason:
        return 400
    if reason == runtime.ProviderUnavailable.reason:
        return 503
    if reason == runtime.DeadlineExceeded.reason:
        return 504
    if reason == runtime.StillComputing.reason:
        return 503
    return 502


def _mpp_facts(auth) -> Optional[dict]:
    """On-chain facts of an MPP-paid call, from the injected lookup. None
    for x402 (which carries its own pending payment) and for anything else."""
    if _mpp_payment_facts is None or getattr(auth, "payment_method", None) != "mpp":
        return None
    credential = getattr(auth, "mpp_credential", None)
    if not credential:
        return None
    try:
        return _mpp_payment_facts(credential) or None
    except Exception:
        return None


def _rail_of(auth) -> Optional[str]:
    if getattr(auth, "pending_payment", None) is not None:
        return "x402"
    if getattr(auth, "payment_method", None) == "mpp":
        return "mpp"
    return None


def _payer_of(auth) -> Optional[str]:
    """Best-effort payer address, for the repeat-customer signal in the ledger.

    Wrapped in a broad except on purpose: the payload shape belongs to the
    x402 library, and an analytics field is never worth failing a paid call.
    """
    pending = getattr(auth, "pending_payment", None)
    if pending is None:
        facts = _mpp_facts(auth)
        return facts.get("payer") if facts else None
    try:
        payload = getattr(pending, "payload", None)
        inner = getattr(payload, "payload", None) or {}
        authorization = inner.get("authorization") or {}
        payer = authorization.get("from") or None
    except Exception:
        payer = None
    if payer:
        return payer
    # No authorization block. A Solana (SVM) payload carries a signed
    # transaction, not an EIP-3009 authorization, so the payer is named only
    # by the facilitator -- on its settle response. Found live 2026-09-22:
    # the first Solana-settled call (tx prRhBh3n...) was recorded with no
    # payer and, because `settled` hung off the payer, as not settled, and
    # its receipt told the buyer they had not been charged.
    try:
        result = getattr(pending, "settle_result", None)
        return getattr(result, "payer", None) or None
    except Exception:
        return None


def _payment_facts_of(auth) -> dict:
    """The on-chain facts of this call's payment, for the receipt: network,
    asset, pay-to wallet, exact settled amount, transaction hash.

    Read off the accepted requirement and the facilitator's settle response
    the same way _payer_of reads the payer: by attribute, never by importing
    the payment layer. Any shape surprise yields an empty field, never a
    failed delivery -- and the receipt then says that field is unknown.
    """
    facts = {"network": None, "asset": None, "pay_to": None,
             "amount_atomic": None, "tx_hash": None}
    pending = getattr(auth, "pending_payment", None)
    if pending is None:
        mpp = _mpp_facts(auth)
        if mpp:
            facts.update({k: mpp.get(k) for k in facts})
        return facts

    def field(obj, *names):
        for name in names:
            value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            if value not in (None, ""):
                return value
        return None

    try:
        requirements = getattr(pending, "requirements", None) or []
        accepted = requirements[0] if requirements else None
        if accepted is not None:
            facts["network"] = field(accepted, "network")
            facts["asset"] = field(accepted, "asset")
            facts["pay_to"] = field(accepted, "pay_to", "payTo")
            amount = field(accepted, "amount", "max_amount_required", "maxAmountRequired")
            facts["amount_atomic"] = int(amount) if amount is not None else None
        result = getattr(pending, "settle_result", None)
        if result is not None:
            facts["tx_hash"] = field(result, "transaction")
            facts["network"] = field(result, "network") or facts["network"]
            amount = field(result, "amount")
            if amount is not None:
                facts["amount_atomic"] = int(amount)
    except Exception:
        pass
    return facts


def _tx_of(auth) -> Optional[str]:
    pending = getattr(auth, "pending_payment", None)
    if pending is None:
        return None
    try:
        result = getattr(pending, "settle_result", None)
        return getattr(result, "transaction", None) if result is not None else None
    except Exception:
        return None


async def _run_job(worker, payload: dict, call_id: str):
    """Execute the worker's skill inside the envelope. Raises WorkerError."""
    skill = REGISTRY.get(worker.skill)
    if skill is None:  # pragma: no cover - guarded by a catalog test
        raise runtime.WorkerError(
            f"Worker {worker.name} has no implementation registered.",
            reason="not_implemented")
    ctx = JobContext(call_id, worker.name, worker.max_seconds)
    result = await skill(ctx, payload)
    # No language barrier: a bee that does not write its prose in `language`
    # natively has its human-readable strings translated on the same job,
    # before the delivery contract is checked (see skills/localize.py).
    language = payload.get("language") if isinstance(payload, dict) else None
    if language and worker.name in catalog.LOCALIZED_WORKERS:
        result = await localize.apply(ctx, result, language)
    return result, ctx


def _record_attempts(call_id: str, ctx_or_attempts) -> None:
    attempts = getattr(ctx_or_attempts, "attempts", ctx_or_attempts) or []
    for attempt in attempts:
        ledger.record_provider_call(
            call_id=call_id, provider=attempt.provider, attempt=attempt.attempt,
            started_at=attempt.started_at, ok=attempt.ok,
            latency_ms=attempt.latency_ms, failure_reason=attempt.failure_reason,
            cost_micros=attempt.cost_micros, cost_measured=attempt.cost_measured,
            usage=attempt.usage)


def _make_handler(worker):
    async def handler(
        request: Request,
        x_api_key: Optional[str] = Header(None),
        x_payment: Optional[str] = Header(None),
        authorization: Optional[str] = Header(None),
        idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    ):
        if not _configured:  # pragma: no cover - startup guard
            return JSONResponse(status_code=503, content={
                "status": "error",
                "detail": "Worker network is not configured on this deployment."})

        # Body first, and validated BEFORE payment is read. Same ordering the
        # audit routes use, for the same reason: a 400 raised after the
        # facilitator has verified burns the payer's nonce, so their corrected
        # retry is refused as a replay.
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={
                "status": "error", "reason": "invalid_request",
                "detail": "Body must be a JSON object.", "billed": False})

        return await serve(worker, payload, request, x_api_key, x_payment,
                           authorization, idempotency_key)

    handler.__name__ = f"work_{worker.name.replace('.', '_')}"
    return handler


async def serve(worker, payload: dict, request: Request, x_api_key, x_payment,
                authorization, idempotency_key=None, sink=None):
    """One paid job, from the availability check to the delivered body.

    This is the whole HTTP handler after its body has been parsed, and the
    MCP transport calls it directly with the tool's arguments as `payload`
    and the /mcp request as `request` -- so a tool sold over MCP is gated,
    run, billed, receipted and recorded by exactly the code its HTTP route
    uses. Returns what the route returns: a dict (a delivered result with no
    receipt headers to carry) or a JSONResponse (a refusal, a failure, or a
    delivery carrying receipt headers). `sink`, when given, receives the
    auth context under "auth" once the gate has passed, for a caller that
    needs the settlement receipt in another shape.
    """
    skill = REGISTRY.get(worker.skill)
    if skill is None:  # pragma: no cover
        return JSONResponse(status_code=503, content={
            "status": "error", "detail": f"{worker.name} is not implemented here."})

    # Fail closed BEFORE the payment gate. A worker whose provider has no
    # credential on this deployment cannot be delivered, so it must never
    # quote a price -- taking money for a capability we cannot run is the
    # worst outcome available here.
    if not worker.available():
        return JSONResponse(status_code=503, content={
            "status": "error", "reason": "capability_unavailable", "billed": False,
            "detail": (f"{worker.name} is not available on this deployment: "
                       f"{worker.unavailable_reason()}"),
        })

    # Cheap input validation before the gate. The skills raise
    # InvalidRequest from their own validators; run them dry by letting
    # the job start only after payment, but catch the obvious shape errors
    # here via the schema's required keys.
    missing = [key for key in (worker.input_schema.get("required") or [])
               if key not in payload]
    if missing:
        return JSONResponse(status_code=400, content={
            "status": "error", "reason": "invalid_request",
            "detail": f"Missing required field(s): {', '.join(missing)}.",
            "input_schema": worker.input_schema, "billed": False})
    # A skill that can tell, from the body alone, that it cannot deliver
    # refuses here -- before the gate, so no nonce is burned and nothing is
    # verified for a request that was always going to fail.
    precheck = PRECHECKS.get(worker.skill)
    try:
        # `language` is accepted by every worker; a malformed tag is refused free.
        validate_language(payload)
        if precheck is not None:
            precheck(payload)
    except runtime.WorkerError as exc:
        return JSONResponse(status_code=_error_status(exc.reason), content={
            "status": "error", "reason": exc.reason, "detail": exc.detail,
            "input_schema": worker.input_schema, "billed": False})

    auth, err = await asyncio.get_running_loop().run_in_executor(
        _executor, lambda: _authorize(
            x_api_key, x_payment, authorization, request,
            price_usd=worker.price_usd, product=worker.name, route=worker.path, body=payload))
    if err is not None:
        return err
    if sink is not None:
        sink["auth"] = auth

    call_id = uuid.uuid4().hex
    payer = _payer_of(auth)
    sale = getattr(auth, "sale", None)
    if isinstance(sale, dict):
        # The core's purchase book keys this call's row on these.
        sale.update(call_id=call_id, receipt_id=ledger.receipt_id_for(call_id),
                    idempotency_key=idempotency_key)

    # Duplicate protection. A "done" key returns the stored result and
    # NEVER calls _bill -- so an x402 payment stays verified but unsettled,
    # and no money moves. The rails that take money AT THE GATE are handed
    # it back (_take_nothing): a prepaid key was debited before this check
    # and an MPP payment was marked as spent, and a body that says "not
    # charged" while keeping either was a second charge for one job --
    # repeated on every poll of a running one.
    claimed = False
    if idempotency_key:
        state, stored = await asyncio.get_running_loop().run_in_executor(
            _executor, lambda: ledger.claim_idempotency(
                idempotency_key, call_id, worker.name))
        if state == "done":
            try:
                content = json.loads(stored) if stored else {}
            except json.JSONDecodeError:
                content = {}
            content["idempotent_replay"] = True
            content["billed"] = False
            content["note"] = (
                "Returned the stored result for this Idempotency-Key. This "
                "request was not charged.")
            await _take_nothing(auth, content, "idempotent replay: stored result returned")
            return JSONResponse(status_code=200, content=content)
        if state == "in_progress":
            content = {
                "status": "error", "reason": "in_progress", "billed": False,
                "detail": ("A request with this Idempotency-Key is still running. "
                           "Retry shortly to collect its result.")}
            await _take_nothing(auth, content, "duplicate of a request that is still running")
            return JSONResponse(status_code=409, headers={"Retry-After": "5"}, content=content)
        claimed = state == "claimed"

    ledger.open_call(call_id=call_id, worker=worker.name, path=worker.path,
                     price_usd=worker.price_usd, idempotency_key=idempotency_key,
                     payer=payer, rail=_rail_of(auth),
                     request_hash=ledger.canonical_hash(payload),
                     node_version=_node_version)

    work = asyncio.ensure_future(
        _complete(worker, payload, call_id, auth, payer, claimed, idempotency_key))
    after = deliver_later_after()
    if after <= 0:
        return await work
    done, _pending = await asyncio.wait({work}, timeout=after)
    if work in done:
        return work.result()
    # Still running at the caller's limit: a job to collect, charged only when
    # it delivers. A prepaid key was debited before the job ran, so its debit
    # is first written down as a hold under the call id, in the store that
    # holds the money -- whoever closes an interrupted job can then hand it
    # back, once. Written off the event loop, and only for a call that has a
    # debit: a job paid per call never reaches the key store.
    held = False
    if (_hold_payment is not None and getattr(auth, "prepaid_key", None)
            and getattr(auth, "prepaid_cents", 0)):
        write = asyncio.get_running_loop().run_in_executor(
            _executor, lambda: bool(_hold_payment(auth, call_id)))
        write.add_done_callback(lambda done: done.cancelled() or done.exception())
        try:
            # The 202 never waits long for the bookkeeping: if every worker
            # thread is busy, the job is handed back without a hold in hand.
            held = await asyncio.wait_for(asyncio.shield(write), _HOLD_WRITE_WAIT_SECONDS)
        except asyncio.TimeoutError:
            log.warning("the prepaid hold of call %s was not written within %.0f s; "
                        "handing the job back without it", call_id, _HOLD_WRITE_WAIT_SECONDS)
        except Exception:  # pragma: no cover - the core's helper does not raise
            log.exception("could not record the prepaid hold of call %s", call_id)
        if held and work.done():
            # The job ended while its hold was being written. Whatever it
            # owed back went back directly (it was not in _HELD yet), so the
            # hold must not stay open for a later sweep to refund again.
            held = False
            if _close_hold is not None:
                _off_loop(_close_hold, call_id)
    return _defer(worker, work, call_id, auth, idempotency_key if claimed else None, held)


async def _take_nothing(auth, content: dict, note: str) -> None:
    """Hand back what the gate took for a request answered without new work,
    and put on `content` anything the payer is owed regardless (a key a
    top-up bought with this very request). Never raises: the answer itself
    is free and must not fail on the bookkeeping."""
    if _not_charged is None:
        return
    try:
        await asyncio.get_running_loop().run_in_executor(
            _executor, lambda: _not_charged(auth, content, note))
    except Exception:  # pragma: no cover - the core's helper does not raise
        log.exception("could not hand back the payment of an unbilled request")


async def _complete(worker, payload: dict, call_id: str, auth, payer, claimed: bool,
                    idempotency_key):
    """Run the job and finish the sale: the delivery contract, billing, the
    receipt, the ledger row and the idempotency record. The same code path
    whether the caller is still waiting or the job was handed back to collect
    (see _defer); only who receives the response differs.
    """
    try:
        async with _semaphore:
            result, ctx = await _run_job(worker, payload, call_id)
    except runtime.WorkerError as exc:
        _record_attempts(call_id, getattr(exc, "attempts", []))
        ledger.close_call(call_id, "failed", failure_reason=exc.reason,
                          failure_stage="execute", payer=payer)
        if claimed and idempotency_key:
            # The job failed and was not billed, so the caller is entitled
            # to retry the same key.
            ledger.release_idempotency(idempotency_key)
        status = _error_status(exc.reason)
        if status == 400:
            # The caller's fault, found only once the job ran -- after the
            # gate. It unwinds through the core like every other unbilled
            # failure (this path used to answer "billed": false and keep a
            # prepaid debit and an MPP payment); only the wording differs.
            return _unbilled_failure(auth, worker, call_id, exc.reason, 400, exc.detail, extra={
                "detail": exc.detail, "input_schema": worker.input_schema})
        # Everything else goes through the CORE's failure path, so an
        # unbilled worker failure unwinds exactly like an unbilled audit.
        response = _unbilled_failure(auth, worker, call_id, exc.reason, status, exc.detail)
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            response.headers["Retry-After"] = str(retry_after)
        return response
    except Exception as exc:  # pragma: no cover - unexpected adapter bug
        log.exception("worker %s crashed", worker.name)
        ledger.close_call(call_id, "failed", failure_reason="internal_error",
                          failure_stage="execute", payer=payer)
        if claimed and idempotency_key:
            ledger.release_idempotency(idempotency_key)
        _refund_through_hold(auth, call_id)
        return _failed(auth, f"{worker.name} failed: {type(exc).__name__}")

    _record_attempts(call_id, ctx)

    # The delivery contract, before anything is charged: the result must
    # match the output schema this route publishes everywhere (openapi.json,
    # the MCP outputSchema, the 402's Bazaar record, the A2A skill). A result
    # that does not is a failed job -- 502, nothing billed, on the ledger as
    # contract_mismatch -- never a paid surprise. Same unwinding as a
    # provider failure.
    problem = catalog.contract.check(worker.output_schema, result)
    if problem is not None:
        log.error("worker %s: result violates its published output schema: %s", worker.name, problem)
        ledger.close_call(call_id, "failed", failure_reason="contract_mismatch",
                          failure_stage="deliver", payer=payer)
        if claimed and idempotency_key:
            ledger.release_idempotency(idempotency_key)
        return _unbilled_failure(
            auth, worker, call_id, "contract_mismatch", 502,
            f"{worker.name} produced a result that does not match its published "
            f"output schema ({problem}); it was not delivered")

    # Only now, with a real result in hand, is anything charged. From here
    # the payment may have moved (the settle runs in a thread that cannot be
    # cancelled), so a job cut off past this point is never unwound.
    _BILLING.add(call_id)
    warning = await asyncio.get_running_loop().run_in_executor(
        _executor, lambda: _bill(auth, worker.price_usd))

    # The receipt: the ledger row read back, at /work/receipts/{id}. Its
    # id is in the delivered body (so an idempotent replay returns the
    # same one) and the result hash is written to the row before the
    # response leaves, so the receipt can never describe a delivery the
    # caller did not get.
    receipt_id = ledger.receipt_id_for(call_id)
    content = {
        "status": "ok",
        "worker": worker.name,
        "price_usd": worker.price_usd,
        "result": result,
        "provenance": ctx.provenance(),
        "receipt_id": receipt_id,
        "receipt_url": f"/work/receipts/{receipt_id}",
    }
    if warning:
        content["billing_warning"] = warning
    if "google-translate-llm" in ctx.providers_used:
        # Cloud Translation's attribution requirement travels with translated text.
        content["attribution"] = [{"text": "Translated by Google", "url": "https://translate.google.com"}]

    delivered = _deliver(content, auth)
    facts = _payment_facts_of(auth)
    # Re-read the payer AFTER settlement: a Solana payment names its payer
    # only in the facilitator's settle response, which did not exist when
    # the call was opened. An EVM payer, known from the start, is unchanged.
    payer = _payer_of(auth) or payer
    settle_state = getattr(getattr(auth, "pending_payment", None), "settle_state", None)

    # _deliver returns the payable 402 instead when the facilitator
    # REFUSED to settle. Nothing was earned, so nothing is stored under
    # the idempotency key and the ledger says refused.
    refused = getattr(delivered, "status_code", 200) == 402
    # Settled means the facilitator settled: its own verdict first, the
    # payer's presence second (MPP hash payments carry no settle_state).
    settled = not refused and (settle_state == "settled" or payer is not None)
    ledger.close_call(
        call_id, "refused" if refused else "ok",
        latency_ms=ctx.provenance()["elapsed_ms"],
        provider_used=",".join(ctx.providers_used) or None,
        providers_tried=",".join(ctx.providers_used) or None,
        attempts=len(ctx.attempts), settled=settled,
        tx_hash=_tx_of(auth) or facts["tx_hash"], payer=payer,
        result_hash=ledger.canonical_hash(result),
        network=facts["network"], asset=facts["asset"],
        pay_to=facts["pay_to"], amount_atomic=facts["amount_atomic"])

    if idempotency_key and claimed:
        if refused:
            ledger.release_idempotency(idempotency_key)
        else:
            ledger.complete_idempotency(idempotency_key, json.dumps(content))
    _BILLING.discard(call_id)
    return delivered


# --- deliver later -----------------------------------------------------------
#
# Routers' clients give up at 30 s (Mercator's gateway, measured 2026-10-01),
# and a slow job -- a Veo video is 30-45 s -- used to be either waited out
# past that or failed. Now any job still running at this many seconds is
# answered 202 with a job to collect, keeps running here, and is charged only
# when it delivers: x402 authorizations stay valid for the 402's 300 s
# maxTimeoutSeconds, so _bill settles the held payment once the result exists,
# exactly as it would have inline. A job that fails is never charged.

_DEFERRED: dict = {}
# Call ids of handed-back jobs whose prepaid debit is written down as a hold
# (billing.hold_prepaid). For these the refund goes THROUGH the hold, which
# can be turned into credit exactly once -- see _refund_through_hold.
_HELD: set = set()
# Call ids whose billing has started (see _complete). A job cut off after
# that point may already have been charged.
_BILLING: set = set()
# How long a 202 waits for its prepaid hold to be written (see serve).
_HOLD_WRITE_WAIT_SECONDS = 2.0


def deliver_later_after() -> float:
    """Seconds a caller waits before a still-running job is handed back."""
    try:
        return float(os.environ.get("WORKER_DELIVER_LATER_AFTER_SECONDS", "22"))
    except ValueError:
        return 22.0


def _defer(worker, work, call_id: str, auth, idempotency_key=None, held: bool = False) -> JSONResponse:
    job_id = uuid.uuid4().hex
    receipt_id = ledger.receipt_id_for(call_id)
    rail = _rail_of(auth)
    # MPP push payments moved money before the job ran; their hash is kept so
    # a restart that interrupts the job can release it (reconcile_interrupted).
    mpp_tx = _payment_facts_of(auth).get("tx_hash") if rail == "mpp" else None
    # `held`: this call's prepaid debit is written down as a hold (see serve).
    if held:
        _HELD.add(call_id)
    ledger.open_deferred(job_id, call_id, worker.name, rail=rail, mpp_tx=mpp_tx)
    _DEFERRED[job_id] = work
    work.add_done_callback(
        lambda task: _finish_deferred(job_id, task, auth, call_id, idempotency_key))
    collect = f"/work/jobs/{job_id}"
    content = {
        "status": "processing",
        "worker": worker.name,
        "price_usd": worker.price_usd,
        "job_id": job_id,
        "collect_url": collect,
        "receipt_id": receipt_id,
        "receipt_url": f"/work/receipts/{receipt_id}",
        "retry_after_seconds": 10,
        "billed": False,
        "detail": (
            f"{worker.name} is still running. You have not been charged: payment is taken "
            f"only when the result is ready, and nothing is charged if it fails. GET "
            f"{collect} (free, no payment) to collect it; it is kept for 48 hours."),
    }
    # A key a top-up bought with this very request is the buyer's from this
    # moment, whatever becomes of the job: it used to arrive only with the
    # finished result, and an interrupted job never delivered one.
    if _attach_key is not None:
        try:
            _attach_key(content, auth)
        except Exception:  # pragma: no cover - the core's helper does not raise
            log.exception("could not put the top-up key on the 202 of job %s", job_id)
    return JSONResponse(status_code=202, headers={"Retry-After": "10", "Location": collect},
                        content=content)


def _response_parts(response) -> tuple:
    """(status, JSON body text, headers JSON) of what a waiting caller would
    have received."""
    if isinstance(response, dict):
        return 200, json.dumps(response), "{}"
    status = getattr(response, "status_code", 500)
    body = bytes(getattr(response, "body", b"") or b"").decode() or "{}"
    headers = {k: v for k, v in getattr(response, "headers", {}).items()
               if k.lower() not in ("content-length", "content-type")}
    return status, body, json.dumps(headers)


def _finish_deferred(job_id: str, task, auth=None, call_id: Optional[str] = None,
                     idempotency_key: Optional[str] = None) -> None:
    _DEFERRED.pop(job_id, None)
    try:
        response = task.result()
    except BaseException as exc:
        # _complete answers its own failures, so this is a job CUT OFF: the
        # task was cancelled (a shutdown whose drain ran out) or crashed
        # outside its own handling. It delivered nothing, so it is closed the
        # way a restart-interrupted job is -- and by the same single step out
        # of 'running', so the payment is handed back at most once.
        billing = call_id in _BILLING
        _BILLING.discard(call_id)
        # Billing moves money only for a payment that settles AFTER delivery
        # (x402, a metered subscription). A prepaid debit or an MPP payment
        # was taken at the gate, so a cut-off there is unwound like any other.
        if billing and (auth is None or getattr(auth, "pending_payment", None) is not None
                        or getattr(auth, "stripe_billable", False)):
            # Cut off WHILE its payment was being finalised. The settle may
            # have gone through, so nothing is handed back, the idempotency
            # key stays taken (a resend must not become a second charge) and
            # the buyer is told the truth: check the receipt.
            _HELD.discard(call_id)
            log.error("deferred job %s (call %s) was cut off while its payment was being "
                      "finalised: %s. Its result was lost; reconcile it by hand.",
                      job_id, call_id, type(exc).__name__)
            receipt_id = ledger.receipt_id_for(call_id)
            ledger.close_interrupted(job_id, json.dumps({
                "status": "error", "reason": "interrupted_during_billing", "billed": None,
                "receipt_id": receipt_id, "receipt_url": f"/work/receipts/{receipt_id}",
                "detail": ("The job was cut off while its payment was being finalised, and its "
                           "result was lost. It may have been charged: check the receipt "
                           "before sending the request again, and write to "
                           "hubvibe@hubvibe-io.com with the receipt id if it was.")}))
            return
        log.error("deferred job %s was cut off: %s: %s", job_id, type(exc).__name__, exc)
        closed = ledger.close_interrupted(job_id, json.dumps({
            "status": "error", "reason": "interrupted", "billed": False,
            "detail": ("The job was cut off before it finished. Nothing was charged; "
                       "send the request again.")}))
        if closed:
            _unwind_cut_off(auth, call_id, idempotency_key)
        else:
            _HELD.discard(call_id)  # another copy closed it and handed it back
        return
    status, body, headers = _response_parts(response)
    ledger.finish_deferred(job_id, status, body, headers)
    # The job ended and its billing is final. A hold still open here belongs
    # to a job that delivered (the debit is kept): close it, so nothing can
    # refund it later. Only for a call that has one -- a job paid per call
    # never reaches the key store -- and off the event loop, which every
    # other request is served from.
    _BILLING.discard(call_id)
    held = call_id in _HELD
    _HELD.discard(call_id)
    if held and _close_hold is not None:
        _off_loop(_close_hold, call_id)


def _off_loop(fn, *args) -> None:
    """Run a small blocking bookkeeping call on the workers' own executor
    rather than on the event loop. Never raises."""
    def run():
        try:
            fn(*args)
        except Exception:  # pragma: no cover - the core's helpers do not raise
            log.exception("background bookkeeping failed: %s", getattr(fn, "__name__", fn))
    try:
        _executor.submit(run)
    except Exception:  # the executor is gone (shutdown): do it here
        run()


def _unwind_cut_off(auth, call_id: Optional[str], idempotency_key: Optional[str]) -> None:
    """A handed-back job this process was running ended without an answer:
    close its ledger row failed, hand back what the gate took (the core's
    unbilled-failure path: prepaid debit through its hold, MPP credential
    released, the call booked as unbilled) and free its idempotency key."""
    if call_id:
        ledger.close_call(call_id, "failed", failure_reason="interrupted",
                          failure_stage="execute")
    if auth is not None and _failed is not None:
        try:
            _refund_through_hold(auth, call_id)
            _failed(auth, "The job was cut off before it finished")
        except Exception:
            log.exception("could not hand back the payment of a cut-off job")
    if idempotency_key:
        ledger.release_idempotency(idempotency_key)


@router.get("/work/jobs/{job_id}", tags=["workers"])
async def collect_job(job_id: str):
    """Collect a job that was handed back as 202. Free: the payment rode on
    the original call and is taken only when the result is ready."""
    row = ledger.get_deferred(job_id)
    if row is None:
        return JSONResponse(status_code=404, content={
            "status": "error", "reason": "unknown_job", "billed": False,
            "detail": "No such job here. Jobs are kept for 48 hours after they start."})
    if row["state"] == "running":
        return JSONResponse(status_code=202, headers={"Retry-After": "5"}, content={
            "status": "processing", "job_id": job_id, "worker": row["worker"],
            "retry_after_seconds": 5, "billed": False,
            "detail": "Still running. Not charged yet; collect again in a few seconds."})
    try:
        headers = json.loads(row["headers"] or "{}")
    except json.JSONDecodeError:
        headers = {}
    try:
        content = json.loads(row["body"] or "{}")
    except json.JSONDecodeError:
        content = {"status": "error", "detail": "Stored result unreadable."}
    return JSONResponse(status_code=int(row["http_status"] or 200), content=content, headers=headers)


@router.get("/work/media/{name}", tags=["workers"], include_in_schema=False)
async def media(name: str):
    """A generated file (video.generate) by its link, for 48 hours. Read off
    the event loop's own pool so a download never waits behind an audit."""
    path = media_store.path_for(name)
    if path is None:
        return JSONResponse(status_code=404, content={
            "status": "error", "reason": "unknown_media", "billed": False,
            "detail": "No such file. Generated media links last 48 hours."})

    def read():
        with open(path, "rb") as handle:
            return handle.read()

    data = await asyncio.get_running_loop().run_in_executor(_executor, read)
    return Response(content=data, media_type=media_store.media_type(name),
                    headers={"Cache-Control": "private, max-age=86400"})


# A job written before jobs carried their owner (rows from the release before
# owners existed) is judged by age instead: no job runs longer than
# catalog.MAX_WORKER_SECONDS plus its settlement, so one this old has no
# process left to finish it.
_OWNERLESS_GRACE_SECONDS = 600.0


def reconcile_interrupted(release_mpp_hash: Optional[Callable] = None) -> int:
    """Every job whose process is gone -- a crash, a restart, a shutdown drain
    that ran out -- is closed as failed and unbilled, what it took before
    delivery handed back (an MPP payment released, a prepaid debit refunded
    through its hold), its idempotency key freed. A job that another live copy of the node is still running is never
    touched (ledger "instance liveness"): during a deploy two copies share the
    volume, and closing the other's job would release a payment for work that
    is about to be delivered. Returns how many were closed."""
    count = 0
    now = time.time()
    for row in ledger.running_deferred():
        if row["job_id"] in _DEFERRED:
            continue
        owner = row.get("owner")
        if owner:
            if ledger.owner_alive(owner):
                continue
        elif now - float(row.get("created_at") or now) < _OWNERLESS_GRACE_SECONDS:
            continue
        closed = ledger.close_interrupted(row["job_id"], json.dumps({
            "status": "error", "reason": "interrupted", "billed": False,
            "detail": ("The job was interrupted by a restart of this node before it "
                       "finished. Nothing was charged; send the request again.")}))
        if not closed:
            continue
        ledger.close_call(row["call_id"], "failed", failure_reason="interrupted",
                          failure_stage="execute")
        if row.get("mpp_tx") and release_mpp_hash is not None:
            try:
                release_mpp_hash(row["mpp_tx"])
            except Exception as exc:  # pragma: no cover
                log.error("could not release MPP payment %s: %s", row["mpp_tx"], exc)
        # A prepaid key debited for the job gets the debit back through the
        # hold written when the job was handed back. Only a job with no
        # per-call rail can have one: x402 and MPP jobs are never asked about.
        if _refund_hold is not None and not row.get("rail"):
            try:
                outcome = _refund_hold(row["call_id"])
                if outcome:
                    log.warning("refunded the prepaid debit of interrupted job %s", row["job_id"])
                elif outcome is None:
                    log.error("the prepaid hold of interrupted job %s (call %s) could not be "
                              "refunded and is left open; the buyer may be owed it",
                              row["job_id"], row["call_id"])
            except Exception as exc:  # pragma: no cover
                log.error("could not refund the prepaid debit of job %s: %s", row["job_id"], exc)
        if row.get("idempotency_key"):
            ledger.release_idempotency(row["idempotency_key"])
        count += 1
    return count


async def drain(timeout: float) -> None:
    """At shutdown: let handed-back jobs finish (and charge, and store) within
    `timeout`, rather than cutting them off mid-sale."""
    pending = [task for task in list(_DEFERRED.values()) if not task.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)


async def keep_reconciled(release_mpp_hash: Optional[Callable] = None,
                          every_seconds: float = 60.0) -> None:
    """reconcile_interrupted once a minute for the life of the process. A job
    whose node died is then answered within a minute -- a clear unbilled
    failure for the buyer collecting it -- instead of reading "still running"
    until the next restart, which after a deploy may never come."""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(every_seconds)
        try:
            closed = await loop.run_in_executor(_executor, reconcile_interrupted, release_mpp_hash)
            if closed:
                log.warning("closed %d job(s) whose node stopped before they finished, unbilled",
                            closed)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("job reconciliation failed: %s", exc)


def _refund_through_hold(auth, call_id: Optional[str]) -> None:
    """Before the core unwinds a failed job: if this job's prepaid debit was
    written down as a hold (it was handed back to collect later), refund it
    through the hold -- the one refund that can happen only once, whichever
    copy of the node gets there first -- and leave the core nothing to refund
    directly. A job that was never handed back has no hold and the core
    refunds it as it always has."""
    if not call_id or call_id not in _HELD:
        return
    # Out of _HELD first: the hold is then never CLOSED by _finish_deferred,
    # so if this refund fails the record of what the buyer is owed survives
    # (key and cents, still 'held') and can be made good.
    _HELD.discard(call_id)
    outcome = None
    try:
        outcome = _refund_hold(call_id) if _refund_hold is not None else False
    except Exception:  # pragma: no cover - the core's helper does not raise
        log.exception("could not refund the prepaid hold of call %s", call_id)
    if outcome is None:
        log.error("the prepaid hold of call %s could not be refunded and is left open; "
                  "the buyer is owed it", call_id)
    try:
        auth.prepaid_cents = 0
    except Exception:  # pragma: no cover
        pass


def _unbilled_failure(auth, worker, call_id: str, reason: str, status: int, detail: str,
                      extra: Optional[dict] = None):
    """The failed-job response: the core's unbilled failure (prepaid debit
    refunded, MPP credential released, nothing settled) with the worker's
    reason, name and receipt id added to the body, then `extra`."""
    _refund_through_hold(auth, call_id)
    response = _failed(auth, detail)
    response.status_code = status
    try:
        body = json.loads(bytes(response.body).decode())
        body["reason"] = reason
        body["worker"] = worker.name
        body["receipt_id"] = ledger.receipt_id_for(call_id)
        body.update(extra or {})
        # The core's headers minus the ones describing ITS body: the body
        # just grew, and a copied Content-Length made every failed worker
        # call die mid-response instead of a clean 502.
        headers = {k: v for k, v in response.headers.items()
                   if k.lower() not in ("content-length", "content-type")}
        return JSONResponse(status_code=status, content=body, headers=headers)
    except Exception:  # pragma: no cover
        return response


def register_routes() -> None:
    """Add one POST route per catalog row."""
    for worker in catalog.CATALOG:
        router.add_api_route(
            worker.path, _make_handler(worker), methods=["POST"],
            name=worker.tool_name, tags=["workers"], summary=worker.title,
            description=f"{worker.description} ${worker.price_usd:.2f} per call.",
            # The 200 contract, in openapi.json: the envelope with this
            # worker's own result schema, and an example generated from it.
            # Without this every /work route documented its response as `{}`.
            responses={
                200: {
                    "description": f"Delivered result of {worker.name}; a receipt is at receipt_url.",
                    "content": {"application/json": {
                        "schema": catalog.response_schema(worker),
                        "example": catalog.response_example(worker),
                    }},
                },
                202: {
                    "description": ("Still running: collect free at collect_url (GET "
                                    "/work/jobs/{job_id}); paid only when it delivers."),
                },
            })


register_routes()


@router.get("/work", tags=["workers"])
async def work_index():
    """Free, unpaid index of the worker network.

    Discovery is free here exactly as it is on /mcp: an agent must be able to
    find out what is for sale and what it costs before deciding to pay.
    """
    live = catalog.live()
    unavailable = [w for w in catalog.CATALOG if w not in live]
    return {
        "workers": [
            {
                "name": worker.name,
                "path": worker.path,
                "title": worker.title,
                "price_usd": worker.price_usd,
                "tier": worker.tier,
                "description": worker.description,
                "tags": worker.tags,
                "input_schema": worker.input_schema,
                "output_schema": worker.output_schema,
                "returns": worker.returns,
                "max_seconds": worker.max_seconds,
                "pricing_basis": worker.pricing_basis,
                "composes": worker.composes,
                **({"buyer_note": catalog.buyer_note(worker)}
                   if catalog.buyer_note(worker) else {}),
            }
            for worker in live
        ],
        "count": len(live),
        "response_envelope": catalog.contract.RESPONSE_ENVELOPE,
        "spend_cap": (
            f"x402 client libraries cap a single payment at ${catalog.SPEND_CAP_USD:.2f} "
            "by default. Workers priced above that carry a buyer_note saying how to "
            "raise the cap; the price itself is fixed."),
        # Named, but NOT sold: an agent that saw this node yesterday can tell
        # "switched off here" apart from "never existed", without us quoting a
        # price for something we cannot run.
        "unavailable": [
            {"name": w.name, "reason": w.unavailable_reason()} for w in unavailable
        ],
        "payment": (
            "Per call over HTTP 402 / x402, the same rail as the audit routes. "
            "POST without payment to receive the challenge."),
        "idempotency": (
            "Send an Idempotency-Key header to make retries safe: a repeated key "
            "returns the stored result and is not charged again."),
        "receipts": (
            "Every response carries a receipt_id. GET /work/receipts/{receipt_id} "
            "(or /work/receipts/{request_id}) returns the machine-readable receipt: "
            "payer, pay_to, amount, asset, network, transaction hash, execution "
            "status, and sha256 hashes of the request and the delivered result."),
        "providers": provider_health(),
    }


@router.get("/work/receipts/{receipt_id}", tags=["workers"])
async def work_receipt(receipt_id: str):
    """The receipt for one job, by receipt_id or request_id. Free: it holds
    on-chain facts and hashes, never the delivered result."""
    call_id = ledger.call_id_for(receipt_id) or (receipt_id if receipt_id.isalnum() else None)
    receipt = ledger.receipt_for(call_id) if call_id else None
    if receipt is None:
        return JSONResponse(status_code=404, content={
            "status": "error", "reason": "unknown_receipt",
            "detail": "No job with this receipt_id or request_id on this node."})
    return receipt
