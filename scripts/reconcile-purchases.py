#!/usr/bin/env python3
"""Check HubVibe's purchase book against the chains, from a checkout.

On the box the same logic runs inside the container, next to the book:

    cd /root/HubVibe/deploy/vps
    docker compose exec hubvibe python -m app.purchase_reconcile            # report
    docker compose exec hubvibe python -m app.purchase_reconcile --backfill # fill gaps
    docker compose exec hubvibe python -m app.purchase_reconcile --backfill --scan  # also read every
                                                                             # Base USDC log (slow)

From a checkout, point PURCHASE_BOOK_PATH / WORKER_LEDGER_PATH at copies of
the databases and run this file with the same arguments.
"""

import importlib.util
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "wcag-audit-engine" / "app"
spec = importlib.util.spec_from_file_location("wcag_audit_engine_purchase_reconcile", APP / "purchase_reconcile.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.exit(module.main(sys.argv[1:]))
