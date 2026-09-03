# HubVibe — session handoff

Read this first. It is the state of the business and the decisions already
made, so a new session does not re-litigate them or rebuild traps that were
already removed.

`docs/SESSION_BRIEF.md` is the companion: the standing rules and the facts
that do not move. It deliberately holds no numbers — every count and commit
is read from here or from a live run, because a brief that froze them went
stale in a chat paste and cost several sessions.

## 2026-09-02: THE REJECTION IS ROOT-CAUSED. The node killed its own verify on a poisoned thread.

The #79 logging paid for itself on its first live outing. `x402-log.sh`,
owner-run:

```
x402 verify FAILED before the facilitator could answer
  (facilitator=https://facilitator.xpay.sh price=$0.03):
  RuntimeError: asyncio.run() cannot be called from a running event loop
```

**Mechanism, reproduced deterministically, then fixed, then re-proven.**
Playwright's sync API (`app/browser_pool.py`) keeps a running asyncio event
loop in each worker thread for the lifetime of the pooled browser — that is
how the sync API works — and anyio REUSES those threads. So any request that
lands on a thread that has ever served an audit (even one whose browser
launch failed, because `sync_playwright().start()` runs first) finds
`asyncio.get_running_loop()` succeeding, and the bare `asyncio.run()` inside
`verify_only_sync` raised before the facilitator was ever contacted. Bare
402 to the caller, every time, whatever the wallet held.

Why no simulation had caught it: none of them ran a real audit before the
paid call, so no worker thread was poisoned. With `MAX_CONCURRENT_AUDITS=1`
(one worker thread) it reproduces on the second request, byte-for-byte the
live error, line 856 and all.

Fixed with `_run_coro_sync()`: when the current thread hosts a running loop,
the coroutine is handed to a fresh thread and `asyncio.run` there; otherwise
plain `asyncio.run`. Applied to all three payment call sites — verify,
settle (where this bug costs money directly: audit delivered, then settle
dies), and the legacy verify+settle. An AST-counted test refuses any new
bare `asyncio.run()` in the module.

Proof: three in-loop tests plus the AST guard, red under the bare call and
green under the fix; then the same poisoned-thread repro re-run — the stub
facilitator RECEIVED `/verify` and the node logged the facilitator's own
reason. **501 passed, 1 skipped**, lint 0.

**With xpay.sh confirmed compatible (next entry) this was the last known
in-code blocker on the paid path.** After deploying this, what remains is
the wallet: fund the payer and run `first-paid-call.sh`.

## 2026-09-02: OWNER READ xpay.sh /supported — it is COMPATIBLE. Do not re-litigate.

The owner ran `curl -s https://facilitator.xpay.sh/supported` from Cloud
Shell (the sandbox cannot; every facilitator host is egress-blocked). The
response, read off the owner's screen:

```json
{"kinds":[
  {"x402Version":2,"scheme":"exact","network":"eip155:8453"},
  {"x402Version":2,"scheme":"exact","network":"eip155:84532"},
  {"x402Version":1,"scheme":"exact","network":"base"},
  {"x402Version":1,"scheme":"exact","network":"base-sepolia"}],
 "extensions":[],
 "signers":{"eip155:*":["0x2772F7F74ac0aCA38C6238aA5EcE72B27bEB8C17"]}}
```

So the fork the #82 entry below left open is resolved: **xpay.sh lists Base
mainnet under BOTH names** — `eip155:8453` for v2 and `base` for v1. The #82
gate passes both versions against it and withholds nothing. The facilitator
does not need to change.

Which means the vocabulary mismatch #82 guards against was NOT the cause of
the two rejected live payments — the deployed revision at the time predated
#79 and threw the reason away, so their cause is still unknown. The leading
unexcluded candidate remains the unfunded paying wallet
(`0x5bcea6496599D65E432E50340056194D92F95d06` — balance never verified; the
Base RPC failed from Cloud Shell on every run). After deploying current
main, the next rejection prints its reason via `bash scripts/x402-log.sh`.
#82 stays: it turns a silent config-mismatch failure class into a loud one,
whichever facilitator is set.

## 2026-09-02: SIMULATED THE REJECTION. The node advertised x402 versions it could not verify.

Owner's instruction: stop looping, simulate, solve. Done with a stub
facilitator on localhost that records every request the node sends it, the
node booted with the live configuration, and `first-paid-call.sh` driving
the real client. Nothing here is inferred; every line below was observed.

**What the node was doing.** It sent the v2 `PAYMENT-REQUIRED` header
(naming `eip155:8453`) on every 402, and the v1 body (naming the legacy
`base`), regardless of what the facilitator's `/supported` listed. Against a
facilitator that lists only the legacy name, a v2-capable client took the v2
offer and signed for `eip155:8453`, and the node raised
`SchemeNotFoundError: No scheme 'exact' registered for network 'eip155:8453'`
**before the facilitator was called.** Fail-closed into a bare 402. Every
time. Whatever the wallet held. That is the exact shape of both live
rejections.

**Why v1 could not save it either.** `_get_requirements()` always builds
under the CAIP-2 name, and the library only does that when the facilitator's
`/supported` lists that exact name: `ExactEvmServerScheme.parse_price("$0.03",
"base")` raises `Unsupported network format`. So against a legacy-only
facilitator this server library can verify **nothing** — offered v1, paid v1,
same `SchemeNotFoundError`, facilitator never called. Not our bug to route
around; a library constraint to respect.

**The fix: never advertise a version the node cannot verify.**
`_facilitator_supports(version, network)` asks the initialized server's own
`get_supported_kind()` — read off the cached `/supported`, wildcards included
— and additionally requires the CAIP-2 name to be listed at all. The v2
header and the v1 body each go through it. Fail-closed on any exception. One
WARNING per (version, network) naming the facilitator and the reason.

Proved end to end against the stub, both ways:

| facilitator lists | v2 header | v1 body | client | facilitator got |
|---|---|---|---|---|
| only `base` | withheld | withheld | **stops at preflight: "no payable x402 entry"** — nothing signed | nothing, correctly |
| `base` + `eip155:8453` | sent | sent | pays v2 | `/verify`, and its reason lands in the node log |

Six unit tests, each proved by removing its gate and watching it go red.
Suite: **497 passed, 1 skipped.** Lint 0. Two app-test loaders now fake the
gate, because `facilitator.example` does not exist and those tests are about
the 402's shape.

**What decides the live case, and only the owner can read it:**

```bash
curl -s https://facilitator.xpay.sh/supported
```

If that lists `eip155:8453` → the node will verify against it, and after this
deploys a rejection carries the facilitator's own reason in
`bash scripts/x402-log.sh`. If it lists only `base` → **xpay.sh cannot be
used by this server library at all**, the node will (correctly) advertise
no x402, and the facilitator has to change — `scripts/probe-facilitators.sh`
checks exactly this for each candidate.

## 2026-09-02: when the balance check cannot run, hand over the Basescan link

The Base RPC (`mainnet.base.org`) has answered `HTTPError` from Cloud Shell on
every `first-paid-call.sh` run so far, so the script proceeded blind each
time and "is the paying wallet funded?" stayed the one open question after
two rejected attempts. It now prints
`https://basescan.org/address/<paying address>` on that branch. One tap on a
phone, no gcloud. A rejection is not to be read as anything else until that
page has been looked at. Proved by removing the line → the test goes red.

**Also from the 1:33 screenshot:** the owner's phone keyboard substitutes
`ø` for `o` in some pastes (`prøject`, `løg`, `ftrst-pald`), and `&&` chains
split across lines. Commands to the owner: one per line, short, and checked
for `ø` before enter. The `(resolver-time)` in the prompt means the project
IS set; the `gcloud config set` error was the `ø`, not the config.

## 2026-09-02: consolidation — one go-live path, and a Stripe mirror that stops lying

Owner's instruction: get rid of trash, solidify what works, be consistent.
Only what had evidence went:

- **`go-live-x402.sh`, `go-live-mpp-tempo.sh` → stubs.** Zero live
  references outside themselves and this file's history; `go-live.sh`
  replaced both with one deploy. Stubs, not deletions: a deleted script
  produces "No such file" and a hunt — that cost a re-derivation once. Each
  prints the replacement command and exits 1. Their 25 tests went with them;
  the three guards only they held (API-version pin, no Stripe mint for x402,
  the stubs themselves pointing here) now live in `tests/test_go_live.py`.
- **`x402-setup.py` → stub.** Named for x402, argued for a Stripe-custodied
  x402 pay-to, and Stripe does not do x402. What it minted, `go-live.sh`
  mints inline for tempo.
- **`record_settlement_in_stripe` is now opt-in (`X402_STRIPE_MIRROR=1`).**
  On a self-custody pay-to it cannot succeed, and default-on it would have
  logged a traceback on **every real payment** saying Stripe "will not show
  it until this transaction hash is recorded" — false, on the day it is read.
  Off: one INFO line saying the money is in the wallet. Proved by removing
  the gate → the new test goes red.
- The module docstring that still said the pay-to "is a Stripe-custodied
  deposit address" now says where the money actually goes.
- `SESSION_BRIEF.md` no longer names the stubbed script.

Suite: **491 passed, 1 skipped** (514 − 25 deleted + 4 new − 2 parametrized
cases). Lint gate 0. Sandbox: stray local servers killed, caches cleared.

## 2026-09-02: the deploy REFUSED to run in a fresh Cloud Shell — and blamed a secret

The second attempt of the night, read off the owner's screenshot:

```
==> Checking the Stripe secret exists before pointing anything at it
  Available secrets:
  STOP  no secret named SECRET_STRIPE_KEY. Re-run as:
        STRIPE_SECRET_NAME=<one of the above> bash scripts/repair-and-deploy.sh
```

**Nothing was deployed.** The node stayed on the old image, so the
`first-paid-call.sh` that followed could only repeat the earlier rejection,
and the `gcloud logging read` that followed *that* was a 200-character line
that arrived from a phone with a newline inside the filter and failed to
parse. Three commands, zero information.

The secret was never missing. **`repair-and-deploy.sh` made eight `gcloud`
calls and none passed `--project`.** It inherited gcloud's default project,
which the first Cloud Shell session of the night had and the second — a fresh
one, after the first disconnected — did not. gcloud does not treat an unset
project as an error: `secrets describe` fails, `secrets list` prints nothing,
and the script read that empty list as "the secret you named is not among
these" and stopped. The one deploy command could not run in a fresh shell,
and its error pointed at the wrong thing.

Fixed everywhere the pattern existed: `repair-and-deploy.sh` (8 calls),
`go-live-x402.sh` (2), and `lib-api-key.sh` — which already threaded
`project_args` through both of its calls but only filled it when `PROJECT`
was exported, so `verify-live.sh`'s paid-path check inherited the same trap.
All now default `PROJECT` to `resolver-time` and pass it explicitly.
`repair-secrets.sh`, `measure-call-cost.sh`, `go-live.sh` and
`go-live-mpp-tempo.sh` already did; a per-line grep undercounted the first
two because their `--project` sits on a `\` continuation line, so the test
joins continuations before counting. **The immediate unblock, on the current
checkout, is one line:** `gcloud config set project resolver-time`.

The secret check now tells the two faults apart: an *empty* secret list is
reported as gcloud not seeing the project, with the `config set` line, rather
than as a missing secret.

Pinned by a static test over all seven scripts — "invocation" meaning
`gcloud` in command position, not inside a message string — proved by
removing one `--project` from the deploy line and watching it go red. Plus a
driven test that the empty-list case names the project, not the secret.

**Also:** `scripts/x402-log.sh` replaces the long `gcloud logging read` line.
One short command, both payload shapes (`textPayload` and
`jsonPayload.message`), and a "(none)" branch that says what none means.
Untested against gcloud from the sandbox; `bash -n` clean.

## 2026-09-02: FIRST REAL PAYMENT ATTEMPTED — rejected, and the node threw the reason away

The owner ran `first-paid-call.sh` against the deployed node from Cloud
Shell, paying from `0x5bcea6496599D65E432E50340056194D92F95d06` (an existing
key at `~/.hubvibe-wallet-key`) to `0x837C…77dd`. Read off the screenshot:

```
OK    x402 advertised: $0.03 to 0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd on base
OK    the Bazaar record on this 402 is well-formed and will survive validation
NOTE  could not reach the Base RPC (HTTPError); proceeding without the check
NOTE  https://facilitator.xpay.sh serves no /discovery/resources ...
STOP  the payment did not go through:
      HubVibeError: Payment was rejected by HubVibe: {'error': 'payment_required', ...
```

Three facts, and one that is NOT known:

1. **The client-side blocker is gone.** A signature was constructed and sent.
   Before the arity fix (previous entry) this run would have died with a
   TypeError before reaching the node.
2. **The node re-challenged with a bare 402.** That body carries no reason.
3. **The balance check was skipped.** `mainnet.base.org` returned an
   HTTPError from Cloud Shell, so the script proceeded without knowing
   whether the paying wallet holds any USDC on Base. The most ordinary cause
   of a rejection — an unfunded payer — is therefore **unverified either
   way.** Check it before anything else, in a browser, no deploy needed:
   `https://basescan.org/address/0x5bcea6496599D65E432E50340056194D92F95d06`

**Why the reason is unknown: the node discarded it.** `verify_only_sync`,
`verify_and_settle` and `settle_sync` each ended in `except Exception:
return None/False` with no log line, and an `is_valid=False` from the
facilitator returned the same way. The facilitator's `invalid_reason` — or
the exception that stopped verify from ever reaching the facilitator — existed
inside the process for a few milliseconds and was dropped. The Cloud Run log
had nothing. That is the #61 silent-bounce, one layer in: from the outside a
refused payment looked like nobody buying, and this made it look like nothing
from the inside too.

Fixed: every fail-closed return now logs first, at WARNING, with the stage,
facilitator URL, price, and either the facilitator's `invalid_reason` /
`invalid_message` / `payer` (fields read off `x402.schemas.responses`, not
recalled) or the exception type and text. A refusal and an outage now read
differently, because they need different fixes. The return values are
unchanged — fail-closed is the contract; the log is what was missing.

Four tests, proved by silencing the logging and watching all four go red.
The suite also caught a bug in the first draft of the fix itself: a format
string with one `%s` given two values raised inside the logger, and the
wrapper reported it as "FAILED before the facilitator could answer" — the
exact misreport its own docstring warns about. Worth noting because it is the
kind of bug a green run cannot see and a mutation run can.

**After deploying this, the next `first-paid-call.sh` leaves its reason
here** (untested from the sandbox — no gcloud; the shape is the standard one):

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="hubvibe" AND textPayload:"x402"' --project=resolver-time --freshness=1h --limit=10 --format='value(textPayload)'
```

## 2026-09-02: the owner's Base wallet IS the recipient — do not pay from it

The owner's wallet is `hubvibe.base.eth` →
`0x837c40e2b4e976f43ffb4451ee281a00fa9477dd`. Compared programmatically
against `DEFAULT_X402_PAY_TO` in `go-live.sh`: **the same address**, differing
only in EIP-55 checksum case.

That is correct as the *recipient*. It is a trap as the *payer*, and the trap
is one paste wide: it is the only Base wallet the owner has, so it is the
obvious thing to put in `HUBVIBE_WALLET_KEY` — which is the paying wallet.

**Nothing caught it.** Verified by booting a node whose `payTo` was the
payer's own address and running `first-paid-call.sh`: the x402 client
produced a signature without complaint. The failure would have landed at the
facilitator, on the one call whose entire purpose is to prove the facilitator
settles. `exact` has the payer sign an EIP-3009 `transferWithAuthorization`
from → to; with from == to that is a degenerate self-transfer nothing here has
ever tested, so whatever came back would say nothing about whether a real
buyer can pay — while consuming the bootstrap attempt.

Guarded now, before any spend, case-insensitively (a wallet app's copy button
yields lowercase; the config is checksummed). Overridable with
`HUBVIBE_ALLOW_SELF_PAYMENT=1`, because a self-transfer may well be valid and
forbidding a deliberate attempt is not the script's call — forbidding an
accidental one is.

**The correct shape: pay from a second wallet, receive into `0x837C…77dd`.**

```bash
bash scripts/first-paid-call.sh --new-wallet   # prints an address to fund
```

Fund that with ~$1 of USDC on Base (no ETH — the facilitator pays gas) and
re-run without arguments. The $0.03 lands in the owner's wallet.

## 2026-09-02: the first paid call could NOT have been constructed — x402 client arity

Found by booting the service locally and running `first-paid-call.sh` against
it with an unfunded throwaway key. It died before any signature existed:

```
HubVibeError: Could not construct an x402 payment:
  x402HTTPClientSync.handle_402_response() missing 1 required
  positional argument: 'request_url'
```

Both signatures read off the installed packages, not from memory:

| x402 | `handle_402_response` |
|---|---|
| **2.18.0** — pinned in both `requirements.txt` | `(headers, body)` |
| **2.21.0** | `(headers, body, request_url)` — **required** |

`integrations/hubvibe_tollbooth.py` passed two arguments unconditionally.

**Why the pin did not protect the thing that spends money.**
`scripts/first-paid-call.sh` shells out to bare `python3`, which resolves to
whatever x402 the machine has rather than the pinned one — in the build
sandbox, 2.21.0. And this module *ships to agent authors* who install x402
themselves. So the one script whose whole job is the first real payment, and
every third-party agent, ran on an unpinned client.

**And the failure shape is the one this file already has three entries about.**
The TypeError is raised inside the caller's process, before a signature
exists. Nothing reaches the facilitator, nothing lands on-chain, and from the
server side it is pixel-identical to nobody buying — #61 rebuilt, one layer
out.

Fixed in `_sign_402()`: the arity is read off the installed callable with
`inspect.signature` and `request_url` passed only when the parameter exists.
Introspection rather than `except TypeError`, because a TypeError raised from
*inside* the library would otherwise be retried with different arguments and
misreported as an arity problem.

Proved twice by mutation — pinning it to two arguments turns the 2.21 test
red; to three arguments turns three tests red, including the pre-existing
payment test.

**What the live re-run does and does not show.** The same
`first-paid-call.sh` invocation now gets past construction: a signature is
produced, sent, and the node answers 402 again. The failure moved from
*"could not construct an x402 payment"* to *"payment was rejected"*, which is
the only claim this run supports. **Why it was rejected is NOT established**
— the sandbox cannot reach `facilitator.xpay.sh` (the Base RPC check in the
same run failed with URLError), so the node's verify call cannot have
succeeded and the rail correctly failed closed. Do not read the rejection as
evidence about the wallet, the facilitator, or settlement. An earlier draft
of this entry attributed it to the unfunded wallet; that was a guess and the
log does not support it.

**Not proven, and still only money can prove it:** that a funded payment
settles. Construction was the blocker; settlement is still untested.

## 2026-09-02: `go-live.sh` ran against the live node — 37 passed, 0 failed

The owner ran, from Cloud Shell, on `main` @ `2d0af7c`:

```bash
cd ~/HubVibe-deploy4 && git fetch origin main && git reset --hard origin/main && bash scripts/go-live.sh
```

then `bash scripts/verify-live.sh` → **37 passed, 0 failed**. That is the
first run of the one-command go-live against the deployed service, and the
first clean checker run since the rails were reconfigured.

**Do not turn 37 into a threshold.** A previous session told the owner that
"under 38 checks is a stale checkout" — a number that was never counted and
does not exist. The checker's total is `PASSES + FAILURES`: it is however many
checks *executed*, and that moves with which rails are configured (36 was the
x402-off total; x402 being on runs more). A count compared against a
remembered constant is exactly the reasoning this file already warns about
twice, applied to the checker instead of the service.

**The staleness signal is the checker's own, and it is not a number.**
`verify-live.sh` fetches `origin/main` and prints a red `STALE CHECKOUT  this
copy is N commit(s) behind` banner, plus `This was the OLD checker` after the
totals. Read for that banner. Its absence is the proof the run is current;
the integer is not.

**Still unproven, and only money proves it:** whether a payment settles. A
green checker means the 402 is well-formed and the rails are advertised —
never that anything paid. Revenue is still zero.

## 2026-09-01: OWNER FACT — Stripe does NOT do x402. Stripe does MPP.

Stated by the owner. It is a fact about what Stripe sells, not a preference,
so do not re-derive it, and do not open a Stripe support thread about x402:

> No, stripe does not do the four zero two. You have to do that somewhere
> else. Stripe will... you have to go get that facilitated somewhere else.
> Stripe will only do the MPP.

The split is now clean and there is nothing left in the repo arguing
otherwise:

| | who runs it | recipient | money ends up |
|---|---|---|---|
| **MPP** (`tempo`, `stripe`) | Stripe | Stripe-custodied deposit address | Stripe balance |
| **x402** | a facilitator (xpay.sh) | a Base wallet you hold the key to | on-chain, yours |

**Two live traps were removed, and the first one was sitting directly in the
go-live path.**

1. `go-live-x402.sh` minted a Stripe-custodied Base deposit address whenever
   no pay-to was set, and when that failed its error said *"ask Stripe support
   to turn on machine payments / x402"*. That is advice for a product Stripe
   does not sell, so following it costs a support thread that cannot resolve —
   the same shape of wrong diagnosis that already cost this project weeks on
   the CDP business review. The mint is gone; with no address it now names the
   fact and stops. An address supplied by hand is also read *first* now,
   because a step that can only fail must not run ahead of one that succeeds.
2. `scripts/x402-setup.py` opened by arguing *"why route x402 through Stripe
   rather than your own wallet"* and defaulted to `--network base`. Both were
   wrong, and the name made it hard to notice. It now defaults to `tempo`,
   prints `MPP_TEMPO_RECIPIENT_ADDRESS`, and says plainly what it is not for.
   The filename is kept so `git log` stays followable.

**What this does NOT change:** the tempo mint in `go-live.sh` is untouched and
still correct — `/v1/crypto/deposit_addresses` is Stripe's, and MPP tempo is
Stripe's rail. What died is using that endpoint to produce an *x402* pay-to.

**And the consequence already recorded below still holds, now for a second
reason:** x402 revenue will not appear in Stripe. The wallet is the counter.

## 2026-09-01: both rails go live in ONE command, and no gate will re-bless `0x2b3b…`

Two things, and the second is the one that had teeth.

**`scripts/go-live.sh` turns on every rail that can settle, in one deploy.**

```bash
bash scripts/go-live.sh
```

There were already two go-live scripts, one per rail, and each ends by
exec'ing `repair-and-deploy.sh`. Running both meant two source deploys, two
waits, and a window in between where one rail was live and the other was in
whatever state the first script left it. The new script resolves both
recipients first, writes them in a single `services update`, and deploys once.

- **x402** defaults to the owner's affirmed Base wallet
  (`0x837C…77dd`) — no address to paste on a phone. `X402_PAY_TO_ADDRESS=0x…`
  overrides it.
- **mpp-tempo** reuses a usable recipient already on the service, else mints a
  Stripe crypto deposit address. `MPP_TEMPO_RECIPIENT_ADDRESS=0x…` skips
  minting.
- **The rails are independent.** A failed Stripe mint leaves tempo off and
  still takes x402 live — an all-or-nothing script would trade the revenue on
  one rail for tidiness. `RAILS=x402` / `RAILS=tempo` narrows it.
- mpp-stripe is absent by design: Stripe's SPT floor is 50c and no route here
  is close. That is gated on the amount in code, not on a deploy.

**Shape is not ownership, and three gates were still saying otherwise.**

`0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256` is `0x` + 40 hex. It is not the
zero address. It passes the #46 guard, the preflight, and every check in this
repo — and the owner does not recognise it. It was sitting deployed as
`X402_PAY_TO_ADDRESS`, which is why the entry below turns the rail off.

The part nobody had noticed: **a bare `bash scripts/go-live-x402.sh` would
have put it straight back.** That script reuses whatever is deployed if it is
well-formed — `ok "already set and well-formed"` — and only an explicit
override replaces it. So the documented recovery command was safe, and the
undocumented one silently re-blessed a stranger's wallet. Same failure as the
zero address exactly: a format check answering a question nobody asked.

Both that address and the test-suite constant `0x32b08c…22bc` (which exists to
make the rail inspectable on a local boot, and whose key nobody holds) are now
named and refused in all three places that can put an address on a revision:

| | on finding one |
|---|---|
| `go-live.sh` | refuses it, uses the affirmed wallet instead |
| `go-live-x402.sh` | refuses it, mints or takes an override instead |
| `repair-and-deploy.sh` | **strips** `X402_PAY_TO_ADDRESS` + the facilitator |

`repair-and-deploy.sh` removes rather than refuses, and the reasoning is the
opposite way round from the malformed case: refusing leaves the *running*
revision advertising the address, so stopping is the option that keeps money
pointed at a stranger for longer. Stripping it turns the rail off on the next
revision — which is exactly the manual `--remove-env-vars` command below,
now automatic.

One fixture had to move: `tests/test_preflight.py` used `0x32b08c…` as its
*good* address. Keeping it would have asserted the opposite of what the script
now does.

23 tests, each proved by reintroducing the bug and watching it go red —
including the pre-fix "reuse it if it's well-formed" branch, which turns the
replacement test red on its own. Suite: **491 passed, 1 skipped** (468 on
`main` before this; the 487 in circulation is PR #75's branch, not `main`).

**Still true, and this changes none of it:** nothing has been deployed, no
rail is live, and no payment has ever been made. This shortens the command
that changes that from three to one.

## 2026-08-29: the x402 recipient is UNIDENTIFIED. Turn the rail off.

**Superseded twice — read the 2026-09-01 entry above and the "recipient is
RESOLVED" entry below first.** The address is still unidentified and still
must never be advertised; what changed is that the rail no longer has to stay
off to achieve that (there is an affirmed wallet), and that turning it off is
no longer a command anyone has to remember.

Read this before touching anything about x402.

The deployed `X402_PAY_TO_ADDRESS` is
`0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256`, **and the owner does not
recognise it.** It appears nowhere in this repo. It was not minted by
`scripts/x402-setup.py` against Stripe (that account exposes only
`source_type: card`). Nobody knows who holds the key.

Nothing has been lost — zero payments have ever been made — so this is
entirely prospective risk. It is also the worst possible shape of risk: the
node was advertising a payable rail pointing at an address the operator
cannot claim. Advertising a rail that cannot settle is the rule this codebase
exists around; advertising one that settles *to a stranger* is worse.

**The rail is off until the recipient is identified.** One command, from
Cloud Shell, and it takes effect on the next revision:

```bash
gcloud run services update hubvibe --project=resolver-time --region=us-south1 --remove-env-vars=X402_PAY_TO_ADDRESS,X402_FACILITATOR_URL
```

Both variables, not just the address. `is_configured()` needs both, so
removing either one turns the rail off — but `repair-and-deploy.sh` preflight
*fails the deploy* when a facilitator is set with no pay-to address (it reads
as a half-configured rail, which is normally exactly right). Removing the
pair leaves the preflight saying `x402 is not configured` and deploys.

Read the whole env before and after, rather than a grep of it:

```bash
gcloud run services describe hubvibe --project=resolver-time --region=us-south1 --format='yaml(spec.template.spec.containers[0].env)'
```

**Check `MPP_TEMPO_RECIPIENT_ADDRESS` in that output too.** It is the other
variable on this service that names a crypto recipient. If it holds the same
unrecognised address, the tempo rail is pointing at the stranger as well and
has to come off in the same breath; if it holds a Stripe crypto deposit
address, that is money landing in the Stripe balance and it stays.

### What `verify-live.sh` should show afterwards

`36 passed, 0 failed` — the same total as before, with four lines changed in
wording, not in colour. x402 being off is now a state the checker knows how
to assert rather than one it reports as failure (that change is in this
branch; an older checkout will show two red lines instead):

```
  x402 rail per the manifest: off
  PASS  x402 is OFF and accepts[] is empty -- no unsettleable rail is advertised
  PASS  x402 is off and no v2 PAYMENT-REQUIRED header is sent
  PASS  402 does not advertise an unpayable x402 rail
  PASS  x402 is off and the 402 carries no Bazaar record (nothing to index, correctly)
```

and under "Live payment rails advertised by the manifest", **no `x402`**.

Before the branch is merged and deployed, the same thing is visible directly:

```bash
curl -sS -D- -X POST https://hubvibe-831480473793.us-south1.run.app/audit/wcag -H 'Content-Type: application/json' -d '{"url":"https://example.com"}'
```

`"accepts":[]`, no `payment-required:` header, no `"extensions"`.

The checker now fails, loudly, if any *one* of those surfaces still sells
x402 while the manifest says it is off — an orphaned v2 header alone is
enough to keep the rail live for a v2 client, since it reads that header
before it reads the body.

### To settle who owns the address

One question, to Stripe support: *"Does account `acct_1U28tvDA21T9EAQB` own
or custody the address `0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256` — as a
crypto deposit address, an onramp/payout destination, or anything else — and
if so, when and by which API call was it created?"*

Faster, and worth doing first: the account **does** have stablecoins enabled
(confirmed in the Dashboard on 2026-08-29), so the crypto deposit-address API
is live for it and lists what it has minted:

```bash
curl -sS https://api.stripe.com/v1/crypto/deposit_addresses -u "$(gcloud secrets versions access latest --secret=SECRET_STRIPE_KEY):" -H "Stripe-Version: 2026-07-29.preview"
```

If `0x2b3b...` is in that list, it is Stripe-custodied, the money was always
going to the Stripe balance, and the rail can go back on unchanged. If it is
not, it stays off.

## 2026-08-29: mpp-stripe cannot serve a 3-cent call, and now says so

`MPP_STRIPE_NETWORK_PROFILE_ID` is
`profile_61VBesDFKefGw7DD7A6VBesDRXSQjgSvGV9E4a316JTc` (the account's Stripe
profile ID). Setting it is *not* enough to make the rail live, and this is
the useful finding: **Stripe requires a minimum 0.50 USD charge for card
payments made with a Shared Payment Token.** Every route here is $0.03 or
$0.10. Confirmed twice over — Stripe's own MPP docs, and Stripe's Dashboard
assistant on this account, which also gives the way around it: stablecoin
payments through MPP have a **1 cent** minimum, and stablecoins are enabled
on this account.

So the SPT rail is now gated on the amount, not just on configuration
(`stripe_available_for`, floor `MPP_STRIPE_MIN_CENTS`, default 50). Below the
floor it is not challenged, not in `accepts`, not in `payment.methods`, and a
stale under-floor challenge is refused before a caller's single-use token is
spent on it. Set the profile ID anyway — it costs nothing and the rail is
ready the moment something here is priced at 50c+ — but do not expect it to
appear in the manifest at today's prices. That absence is the fail-closed
rule working, not a misconfiguration.

**The rail that can take $0.03 through Stripe is the stablecoin one**: the
MPP `tempo` method with `MPP_TEMPO_RECIPIENT_ADDRESS` set to a Stripe crypto
deposit address, which Stripe offramps into the Stripe balance. See
`wcag-audit-engine/README.md` for the deposit-address call.

(The Node snippet the Dashboard assistant offers is not usable here — this
service implements MPP directly in Python because there is no Python SDK —
and it multiplies by 100 an amount that is already in cents, which would
charge $3.00 and $10.00 rather than 3c and 10c.)

## 2026-08-29: the Bazaar blocker is the FACILITATOR, and it is now measurable

Two weeks in, revenue is zero, and the reason is narrower than "demand": the
node is payable and **invisible**, because the facilitator it pays through
keeps no Bazaar index.

Established from the x402 spec repo (`coinbase/x402`, cloned and read, not
recalled):

- A facilitator catalogs a resource **when it receives a `PaymentPayload`
  carrying the bazaar extension** (`specs/extensions/bazaar.md`, "Facilitator
  Behavior"). How it stores and exposes that is explicitly an implementation
  detail — so whether any given facilitator indexes at all is a fact to be
  measured, not assumed.
- The index is read back at `GET /discovery/resources`
  (`specs/x402-specification-v2.md` §8.1), answering with an `items[]` array.
- **The protocol is permissionless.** From the FAQ, verbatim: *"Multiple
  organizations operate production facilitators. The protocol is
  **permissionless**—anyone can run a facilitator."* Community-run
  facilitators are listed alongside private ones "for enterprises that need
  custom KYT / KYC flows". **x402 is not gated on a business account.** CDP's
  DBA review is one facilitator's policy, not the protocol's, and treating it
  as the protocol's cost this project weeks.
- The ecosystem list is at `https://www.x402.org/ecosystem?filter=facilitators`
  (not reachable from the build sandbox; reachable from Cloud Shell).

So the question that decides Bazaar listing is: **which facilitator both
settles Base mainnet AND keeps an index?** `facilitator.xpay.sh` settles and
does not index — that combination is precisely why this node is payable and
uncatalogued.

`scripts/probe-facilitators.sh` answers it with evidence. GETs only, no
wallet, no money moved; run it from Cloud Shell:

```bash
bash scripts/probe-facilitators.sh
```

It reports, per candidate, whether `/supported` offers Base mainnet (CAIP-2
`eip155:8453` **or** the legacy name `base` — v1 clients use the latter) and
whether `/discovery/resources` returns a real `items[]` index. It names the
winner and prints the one command to switch to it.

**The trap it exists to avoid:** xpay.sh answers `/discovery/resources` with
**HTTP 200** and `{"message":"Not Found"}`. A status-code check calls that an
index and sends the next session chasing a listing that can never appear —
the same form-not-function error as the zero address, the Bazaar record, and
the unpayable 402. The probe reads the body; a test proves it by reverting to
a status-only check and watching it go red.

Add candidates from the ecosystem page as arguments (they are appended to the
built-in list, not substituted):

```bash
bash scripts/probe-facilitators.sh https://candidate-one https://candidate-two
```

A facilitator that answers 401/403 is reported as needing credentials rather
than as a failure — permissionless means such a facilitator may still hand
out credentials without a business review, and
`X402_FACILITATOR_AUTH_HEADERS` already exists to carry them.

## 2026-08-29: OWNER DECISION — one rail per network. MPP is Stripe. Base is x402.

Stated by the owner, and it settles an architecture question rather than a
preference, so do not re-propose the alternative:

> the mpp is not base. that was stripe. base is strictly for the crypto.

So:

- **MPP = Stripe.** Both of its methods, and this is the part an earlier
  draft of this entry got wrong by implying `tempo` was something other than
  Stripe. MPP was co-authored by Stripe and Tempo, and Stripe's own MPP stack
  uses Tempo for its stablecoin half — Stripe mints the deposit address,
  offramps the USDC, and settles it into the Stripe balance:

  | method | rail | minimum | lands in |
  |---|---|---|---|
  | `stripe` | SPT / cards | **50c** | Stripe balance |
  | `tempo` | USDC on Tempo | **1c** | Stripe balance (auto-offramp) |

  At $0.03–$0.10, `stripe` is below its floor and `tempo` is the Stripe rail
  that works. `MPP_TEMPO_RECIPIENT_ADDRESS` must therefore be a
  **Stripe-managed Tempo deposit address** (`/v1/crypto/deposit_addresses`,
  `network=tempo`) — not a self-custody wallet, or the offramp-into-Stripe
  property is lost. Every other tempo default in the code is already correct
  for Tempo mainnet; the recipient is the only value to set.

- **Base = x402 only.** Self-custody wallet, USDC stays USDC, facilitator
  settles, money stays on-chain.

**A previous suggestion in this file to point the MPP `tempo` method at Base
is withdrawn.** It validated (75 server-side checks passed against chain
8453) and the code genuinely is chain-agnostic, so the note stays as a
technical fact — but it is NOT the chosen architecture. It advertises
`method="tempo"` while naming a Base chain id, which is off-label, and it
duplicates on Base what x402 already does natively there. Nothing enables it
unless someone sets those env vars. Do not set them.

**Base being a Coinbase-built L2 does not drag Coinbase CDP back in.** That
distinction matters, because the CDP review is what blocked this project for
weeks. `facilitator.xpay.sh` is keyless and settles on Base mainnet with no
Coinbase account, no API key, and no business verification. Using the Base
*chain* requires nothing from Coinbase the *company*.

## 2026-08-29: the x402 recipient is RESOLVED — a self-custody Base wallet

The owner created a Base wallet: `0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd`.
Verified 0x + 40 hex, not the zero address, and it appears nowhere in this
repo (so it is not a test constant recycled by accident — the fault that had
just been found in the tempo recipient).

**This replaces the unidentified `0x2b3b…0256` and unblocks the x402 rail.**
It does not identify that address, and does not need to: the rail now points
somewhere the owner holds the key to. Leave `0x2b3b…` off the service. If the
Stripe deposit-address list ever explains it, that is bookkeeping, not a
blocker.

Turning it on is one command — `go-live-x402.sh` shape-checks the address,
sets it with the facilitator, and hands off to `repair-and-deploy.sh` for the
source deploy:

```bash
cd ~/HubVibe-deploy4 && git fetch origin main && git reset --hard origin/main
X402_PAY_TO_ADDRESS=0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd bash scripts/go-live-x402.sh
```

**One consequence to internalise: x402 revenue will NOT appear in Stripe.**
The #47 PaymentIntent mirroring only fires for a *Stripe-custodied* deposit
address, because Stripe verifies the transaction against its own address. A
self-custody wallet settles on-chain and stays there. "Is the Stripe balance
going up" stops being the measure of x402 income; the wallet is. That is a
fair trade for a recipient whose key the owner actually holds, but it must not
be mistaken later for payments failing.

### On-chain verification: the pattern is right, the Basescan API is not

The plan (from Stripe's own Dashboard assistant, and its strategy is sound —
a 3-10c call genuinely cannot go through Stripe cards, whose SPT floor is
50c) was: accept direct USDC to the Base wallet, verify it on-chain, return a
receipt. Secret `basescan_etherscan` holds a Basescan API key for it.

**The pattern is legitimate and already implemented here.** The MPP `tempo`
push-mode path is exactly "caller broadcasts a transfer and hands over the
hash, server verifies it and returns a receipt" — with the two things a
hand-rolled version would omit: an HMAC-bound per-request challenge
(`_verify_challenge_binding`) and a spent-hash ledger (`_used_credentials`).

**And it is not Tempo-specific.** Verification is `eth_getTransactionReceipt`
over JSON-RPC plus standard ERC-20 `Transfer` log matching; four env vars are
all that tie it to a chain. Pointed at Base (chain 8453, RPC
`https://mainnet.base.org`, USDC on Base, the owner's wallet) the reference
validator passes every server-side check on all six routes — 75 passed,
including `Valid currency address (mainnet)`. See `wcag-audit-engine/README.md`
for the exact configuration and the `eth_call` that confirms the token
address. Its 6 payment-phase failures are the validator auto-provisioning a
*Tempo testnet* wallet to pay with, which a Base mainnet config cannot use;
not a server fault.

**What must NOT be built is the Basescan API as the payment gate.** Three
reasons, in order of severity — and note the first two are about a *bespoke
explorer check*, not about on-chain verification as such, which the RPC path
above does correctly:

1. **Bolting it onto x402 specifically would make x402 unpayable.** x402's
   exact-EVM scheme has the client sign an EIP-3009
   `transferWithAuthorization` *off-chain* and send the signature in
   `X-PAYMENT` / `PAYMENT-SIGNATURE`; the facilitator verifies and submits
   it, paying the gas. An x402 client never broadcasts a transfer, so there
   would be nothing for an explorer lookup to find — the #61 failure
   (advertised, unpayable, invisible from this side) rebuilt on purpose. The
   caller-broadcasts flow belongs to MPP, where it is already implemented.
2. **It is replay-vulnerable.** "A transfer of the right amount to the right
   address exists on-chain" is not proof that *this request* was paid for.
   Without binding the payment to a per-request challenge and keeping a spent
   ledger, one real payment authorises unlimited calls, by anyone who can read
   a block explorer. The MPP tempo path does this correctly
   (`_verify_challenge_binding` + `_receipt_matches` + `_used_credentials`);
   a hand-rolled Basescan check would have none of it.
3. **It puts a third-party block-explorer index on the paid path**, with its
   rate limits and its indexing lag, in place of the facilitator whose entire
   job this is.

**CORRECTION (same day, after reading the spec instead of asserting):** an
earlier version of this entry said "verification is the facilitator's role by
design." **That is wrong, and it was repeated three times before being
checked.** The x402 docs are explicit — `docs/core-concepts/facilitator.md`:

> The facilitator is an **optional but recommended** service…

and the interaction flow makes both halves optional, step by step:

> 5. `Resource server` verifies the `Payment Payload` is valid **either via
>    local verification** or by POSTing … to `/verify`
> 8. `Resource server` **either settles the payment by interacting with a
>    blockchain directly**, or by POSTing … to `/settle`

So a merchant can verify and settle entirely on its own. What self-settlement
actually involves, from `specs/schemes/exact/scheme_exact_evm.md`: the
settling party calls `transferWithAuthorization` on the USDC contract with the
client's signature and **pays the gas** — "they serve only as the transaction
broadcaster." That means a hot private key on Cloud Run and ETH on Base for
gas, and it means the spec's duplicate-detection requirement becomes ours
(EIP-3009 nonces are one-time-use at the contract, which covers it).

**And it gives an explorer read a legitimate job.** Once *we* broadcast, there
is a transaction hash, and confirming it landed is step 10 of the flow. That
is a real use for `basescan_etherscan` — confirming a transaction we
submitted. What an explorer cannot do is find a payment a client sent
unprompted: in `exact` the client transmits a *signature*, never a broadcast
transfer, so before settlement there is nothing on-chain to look up. The
distinction is confirmation-after-broadcast versus discovery-before, not
"explorers are wrong."

**The tension that decides the choice, and it is sharp:** Bazaar cataloging
happens when a **facilitator** receives the `PaymentPayload`
(`specs/extensions/bazaar.md`). Self-settling means no facilitator sees the
payload, so **self-settlement forfeits the Bazaar listing entirely.** The two
goals pull opposite ways:

| | facilitator | self-settle |
|---|---|---|
| gas | facilitator pays | **you pay** (ETH on Base) |
| private key on Cloud Run | none | **required** (hot key) |
| Bazaar listing | yes, *if it indexes* | **never** |
| blocked on | finding one that indexes | nothing |

A hybrid exists: call a facilitator's `/verify` (which puts the payload in
front of it, so it can catalog) while settling directly. It keeps the listing
and the custody, at the cost of the gas and the hot key.

So `basescan_etherscan` stays unmounted **only while the facilitator path is
the chosen one** — on that path nothing broadcasts locally, so there is no
hash of ours to confirm. Choose self-settlement and the secret gets a real
job on day one.

### Also rejected: a `Stripe-Payment-Credential` header

MPP's wire format is `Authorization: Payment <credential>`, answering a
`WWW-Authenticate: Payment` challenge — that is what `mppx validate` exercised
75/0 against this service. A bespoke `Stripe-Payment-Credential` header would
be understood by nothing. Same class of error as the invented `accepts[]`
shape in #61.

## 2026-08-29: the tempo recipient is a TRUNCATED TEST ADDRESS, and nothing guarded it

The reference validator, run against the live node, found what none of our
own checks did. `npx mppx@latest validate` reported, on all six paid routes:

```
✗ Valid recipient address (Got: 0x32b08c5e927c69877d0fcab35618c265674922b)
  → Set request.recipient to a valid 0x-prefixed 40-hex-char address.
Summary: 69 passed, 6 failed, 6 skipped
```

That value is **39 hex characters**, and it is the test-suite constant from
this file with the trailing `c` dropped — a truncated paste. Two separate
faults, both bad:

1. **It is a test address, not a recipient.** Even at full length,
   `0x32b08c...22bc` is the shape-valid placeholder used to make the rail
   inspectable in local tests. Nobody holds its key. It must never be a
   deployed recipient.
2. **Nothing on this side checked.** `tempo_configured()` was
   `bool(_TEMPO_RECIPIENT_ADDRESS)` — any truthy string turned the rail on.
   The x402 rail shipped a 16-hex address, and later the zero address,
   through exactly this gap; the guard added after those incidents was never
   extended to the tempo recipient, which carries real money too. `mppx
   validate` caught in one run what three of our own green runs could not.

**Fourth instance of the same lesson: test it with the consumer's parser.**
The Bazaar record, the unpayable 402, the zero address, and now this.

Fixed in both places the x402 address is already guarded:

- `mpp_payments._tempo_recipient_is_usable()` — 0x + exactly 40 hex, zero
  address rejected explicitly, logged at ERROR once. Verified: booted with
  the truncated value, the node emits **no** WWW-Authenticate challenge, no
  `x-payment-info`, and `payment.methods` drops to `["stripe_api_key"]`.
- `repair-and-deploy.sh` preflight — same check as the x402 address, blocks
  the deploy rather than shipping a rail that cannot settle.

**Consequence, and it is the honest one: this node currently advertises NO
machine payment rail.** x402 is off (unidentified recipient), mpp-stripe is
below Stripe's 50c SPT floor, and tempo now fails closed on the truncated
address. That is correct — every rail it could advertise is one that cannot
settle — and it is also the whole remaining blocker to a first paid call.

**To take money again, mint a real Stripe crypto deposit address** (stablecoins
are enabled on the account; Stripe custodies it and offramps into the Stripe
balance, so there is no wallet to run and no key to lose):

```bash
curl -sS https://api.stripe.com/v1/crypto/deposit_addresses -u "$(gcloud secrets versions access latest --secret=SECRET_STRIPE_KEY):" -H "Stripe-Version: 2026-07-29.preview" -d network=tempo
```

Set the returned `address` as `MPP_TEMPO_RECIPIENT_ADDRESS`, deploy with
`repair-and-deploy.sh` (its preflight now refuses a malformed one), and
re-run `mppx validate` against the deployed node. That same GET, without
`-d network=tempo`, also lists addresses the account already holds — which
is how to settle whether `0x2b3b…0256` was Stripe-custodied all along.

## 2026-08-29: the MPP rail was invisible to MPP's own tooling — now validated 75/0 against the reference implementation

The rail the first paid call must now ride (MPP tempo — see the mpp-stripe
floor entry above) had never been checked against anything but our own tests.
Ran `npx mppx@latest validate` (the protocol's reference implementation)
against a locally booted node. Two findings:

1. **Discovery was empty.** MPP tooling discovers paid endpoints from
   openapi.json: an operation is payable iff it carries `x-payment-info`.
   Ours carried none, so the validator reported `endpoints: []`, skipped its
   entire challenge suite, and any MPP-aware agent walking our OpenAPI doc
   saw a service with zero paid endpoints. Same lesson as the Bazaar record —
   a surface consumed by someone else's parser must be shaped for their
   parser — but unlike the Bazaar this surface is entirely ours: no
   facilitator involved. Fixed: every catalog route (+ the /audit alias) now
   carries `x-payment-info` with offers gated exactly like the challenges
   (tempo at any amount, stripe only at/above its floor, nothing when no rail
   is configured), a declared 402 response (the spec requires it), and a
   body example (the validator derives its probe body from it; without one
   its schema-generated guess failed the html-or-url anyOf and three routes
   read as broken). Root `x-service-info` added, relative paths only.

2. **The hand-rolled challenge format is right.** With discovery fixed, the
   reference validator passes **75/0 across all six paid routes**: challenge
   parseable, realm binding, expiry, recipient, currency, integer amounts,
   malformed credential → 402-not-500, fresh challenge on error. First time
   this implementation has ever been validated against the reference. The 6
   skips are the live payment roundtrips — sandbox has no wallet.

**The remaining skip is the first-paid-call path for MPP.** In Cloud Shell,
against the deployed node (after deploy):

```bash
npx mppx@latest validate https://hubvibe-831480473793.us-south1.run.app
```

reruns everything above live, and with a funded wallet (`npx mppx@latest
account create`, fund with USDC on Tempo) completes a real roundtrip — a real
$0.03 settlement through the tempo rail. That is the MPP equivalent of
`first-paid-call.sh`, using the protocol authors' own client. Precondition:
`MPP_TEMPO_RECIPIENT_ADDRESS` must be a Stripe crypto deposit address you
have verified (see the recipient-audit entry above — do NOT run a paid
roundtrip at an unverified recipient).

## 2026-08-29: API-key calls metered a third of what they charge

`billing.record_usage` reported **one meter unit per call** against a Price
of **$0.01 per unit**. A $0.03 audit metered $0.01; the $0.10 bundle sent 3
units and metered $0.03. Every invoice this would ever have produced was for
roughly a third of the money owed.

It has cost nothing so far, for the same reason it was invisible: the human
plans are `licensed` flat Prices with no metered item on the subscription, so
the events were accepted, aggregated, and charged to nobody. It would have
started under-billing on the day someone attached that Price — which is
exactly the moment nobody is auditing the arithmetic.

Fixed by metering the price rather than the call: `record_usage` now takes
`price_cents`, and routes pass their own rate (`_bill(auth, price_usd=...)`,
no default, so a route cannot forget). Two Stripe-side facts it depends on
are now variables rather than assumptions, because getting either wrong is a
silent uniform mis-bill:

- `STRIPE_METER_UNIT_CENTS` (default `1`) — what one unit costs on the
  attached Price. **Reconcile the Price before attaching it.**
- `STRIPE_METER_AGGREGATION` (default `count`) — `count` ignores the event
  value, so N units means N events; `sum` reads the value, so it is one
  event. A meter's formula cannot be edited after creation. The live meter is
  `mtr_61VBfeoxeQjPttsXD41DA21T9EAQBOA4` behind Price
  `price_1U2Hqm...` ($0.01/unit, billed daily); this session could not read
  its formula (the Stripe connector does not expose the meters API), so the
  default follows what `README.md` says it was created with. Check it and set
  the variable to match — under `count` the code sends 3 events for a $0.03
  audit, which is correct but chatty; a `sum` meter makes it one call.

## What this is

A machine-payable site auditing service. Software agents call an HTTP
endpoint, get a 402 carrying the price and how to pay, settle it, and receive
a result — no account, no signup, no human in the loop. There is also a human
plan tier, but **the A2A machine API is the product**, not a SaaS with an API
bolted on.

The goal is revenue. Treat "does this make money / can a buyer actually pay"
as the test that outranks everything else.

- **Live service:** https://hubvibe-831480473793.us-south1.run.app
- **Public site:** https://its-fortunatefolly.github.io/HubVibe/
- **Repo:** https://github.com/Its-fortunatefolly/HubVibe (public)
- **Action repo:** https://github.com/Its-fortunatefolly/hubvibe-audit-action (public,
  generated by `scripts/publish-action-repo.sh` — Marketplace requires an
  action repo with no workflow files)

## Current state — all verified live, not assumed

| | |
|---|---|
| Cloud Run | project `resolver-time`, service `hubvibe`, region `us-south1` |
| Tests | `python -m pytest -q` — read the number off the run, do not trust a number written here. It was 292, then 313, then 322, then 325 (#51) inside two days, and a row like this is stale one merge after it is written. flake8 clean, and the count is the same in CI and locally since #44 pinned PyYAML — that agreement, not the integer, is the thing worth checking. |
| Live checks | `bash scripts/verify-live.sh` → **36 passed, 0 failed against the deployed node, 2026-08-27** (owner, Cloud Shell, post-#61/#62 deploy). This is the first run that ever proved payability: `accepts[] is spec-shaped: 1 payable x402 entry` and `402 carries the v2 PAYMENT-REQUIRED challenge header` both passed live. The deployed 402 is now constructible into a signature by a conforming x402 client — the #61 unpayability is fixed *in production*, not just on main. |
| Firestore | `(default)` in `us-south1` — created 2026-08-15; before that every keyed call 500'd |
| min-instances | `0` — was `1`, burning ~$137/mo against zero traffic |
| Stripe account | `acct_1U28tvDA21T9EAQB`, **zero outstanding requirements** |
| Payouts | daily → SUTTON BANK ····1444 |
| Webhook | `/billing/webhook`, enabled, `checkout.session.completed` |
| MCP registry | `io.github.Its-fortunatefolly/hubvibe` 1.1.0 active; `server.json` on main is **1.1.2** and not yet republished |
| Payment rails live | **`x402` is OFF as of 2026-08-29** — unidentified recipient, see the entry at the top. `mpp-stripe` is configured but below Stripe's 0.50 USD SPT floor, so it is deliberately not advertised either. That leaves `mpp-tempo` and `stripe_api_key`. |
| x402 | **OFF since 2026-08-29** (unidentified pay-to recipient — see the top of this file). Everything below is the history of how it got to being live, and stays accurate about the code; it is no longer a description of the running node. **Live per #58** — `verify-live.sh` reported 34 passed / 0 failed against the deployed node on 2026-08-25. **Do not re-derive this from the advertised-methods list.** On 2026-08-18 the deployed pay-to address was `0x` + 40 ZEROS: shape-valid, so it passed the #46 guard, the preflight, and every verify-live run, while `address(0)` is unownable and USDC reverts transfers to it — the rail was advertised and unpayable for days and nothing said so. A shape check proves shape; shape is not payability. `scripts/go-live-x402.sh` now replaces zeros explicitly, and the app and preflight both reject them. To know the current recipient, read it: `gcloud run services describe hubvibe --project=resolver-time --region=us-south1 --format=json` and look at `X402_PAY_TO_ADDRESS`. |
| x402 payability | **The 402 was unpayable by any conforming client until 2026-08-27** — `accepts[]` was a shape of our own invention missing four required fields, so the x402 library raised before signing. See the 2026-08-27 entry. Now both v1 body and v2 `PAYMENT-REQUIRED` header go out and both are proven payable against the real client. |
| x402 settle side | Facilitator is now **xpay.sh** (keyless, Base mainnet, zero fee) — CDP is abandoned, not pending: its review wants proof of a DBA that does not exist. The CDP key pair may stay mounted; since #55 those credentials only go to a Coinbase host. Settlement itself is still unproven until the first real agent payment — nothing has ever been attempted. |

### Stripe price IDs (verified against the live account)

| Plan | Price ID | Amount |
|---|---|---|
| Single report | `price_1U34JXDA21T9EAQB8IfiGxII` | $29.99 one-time |
| Pro | `price_1U34LiDA21T9EAQB3LK5dS0I` | $79.00/mo |
| Agency | `price_1U34PXDA21T9EAQB7aMyADgE` | $249.00/mo |

Machine rates: **$0.03** per single audit, **$0.10** per bundle.

## 2026-08-18: the Bazaar record every 402 emitted was rejected by the indexer

The single most expensive bug found so far, because nothing about it was
visible from this side. x402 was on, payments were settleable, and every 402
carried a `extensions.bazaar` block — and the Bazaar's own facilitator-side
validator threw all of them away.

```
validate_discovery_extension(...) ->
    ValidationResult(valid=False, errors=["input: 'method' is a required property"])
```

The record shipped an `info.input` with no `method`, alongside a `schema` —
in the same object — declaring `method` required. The x402 library says why
in its own docstring: *"The HTTP method is NOT passed to this function. It is
automatically inferred from the route key or enriched by
`bazaar_resource_server_extension` at runtime."* This service uses neither.
It hand-builds its 402 so one challenge can carry x402, MPP and the API-key
rail together, which is the right call and is exactly what left `method`
unfilled forever. A facilitator that validates before cataloguing indexed
nothing, so capability-based discovery — the entire reason the extension is
emitted — had never worked, on any route, since the day it shipped.

Fixed in `bazaar_extension_for_body`, which now names the method (`setdefault`,
so a future library version that emits it wins). Four tests now assert against
the library's own `validate_discovery_extension` rather than a hand-written
expected dict: the question that matters is not "does this look right to us"
but "does the indexer accept it". Verified against a locally booted node —
all six sellable paths (`/audit`, the four dimensions, `/audit/bundle`) plus
the MCP-tool variant emitted on a paid `tools/call` now return `valid=True`.

**This does not make discovery live, and must not be read as doing so.** See
"Bazaar discovery is NOT live" below: xpay.sh serves no `/discovery/resources`,
so there is currently no facilitator indexing anything this node emits. What
changed is narrower and still worth having — the record was *also* invalid, so
the moment a facilitator with an index does arrive, it would have discarded
every one. Two independent failures stacked; this fixes the one that is ours.
It corrects one line in that entry too: "this node's 402s carry the extension
correctly — `verify-live.sh` passes that check" was true only of the checker,
which grepped for the word `"bazaar"` and never read the record. That check
now asserts the method is named, so this cannot pass while being discarded.

**Lesson, generalised:** when a surface exists to be consumed by someone
else's validator, test it with *their* validator. Everything about this bug
looked correct locally and in every prior test.

### Same session: /mcp.json told crawlers there was no MCP server

`note` read *"This service is a plain HTTP/REST API, not a live MCP stdio/SSE
server … wrap these with an MCP-to-HTTP adapter."* True when written; false
since `/mcp` shipped. `server.json` was meanwhile advertising `/mcp` as a
`streamable-http` remote, so the two files a registry reads contradicted each
other, and the one that said "no server here" is the one Glama, Smithery and
MCP.so fetch. `/mcp.json` now carries `remotes[]`, `version`,
`documentationUrl`, `repository` and `icons`, and the note describes both
transports as alternatives to one catalog. The served copy rewrites every
absolute URL from `PUBLIC_BASE_URL`, so a self-hosted copy cannot hand its
clients the production endpoint.

Also folded in: `SERVICE_VERSION`, one constant behind the OpenAPI spec, the
MCP handshake's `serverInfo` and `/mcp.json`. Three literals had drifted to
two values — the registry said 1.1.2 while every client that completed an MCP
handshake was told 1.1.0. And `sitemap.xml` had omitted `/mcp.json` and
`/openapi.json`: the file registry crawlers come for was not in the file that
tells crawlers what to fetch.

## 2026-08-18: the discovery contract — five gaps closed

All five were the same shape: a machine-readable surface that told an agent
something *different* from what another surface said, or said nothing at all
where an agent needed a fact to act. None of them 500s. Each one ends with an
agent that does not call, and from this side that is indistinguishable from
nobody wanting to buy — which is the failure mode this business can least
afford to misread.

Each was verified by booting the service locally with every rail configured,
running `verify-live.sh` against it, then booting the **pre-fix** code on a
second port and re-running the same script: **33 passed / 0 failed** on the
fix, **28 passed / 5 failed** on `origin/main`. The five FAIL lines are the
five gaps. (33 not 34 because the paid-path check reports NOTE rather than
PASS on a local box with no Playwright browsers — auth succeeds, the audit
itself 502s. That branch is loud, not silent, which is the point of it.)

**None of this was verified against the deployed Cloud Run node.** This
session's sandbox is denied `*.run.app` at the network policy (`CONNECT
tunnel failed, response 403`), so the deploy-and-verify step is still owed —
see *How to deploy* below. Local agreement is not a deploy; that lesson is
already in this file twice.

1. **`accepts[]` omitted the API-key rail.** `/.well-known/agent.json` listed
   `stripe_api_key` in `payment.methods`; the 402's `accepts[]` — the array an
   agent actually iterates — did not. A CI pipeline holding a pre-funded key
   had to parse prose out of `alternative` to learn its key was spendable.
   Now emitted as a fourth `accepts` entry, gated on `billing.is_configured()`
   like every other rail, so an unconfigured node still advertises nothing.

2. **`/mcp.json` had drifted from `/mcp`.** The static file carried its own
   `inputSchema` copies and they no longer matched what `tools/list` serves —
   the file omitted the html-or-url either-or the routes enforce, so an agent
   reading the manifest the *MCP registry points at* could build a body the
   route rejects. `/mcp.json` now takes schemas, titles and annotations from
   `_mcp_tools()`, the same function `/mcp` answers with. The static file
   still owns the prose. Two tests hold the line, one on the served pair and
   one on the copy on disk (crawlers fetch the raw file from GitHub).

3. **Tool definitions were under-specified.** No `outputSchema` on any tool,
   no `anyOf` expressing the html-or-url rule, no annotations. An agent
   deciding whether to spend money here answers three questions from the tool
   definition alone — what do I send, what comes back, is it safe to retry —
   and all three were prose or absent. Added `outputSchema` per tool, `title`,
   and `readOnly/destructive/idempotent/openWorld` hints (all accurate: these
   read a third-party page and mutate nothing).
   **Deliberately NOT added: `additionalProperties: false`.** The Pydantic
   models ignore unknown keys, so declaring it would advertise a rejection
   that never happens — a schema stricter than the route makes a conforming
   client refuse a call that would have worked. Same reasoning as the
   fail-closed rule, pointed the other way.

4. **`/audit` was invisible to capability discovery.** It is an alias of
   `/audit/wcag` and correctly has no catalog row of its own, which left the
   shortest and most guessable paid path on the service as the one paid path
   carrying no Bazaar extension. `_CATALOG_ALIASES` maps it.

5. **`agent.json` described its inputs only as prose** (`"url": "string
   (required)"`). Nothing a crawler scoring this node or an agent generating
   a request can act on. Each endpoint now also carries `input_schema` and
   `output_schema` — the same objects the MCP tools advertise, via
   `_schema_for()`, so there is one contract rather than three.

Also: `server.json` gained `icons` (validated against the live registry
schema, which is reachable from the sandbox) and went to **1.1.2**. Two tests
guard it offline — the 100-character `description` cap whose violation is
invisible here and only shows up as a rejected publish, and that every
advertised icon URL is a path this app actually serves.

**GitHub Action.** `action.yml` was already correct at the root and needed no
change. What changed is `integrations/github_action.yml`, the file consumers
copy: it hand-rolled the HTTP call, so it duplicated — and would have to keep
duplicating — the action's retry policy, its 4xx no-retry rule (a metered
endpoint must never be paid twice for the same answer) and its JSON encoding
of the target URL. It now calls the published action, and the Marketplace
listing README carries the same complete workflow rather than a bare step
fragment. A developer copies a file, not a snippet plus the scaffolding they
have to infer.

**Every guard above was proved by reintroducing the bug and watching the test
go red, then restoring** — 12 new tests, 12 proven, per the rule below.

## 2026-08-27: the rail was live and UNPAYABLE. This is why revenue is zero.

Read this before concluding anything about demand.

**No conforming x402 client could ever have paid this service.** Not "payments
were rejected" — no payment could be *constructed*. A client hands the 402 to
the x402 library, which validates `accepts[]` against `PaymentRequirementsV1`
and raises before producing any signature:

```
ValidationError: 4 validation errors for PaymentRequiredV1
  accepts.0.maxAmountRequired  Field required
  accepts.0.resource           Field required
  accepts.0.maxTimeoutSeconds  Field required
  accepts.0.asset              Field required
```

`accepts[]` carried a shape of our own invention — `protocol`, `price`,
`pay_to`, `send_via_header` — and `pay_to` is not how the spec spells `payTo`.
The array also held the MPP and API-key rails, and the client validates *every*
entry, so those broke the challenge a second time on their own.

The failure happens **inside the caller's process**. It never reaches the
facilitator, so there is nothing to reject, nothing to log, and nothing on this
side to see. An agent that found us, wanted the audit, and had a funded wallet
would bounce with a client-side error — and from here that is pixel-identical
to nobody showing up.

**So "the constraint is demand, not plumbing" was wrong.** Both were broken.
Every discovery fix shipped so far was pointing traffic at a booth with no
coin slot.

### How it stayed invisible

`verify-live.sh` was green — 34/34 — the whole time. Every check asked whether
the 402 *mentions* x402. None asked whether it can be *paid*. Same shape as the
Bazaar bug from #52 (grep for the word `"bazaar"`), and the same shape as the
zero-address (a check that proves form, not function). Third time. **When a
surface exists to be consumed by someone else's parser, test it with their
parser** — that is now three separate bugs teaching one lesson.

### What was actually wrong, and what fixed it

The library's own parser accepts exactly two shapes:

```python
header = get_header(PAYMENT_REQUIRED_HEADER)          # v2 path
if header: return decode_payment_required_header(header)
if body and body.get("x402Version") == 1:             # v1 path
    return PaymentRequiredV1.model_validate(body)
raise ValueError("Invalid payment required response")
```

We emitted neither. Now both go out on every paid route:

- **v1 body** — `accepts[]` holds spec-shaped x402 entries only, with
  `maxAmountRequired`, `resource`, `mimeType`, `maxTimeoutSeconds`, `asset`,
  `extra`, and `payTo`. Note `network` is the **legacy name** (`base`, not
  `eip155:8453`): v1 clients register schemes by legacy name, and CAIP-2 there
  resolves to no scheme and fails as `NoMatchingRequirementsError`.
- **v2 `PAYMENT-REQUIRED` header** — where v2 puts the challenge. A client
  reads it first. It also carries `ResourceInfo.serviceName` and `.tags`,
  which are what an agent shopping the Bazaar *by capability* matches on;
  the v1 body has no field for either, so a v1-only node is at best an
  anonymous row.
- MPP and the API-key rail moved to `other_rails`. They lost nothing but the
  array they were in — MPP's real channel was always its `WWW-Authenticate`
  headers.
- The server now reads **`PAYMENT-SIGNATURE`** as well as `X-PAYMENT`. v2
  clients answer with the former; advertising v2 while reading only the latter
  would hand a client a challenge it can satisfy and then ignore the answer.

Proved by driving the real `x402HTTPClientSync` against every paid route: all
six now yield a signature, on both protocol versions. Restoring the shipped
code turns 10 tests red.

### Bazaar: item 3 was the wrong question

The open item asked whether a keyless facilitator exists that also serves
`/discovery/resources`. **Finding one would not have indexed us**, because
that is not how anything gets indexed. From the spec:

> When a facilitator receives a `PaymentPayload` containing the `bazaar`
> extension, it should: 1. Validate the `info` field against the provided
> `schema`  2. Extract the discovery information

That is the only ingestion path — no registration endpoint, no crawler.
`/discovery/resources` is read-only; it lists what payments have already
taught the facilitator. The client library does its half automatically
(`client_base._merge_extensions` copies the server's declared extensions into
the payload).

So: **a resource nobody has paid is a resource nobody can find, on every
facilitator.** Zero payments ⇒ zero index entries ⇒ zero discovery ⇒ zero
payments. Nobody breaks that from outside.

`scripts/first-paid-call.sh` breaks it from inside for $0.03: it pays our own
endpoint once, which proves the settle side (still never exercised) and
registers the resource wherever it settles. It refuses to spend if the live 402
is not payable or the Bazaar record would be discarded — there is no point
burning the bootstrap on a stale revision.

**Still unproven and only a real payment can prove it:** verify-then-settle
against the live facilitator. Building a signature is now certain; the
facilitator accepting it is not.

## 2026-08-25: the rail is live, and why it was not

`34 passed, 0 failed`. The first time the full checker has ever run clean
against the deployed service.

What had been wrong was not the code and not the config. It was that they
were never in the same place. `gcloud run services update --update-env-vars`
mints a revision carrying the **same container image**, so every fix merged
to `main` — the #48 discovery contract, the #55 CDP guard — sat in the repo
while the container went on serving an older image. Config read correct,
`verify-live.sh` failed checks that passed locally, and that reads as a
broken checker rather than a stale deploy. Fixed in #57: `go-live-x402.sh`
now hands off to `repair-and-deploy.sh`, which deploys source.

**Green config is not a deploy.** File that next to "green tests do not prove
a deploy" — same failure, one layer up.

Two questions are now closed and should not be reopened:

- **The pay-to address exists and is well-formed.** Two sessions burned days
  on "the owner has no 40-hex address." The live 402 advertises x402, and the
  #46 guard refuses to advertise unless the address is exactly `0x` + 40 hex.
  The address is real; it was minted for the deployment, not held in a wallet
  app. Both halves of the old contradiction were true.
- **Coinbase is out of the path entirely.** It was only ever the facilitator —
  the referee that verifies signatures — never the destination of the money.
  The DBA review blocks CDP and nothing else.

### Bazaar discovery is NOT live, and cannot be with this facilitator

Checked directly on 2026-08-25, after the swap:

    curl -s https://facilitator.xpay.sh/discovery/resources
    {"message":"Not Found"}

xpay.sh settles payments and runs no Bazaar index. The Bazaar is populated by
facilitators reading the discovery extension off 402s; if the facilitator has
no `/discovery/resources`, nothing is cataloged. This node's 402s carry the
extension correctly -- `verify-live.sh` passes that check -- and it goes
nowhere.

**Payable is not discoverable.** Two sessions in a row inferred the second
from the first. Do not repeat it: serving discovery data is our half; a
facilitator indexing it is the other half, and we do not control it.

So capability-based discovery -- an agent that has never heard of HubVibe
finding it by what it does -- is currently **unavailable**. CDP is the
facilitator that would provide it and it is blocked on the DBA review. The
open question worth one search, and nobody has done it: **is there a keyless
x402 facilitator that also serves `/discovery/resources`?** If one exists,
switching to it restores capability discovery for the price of one env var.

Until then the discovery that actually exists is name-based or human-browsed:
the MCP registry, the GitHub Marketplace listing, Glama, and the crawler
surfaces (`llms.txt`, `agent.json`, `sitemap.xml`). Marketplace is the
primary channel now, not the Bazaar.

None of this is demand, and the counter to watch is still charges, not checks
passed.

## Decisions already made — do not reverse these

1. **Never print the per-call cent price on a human-facing page.** Not on the
   landing page, not in meta descriptions, not in JSON-LD `lowPrice`. A
   `$0.03` sitting above a `$79` plan invites one subtraction and makes every
   plan look absurd. This exact mistake has been made and fixed twice. Agents
   read exact rates from `/.well-known/agent.json` and the 402 challenge,
   which are generated from the same catalog the routes charge from. Three
   tests enforce this.
2. **Human plans are priced per site watched, never per scan.** Denominating
   in scans makes them directly comparable to the machine rate and strictly
   worse than paying per call.
3. **No free scan.** An audit costs a real browser page load.
4. **Never advertise a rail or plan that cannot settle.** Everything is
   fail-closed and omits what is not configured, rather than advertising it
   with a null recipient. This is the core discipline of the codebase.
5. **A2A leads.** The machine API section precedes the plans on the landing
   page. The plans are the secondary path for humans who don't want to build
   an integration.

## How to deploy

One command. It reads the live config, repairs only what is wrong, deploys,
and verifies:

```bash
cd ~/HubVibe-deploy4 && git fetch origin main && git reset --hard origin/main
bash scripts/repair-and-deploy.sh
```

It is safe to re-run. It refuses to point at a Stripe secret that does not
exist, and it strips placeholder x402 values before they can reach a live
revision.

## What is left

**THE ONE THING — half done.** The deploy landed and the checker proved it:
**36/36 against the deployed node, 2026-08-27**, including both payability
checks. The half that remains is the first paid call, in Cloud Shell:

```bash
cd ~/HubVibe-deploy4                       # already reset + deployed 2026-08-27
export HUBVIBE_WALLET_KEY=0x...            # funded with USDC on Base
bash scripts/first-paid-call.sh
```

That one call, for $0.03, is the only thing that can prove the two facts still
unproven: the facilitator actually settling a payment, and the Bazaar record
riding a real payment payload. The script preflights the live 402 and refuses
to spend if anything about it would waste the payment.

Two ways to read the output wrong, both already made once:

- **`34 passed` is a stale checkout, not a pass.** The checker has been 36
  checks since #61. A 34 means `HubVibe-deploy4` is behind `origin/main` — the
  two payability checks are not in that copy, so the run cannot tell you
  whether the rail works. Re-run the `git reset --hard` line.
- **`first-paid-call.sh: No such file or directory` is the same problem.** That
  script arrived in #61. It is on `main` now; it is not in an older checkout.

`repair-and-deploy.sh` before `verify-live.sh` is not optional ordering.
`gcloud run services update --update-env-vars` mints a revision carrying the
**same container image**, so the fix can be merged, the config correct, and the
container still serving the old code. That is documented twice above because it
has happened twice.

**0. Turn x402 on — everything needed is now known, and none of it is Coinbase.**

Established 2026-08-18, each piece verified, so do not re-litigate:

- **Facilitator: Coinbase is NOT required.** CDP's verify/settle endpoints
  need a CDP API key AND a verified Coinbase Business Account (their docs
  say so directly) — that is the review sitting in limbo. But the code
  supports any facilitator, and `https://facilitator.xpay.sh` is keyless,
  Base mainnet, zero-fee, x402 v1+v2. **The owner health-checked it live
  from Cloud Shell on 2026-08-18**: `curl https://facilitator.xpay.sh/health`
  → `{"status":"ok","service":"xpay-facilitator"}`. When CDP clears review
  it can replace xpay for the Bazaar listing; it is not the gate.
- **Pay-to address: two working options.** A Stripe-custodied Base deposit
  address (`scripts/x402-setup.py`, preview API — same architecture as
  Stripe's own machine-payments sample), or a self-custody Base wallet
  (e.g. Base App). NOT Cash App — custodial with no Base support, funds
  sent there are unrecoverable. The service now refuses to advertise x402
  for any address that is not 0x + 40 hex, wherever it came from.
- **Settlements are mirrored into Stripe** (added 2026-08-18, from Stripe's
  machine-payments sample): when `STRIPE_SECRET_KEY` is set, every settled
  x402 payment is recorded as a PaymentIntent in `transaction_verification`
  mode, idempotent by transaction hash. This is what makes on-chain revenue
  show in the Stripe balance — "is my Stripe account going up from zero"
  now measures x402 too, but only if the pay-to address is the
  Stripe-custodied one, since Stripe verifies the transaction against its
  own deposit address.

So the recipe: mint the deposit address with `x402-setup.py`, then set

```
X402_FACILITATOR_URL = https://facilitator.xpay.sh
X402_PAY_TO_ADDRESS  = <the deposit address>
```

and deploy. Preflight validates the address shape; `verify-live.sh` proves
the 402 advertises a settleable rail.

**1. Marketplace listing — one form, and it has never been submitted.**
The standalone repo exists and is current: `action.yml` and the renderer are
byte-identical to this repo (`diff -r` against a fresh
`scripts/publish-action-repo.sh` output is clean, zero drift), the listing
README is live on its `main`, and tags `v1`/`v1.0.0` are pushed.
`uses: Its-fortunatefolly/hubvibe-audit-action@v1` works today.

**Tags were retagged onto the listing rewrite on 2026-08-16** and verified
against the remote: `v1` and `v1.0.0` both → `ab666f3`, which is that repo's
`main`. Before this they sat on `a900531`, one commit behind — `action.yml`
and the renderer were identical across the two, so `@v1` always *behaved*
correctly, but README is the Marketplace listing body, so a Release cut from
the old `v1.0.0` would have published the pre-rewrite copy and the work in
&#35;41 would never have reached the listing. **A Release can now be cut from
`v1.0.0` as it stands.**

If they ever need moving again, note these run in the ACTION repo, not this
one — that distinction already cost one wrong push (see the lessons below):

```bash
cd ~ && git clone https://github.com/Its-fortunatefolly/hubvibe-audit-action
```
```bash
cd ~/hubvibe-audit-action
```
```bash
git tag -f -a v1.0.0 -m v1.0.0 origin/main
```
```bash
git tag -f -a v1 -m v1 origin/main
```
```bash
git push -f origin v1.0.0 v1
```

Confirm it landed on the right remote — the push output must say
`To https://github.com/Its-fortunatefolly/hubvibe-audit-action`. If it says
`.../HubVibe.git`, it went to the monorepo.

What does NOT exist is a **Release** — the repo has zero. Tags do not publish
to Marketplace; a Release with the "Publish this Action to the GitHub
Marketplace" box ticked does. That box is UI-only (there is no API for
Marketplace publication, and it is gated on accepting GitHub's Developer
Agreement), so it cannot be automated. Verified independently: the releases
page says "There aren't any releases here", and
`github.com/marketplace/actions/hubvibe-site-compliance-audit` 404s.

**2. Republish `server.json` to the MCP registry** (`./mcp-publisher publish`).
`server.json` is at 1.1.1 (#45) precisely so the publish can land — the
registry rejects re-publishing a version it already serves, and it has
served 1.1.0 with pre-correction text since 2026-08-11. Login opens a
browser, so it must be run by a human.

**3. Cosmetic:** the card statement descriptor reads `HUBEVIBE` (extra E).
Dashboard → Settings → Payments. (The Stripe MCP connector is read-only for
account settings — verified 2026-08-18 — so this cannot be fixed from a
session.)

~~Register with Glama~~ — done; the owner confirmed 2026-08-18 the service
is on the Glama connections registry.

## 2026-08-16: x402 + Bazaar discovery — discovery shipped; the MONEY PATH IS DEAD

This section spent two days marked "not confirmed" because of a real
contradiction: it claimed a valid pay-to address was set, and the owner said
they do not have a 40-hex address. The resolution, established on
2026-08-18: **both were true**. The address belongs to the deployment —
minted custodially, never something the owner holds in a wallet app. Keep
that distinction; it is what made the contradiction look unresolvable.

### The "by construction" argument was WRONG. Read this before trusting a shape check.

An earlier version of this section argued: revision `hubvibe-00071-97g`
carries the #46 fail-closed guard, which refuses to advertise x402 unless
the pay-to address is `0x` + 40 hex; the live node advertises x402;
therefore the address is well-formed **by construction**.

The logic held. The conclusion was worthless. Later on 2026-08-18 the owner
printed the deployed value:

```
X402_PAY_TO_ADDRESS = 0x0000000000000000000000000000000000000000
```

The zero address is `0x` + 40 hex. It passes the #46 guard, it passes the
preflight, and the verify-live "unpayable rail" check only ever looked for
`payTo:null`. Every gate said yes. And `address(0)` is unownable — USDC's
contract reverts transfers to it — so **not one x402 payment could ever have
arrived**, on any revision, since the day it was set.

This is the single most expensive lesson in this file: **a shape check
proves shape, and shape is not payability.** "Well-formed" answered a
question nobody was asking. The question was "can money land here", and the
zero address is precisely the value that satisfies a format gate while
answering no. Whoever set it on 2026-08-16 almost certainly did so to get
past the shape gate.

Three layers now reject it specifically (see the 2026-08-18 zero-address
commit): the app guard logs at ERROR and turns the rail off, the preflight
blocks the deploy, and both are mutation-tested. But those protect the
NEXT deploy — they do not retroactively fix a running revision.

**Current state of x402: advertised, and unpayable, until a real recipient
address is deployed.** Do not read "x402 in the advertised methods" as
working. Read the address itself:

```bash
gcloud run services describe hubvibe --project=resolver-time --region=us-south1 --format=json \
  | python3 -c "import json,sys;d=json.load(sys.stdin);env=[e for c in d['spec']['template']['spec']['containers'] for e in c.get('env',[])];print(next((e.get('value') or 'FROM SECRET: '+e['valueFrom']['secretKeyRef']['name'] for e in env if e['name']=='X402_PAY_TO_ADDRESS'),'NOT SET'))"
```

Then set a real one — a Stripe-custodied Base deposit address from
`scripts/x402-setup.py --network base` (revenue lands in the Stripe balance,
and the #47 PaymentIntent mirroring only fires for a Stripe-custodied
address), or a self-custody Base address — and redeploy.

Also still unknown, and separate: whether the CDP facilitator will SETTLE —
its verify/settle endpoints are gated on a Coinbase Business Account review
that is still pending. The first real agent payment answers it. If payments
bounce, switch `X402_FACILITATOR_URL` to `https://facilitator.xpay.sh`
(keyless, Base mainnet, zero-fee, owner-health-checked) and redeploy.

The original 2026-08-16 report follows, unedited:

A valid `0x` + 40-hex pay-to
address was set alongside the CDP facilitator, and revision
`hubvibe-00069-8kp` went out. `verify-live.sh` then reported 29/29 including:

```
PASS  402 does not advertise an unpayable x402 rail
PASS  402 carries x402 Bazaar discovery data (indexable by facilitators)
      payment:{methods:[x402 mpp-stripe mpp-tempo stripe_api_key]}
PASS  authenticated /audit/wcag -> 200 with a real audit result
```

Why it mattered: the MCP registry is name-based — an agent finds this node
only if it already knows the name. Bazaar is capability-based; facilitators
catalog x402 resources by reading the discovery extension off their 402s, and
agents shop that index by what a service does and what it costs. That is how
an agent that has never heard of HubVibe finds it. Before this, the deployed
`X402_PAY_TO_ADDRESS` held **16 hex characters** where 40 are required, so
x402 was advertised as live while being unsettleable — from this side
indistinguishable from nobody buying.

`repair-and-deploy.sh` preflights the address and refuses to deploy a
malformed one, so this cannot silently regress. Note the preflight only
checks a *plain env var*: a pay-to address supplied via Secret Manager is
explicitly not shape-checked. An EVM address is public — keep it a plain env
var so the guard applies.

**Being listed is not the same as being bought.** Bazaar makes the node
findable by capability; it does not create demand. The counter to watch is
charges, not checks passed — see below.

## The number that reframes everything: ZERO

Checked directly against the live Stripe account on 2026-08-15:

**Zero checkout sessions, ever. Zero charges, ever.**

Not one failed payment. Not one abandoned cart. Nothing has ever been
rejected, because nothing has ever been attempted. Whatever else is true, the
problem has never been that buyers were blocked — no buyer has arrived.

That matters for how the outages of 2026-08-15 are read. The service returned
HTTP 500 to every authenticated caller for an unknown number of days, x402
advertised a rail that could never settle, and checkout had never been walked
end to end. All three were real and all three are fixed — and all three cost
exactly zero revenue, because there was no one on the other side.

So: fixing infrastructure was necessary (a 500 service cannot take money) and
is now done. It was never sufficient. Do not read a future outage fix as
progress toward revenue.

Three live payment links were created on 2026-08-15 so buying needs no
integration at all — anyone with the URL can pay:

| | |
|---|---|
| Single report $29.99 | https://buy.stripe.com/aFa3cvf0q6x6dg2apMgQE00 |
| Pro $79/mo | https://buy.stripe.com/7sYdR93hIbRq2BoeG2gQE01 |
| Agency $249/mo | https://buy.stripe.com/4gM4gz6tU3kUek61TggQE02 |

## The growth math, stated plainly

The build side of "more machine traffic" is now essentially done. What is not
done, and what no amount of code closes, is demand.

At $0.03–$0.10 a call, **$1M of revenue is 10–33M paid calls; a multi-million
run rate is 100M+.** Spread over a year, 100M calls is ~3 paid calls every
second, continuously, from a base that is currently approximately zero paying
machine callers.

**Per-call compute is not the risk.** An earlier session flagged it as the
biggest unknown; that was wrong, and worth correcting so nobody re-opens it.
Run `bash scripts/measure-call-cost.sh` for the real figure, but the ceiling
is easy to bound: even at a pessimistic 4 vCPU / 4 GiB and 12 seconds per
audit, one call costs roughly **$0.0016** — about 1.6% of the $0.10 bundle.
Compute would have to be ~60x worse than that before the margin is in danger.
Gross margin per call is ~98%.

Two things actually threaten the economics, and neither is CPU time:

1. **Idle billing at low volume.** `min-instances=1` on a browser-sized
   container is on the order of **$275/month before a single call arrives**.
   Against near-zero paid traffic that is the entire cost structure, and it is
   pure loss. Check it first; the measure script warns when min-instances > 0.
2. **Demand.** ~98% margin on zero calls is zero. The constraint was never the
   cost side.

The honest constraint: adoption is not something the code can force. A CI
gate that costs money on every push is a line item someone has to approve,
and the ones that get adopted are the ones that are trivially removable and
never block a deploy on a third-party outage. That is why the action defaults
to retrying transient failures and supports `fail-on-error: false`. Making it
harder to remove would lower adoption, not raise it.

## Sandbox limits — know these before promising to check something

The build sandbox **cannot reach**: `*.run.app`, `*.github.io`,
`api.stripe.com`, `api.cdp.coinbase.com`, `docs.cdp.coinbase.com`,
`x402.org`. It **can** reach PyPI, the GitHub API, and the MCP registry API,
and it has a Stripe MCP connector with live read + write access.

So: live-service verification must be run by the user with
`verify-live.sh`. Do not claim a live URL is fine without evidence.

There is also **no `gcloud` in the sandbox**, so nothing here can write to
Secret Manager or create a Cloud Run revision. `repair-and-deploy.sh` says as
much and stops (`gcloud is not on PATH. Run this in Cloud Shell.`) rather than
half-applying. Setting `X402_PAY_TO_ADDRESS` is therefore always an
owner-side action, from Cloud Shell.

**What the sandbox CAN do instead, and should — boot the service locally.**
This is how the Bazaar bug above was found, after months of it being invisible:

```bash
python3 -m venv .venv && .venv/bin/pip install -r wcag-audit-engine/requirements.txt
cd wcag-audit-engine
X402_FACILITATOR_URL=https://x402.org/facilitator \
X402_PAY_TO_ADDRESS=0x32b08c5e927c69877d0fcab35618c265674922bc \
PUBLIC_BASE_URL=http://127.0.0.1:8080 \
../.venv/bin/python -m uvicorn app.main:app --port 8080
```

The address above is the test-suite constant — shape-valid, so the fail-closed
guard advertises x402 and the whole 402 path becomes inspectable without
network, money, or a facilitator that answers. Every discovery surface
(`/mcp.json`, `/.well-known/agent.json`, `/openapi.json`, the 402 bodies, the
`/mcp` JSON-RPC endpoint) can be driven and asserted against this. Use it
before claiming a discovery surface is right; do NOT use it to claim anything
about the deployed node, which may be running an older revision.

## Tooling added 2026-08-15 — use it, do not rebuild it

| | |
|---|---|
| `scripts/repair-and-deploy.sh` | the one deploy command. **Preflights the live environment and refuses to deploy into a broken one** (Firestore exists, every secret readable AND correctly shaped, pay-to address well-formed, min-instances reported). Idempotent — a healthy service now mints zero revisions. |
| `scripts/repair-secrets.sh` | repairs Secret Manager. Additive only: never disables, never destroys, refuses to invent a value it cannot find, never prints one. |
| `scripts/verify-live.sh` | 29 live checks **including the authenticated paid path** — the check that answers "can this take money". It resolves the API key itself. |
| `scripts/lib-api-key.sh` | resolves an API key with no human: reads which secret backs `AUDIT_API_KEY` off the service and fetches it. An explicit `HUBVIBE_API_KEY` still wins. |
| `scripts/measure-call-cost.sh` | measures real cost per audit against real Cloud Run rates. |
| `scripts/publish-action-repo.sh` | generates the standalone Marketplace repo, verbatim, so it cannot drift. |

## Hard-won lessons — these cost real time

- **A filtered view is not a record.** Reading `gcloud ... | grep price`
  output and reconstructing name/value pairs produced a confident, wrong
  conclusion that a Stripe key was broken, and a "fix" that overwrote a
  working secret reference and blocked every deploy. Dump the whole record.
- **Green tests do not prove a deploy.** A route that passed every local test
  still 500'd in the container because its data file was never COPYed into
  the image. `verify-live.sh` exists for exactly this.
- **A dev machine's transitive dependencies mask missing pins.** Bazaar
  discovery silently returned `{}` because `jsonschema` was installed locally
  but not pinned. Only clean CI caught it. There is now a static test on the
  requirements text. **It then happened again with PyYAML**, and the second
  time it hid better: `tests/test_marketplace_action.py` opened with
  `pytest.importorskip("yaml")`, so in CI the module did not fail — it
  vanished, 29 tests reported as one tidy skip. Two sessions read 291 and 262
  off the same commit and both were honest. The lesson generalises past
  "pin your deps": **`importorskip` on a module that guards a shipped
  artifact converts a missing pin into a green run.** Those 29 tests guard
  `action.yml`, which had already shipped broken once, and they had never
  executed in CI. A count that moves with the machine is itself the signal —
  chase it, do not reconcile it. Fixed in #44: the pin, a hard `import yaml`,
  and a test asserting the pin is still declared (deleting it breaks nothing
  locally, which is exactly how it hid).
- **Prove a test fails.** Every guard in this repo was verified by
  reintroducing the bug and watching the test go red, then restoring.
- **`gcloud --format=flattened` pads names with alignment spaces.** Any
  `grep 'name: X'` against it silently matches nothing. This shipped THREE
  times: the pay-to check (reported nothing at all, which read as a pass),
  the Stripe secret lookup (minted a pointless Cloud Run revision on every
  run, reaching revision 62), and once in a fix for the first two. Always
  `--format=json` and parse it. A test now asserts `flattened(` appears
  nowhere in the deploy script.
- **`latest` on a Secret Manager secret is the highest version NUMBER,
  regardless of state.** Disabling a bad newest version does not fall back —
  it makes `latest` unreadable and any container mounting it fails to start.
  Disabling looks like the careful move and is strictly worse than nothing.
  The safe repair is always additive: copy a good version forward.
- **Readable is not correct.** A hand-written test string in the Stripe key
  secret read back fine and would have failed at the Stripe API rather than
  at startup — far harder to notice. Check shape, not just readability.
- **A check that silently skips is worse than no check.** It converts
  "unverified" into "verified" in the reader's head. `verify-live.sh` printed
  28/28 while the paid path was dead, for exactly this reason. Every branch
  must say something, including "there was nothing to check".
- **Command substitution strips trailing newlines.** `case $x in *"$(printf
  '\n')")` evaluates to `*""` and matches everything; and `$(f)` runs `f` in a
  subshell, so variables it sets are lost. Both bit the API-key resolver.
- **A secret with a trailing newline can never authenticate.** The container
  compares exactly and an HTTP header cannot carry a newline. Write secrets
  with `printf '%s'`, never `echo`.
- **A Marketplace action repo must contain NO workflow files.** Not "one
  action at the root" — no workflows, at all. HubVibe has CI, so it can never
  be listed itself, and moving `action.yml` around does not fix it. This was
  correctly worked out in an earlier session, written down in a README, and
  then that README was deleted along with the directory it lived in. It cost
  a re-derivation. `scripts/publish-action-repo.sh` now encodes it, and
  `tests/test_marketplace_action.py` fails if the assumption changes.
- **Direct `uses:` and a Marketplace listing are different things.**
  `uses: owner/repo@ref` resolves a root `action.yml` with no listing
  involved. Marketplace is discoverability only. Conflating them makes the
  listing look like a blocker for adoption when it is not.
- **The user is often on mobile.** Long multi-line pasted commands land on a
  non-empty input line and run together into garbage (`1gcloud`, `%bash`).
  Keep commands to one short line; that is why `repair-and-deploy.sh` exists.
- **`git` commands are silent about which repo they ran in, so say it.** Two
  repos are in play here and only one shell is ever open, almost always
  `~/HubVibe-deploy4`. A retag block written without a `cd` was pasted there
  and force-moved the MONOREPO's `v1` and created a monorepo `v1.0.0`, while
  the action repo it was meant for stayed untouched — and the push output
  said so plainly (`To .../HubVibe.git`) with nobody reading it. Any command
  block for the action repo starts with the clone or the `cd`, and ends with
  what the push output must say. Same class of failure as reading a filtered
  view instead of the record: the evidence was right there and unexamined.
  Both were repaired the same day; the monorepo's stray `v1.0.0` was deleted
  and its `v1` left at `30fce35`, which resolves a current `action.yml`, so
  `uses: Its-fortunatefolly/HubVibe@v1` still works. The monorepo's previous
  `v1` target is not recoverable — a force-moved tag takes its old value with
  it, which is the argument for reading the push output *before* the next
  command, not after someone asks.
