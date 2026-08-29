"""
scripts/test_mkt_depth.py
One-off diagnostic: request Level 2 (market depth) on IB LIVE (port 4001, hardcoded --
ignores trader/config.yaml's live_port, which currently points at the paper gateway) for
our 4 index futures. Used to see the exact IB error before subscribing to depth data, and
to confirm it clears after subscribing.

Usage:
    python scripts/test_mkt_depth.py

ponytail: throwaway diagnostic, not part of the running pipeline -- no CLI args, no config
of its own, just enough to answer "does depth work yet."
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.ib_client import IBClient

SYMBOLS = ["MES", "MNQ", "MYM", "M2K"]
LIVE_PORT = 4001          # hardcoded -- config.yaml's live_port currently == paper (4002)
TEST_CLIENT_ID = 999      # outside every existing pool (101-120, 201-210, 801-804)
WAIT_SECONDS = 8


def main():
    ibc = IBClient()
    ibc._live_port = LIVE_PORT
    ibc._live_ids = [TEST_CLIENT_ID]

    print(f"Connecting to LIVE 127.0.0.1:{LIVE_PORT} clientId={TEST_CLIENT_ID} ...")
    ibc.connect(live=True, paper=False)
    ib = ibc.live
    print("Connected.\n")

    # Baseline: proves the connection itself is healthy, independent of depth entitlement
    # or market hours (this already runs on delayed data, type 3 -- see ib_client.py:104).
    print("--- Baseline: get_price('MES') ---")
    try:
        price = ibc.get_price("MES")
        print(f"OK — MES price (delayed): {price}\n")
    except Exception as e:
        print(f"FAILED — baseline price fetch errored: {e}\n")

    # Collect every error IB sends us, keyed by the contract's symbol (ib_insync's errorEvent
    # includes the contract, which is a more reliable key here than digging for the reqId).
    errors_by_symbol = {}

    def on_error(reqId, errorCode, errorString, contract=None, advancedOrderRejectJson=""):
        sym = getattr(contract, "symbol", None)
        if sym:
            errors_by_symbol.setdefault(sym, []).append((errorCode, errorString))
        print(f"[ERROR] reqId={reqId} code={errorCode} msg={errorString}")

    ib.errorEvent += on_error

    print("--- Requesting market depth for", ", ".join(SYMBOLS), "---")
    tickers = {}
    for sym in SYMBOLS:
        try:
            con = ibc.get_contract(sym)
        except Exception as e:
            print(f"{sym}: could not resolve contract — {e}")
            continue
        ticker = ib.reqMktDepth(con, numRows=5, isSmartDepth=False)
        tickers[sym] = (con, ticker)
        print(f"{sym}: requested depth")

    print(f"\nWaiting {WAIT_SECONDS}s for depth rows / errors ...")
    ib.sleep(WAIT_SECONDS)

    print("\n=== RESULTS ===")
    for sym, (con, ticker) in tickers.items():
        bids = ticker.domBids
        asks = ticker.domAsks
        req_errors = errors_by_symbol.get(sym, [])

        if req_errors:
            codes = ", ".join(f"{c} ({m})" for c, m in req_errors)
            print(f"{sym}: SUBSCRIPTION-DENIED — {codes}")
        elif bids or asks:
            print(f"{sym}: OK — {len(bids)} bid rows, {len(asks)} ask rows")
            for row in bids[:3]:
                print(f"    BID {row.price} x {row.size}")
            for row in asks[:3]:
                print(f"    ASK {row.price} x {row.size}")
        else:
            print(f"{sym}: NO DATA, NO ERROR — inconclusive (market closed and no permission "
                  f"error yet? re-check, don't treat as pass or fail)")

    print("\nCleaning up ...")
    for sym, (con, _ticker) in tickers.items():
        try:
            ib.cancelMktDepth(con)
        except Exception:
            pass
    ibc.disconnect()
    print("Disconnected.")


if __name__ == "__main__":
    main()
