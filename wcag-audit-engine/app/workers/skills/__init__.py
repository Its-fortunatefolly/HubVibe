"""The bees.

A provider adapter is a tool. A skill in here is a WORKER: it takes a defined
task, picks and invokes the right capability, processes what comes back, and
returns a completed result a buying agent can use without further work.

Each skill is `async def run(ctx, payload) -> dict`. It validates its own
input (raising runtime.InvalidRequest, which the router answers before any
payment is read), runs its steps through `ctx.run` so every provider attempt
is accounted for, and returns the finished product.

Composites live in `composites.py` and are built by CALLING these same
skills -- not by reimplementing them. That is what makes the network compose
rather than merely coexist.
"""

from . import chain, composites, data, extract, llm, market  # noqa: F401

REGISTRY = {}
for _module in (extract, llm, chain, market, data, composites):
    REGISTRY.update(_module.SKILLS)
