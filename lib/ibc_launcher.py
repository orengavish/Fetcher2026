"""
lib/ibc_launcher.py
IBC (Interactive Brokers Controller) gateway launcher.

Calls IBC's StartGateway.bat to start IB Gateway automatically.
Does NOT wait for the gateway to be ready — callers handle polling.

Config keys (under ib:):
  ibc_startgateway_bat: ""     # full path to IBC's StartGateway.bat
  ibc_mode: paper              # paper | live  (passed as first arg to StartGateway.bat)

If ibc_startgateway_bat is empty or missing, try_start_gateway() returns False
(silently — no crash). Gateway startup degrades to manual.

Usage:
  from lib.ibc_launcher import try_start_gateway
  launched = try_start_gateway(cfg, label="backfill")

Self-test:
  python -m lib.ibc_launcher --self-test
"""

import sys
import subprocess
import argparse
from pathlib import Path

from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("ibc_launcher")


def try_start_gateway(cfg, label: str = "") -> bool:
    """
    Launch IB Gateway via IBC's StartGateway.bat.

    Returns True if the launch command was issued, False if IBC is not
    configured or the bat file is not found. Errors are logged but never
    raised — callers must poll separately to know when the gateway is ready.
    """
    ib_cfg = getattr(cfg, "ib", None)
    bat    = getattr(ib_cfg, "ibc_startgateway_bat", None)

    if not bat:
        log.debug("ibc_startgateway_bat not set — skipping auto-start")
        return False

    bat_path = Path(bat)
    if not bat_path.exists():
        log.warning(f"IBC StartGateway.bat not found: {bat_path} — skipping auto-start")
        return False

    mode = getattr(ib_cfg, "ibc_mode", "paper")
    tag  = f" ({label})" if label else ""

    log.info(f"Launching IB Gateway via IBC — mode={mode}{tag}: {bat_path}")
    print(f"  Starting IB Gateway via IBC ({mode})...", flush=True)

    try:
        kwargs = {}
        if sys.platform == "win32":
            # Open a new console window so the IB Gateway UI has somewhere to render.
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE

        subprocess.Popen(
            ["cmd.exe", "/c", str(bat_path), mode],
            **kwargs,
        )
        log.info("IBC StartGateway.bat launched")
        return True

    except Exception as e:
        log.error(f"IBC StartGateway launch failed{tag}: {e}")
        return False


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    try:
        cfg = get_config()

        # 1. Missing bat → returns False, does not crash
        class _NoBat:
            ibc_startgateway_bat = None
            ibc_mode = "paper"
        class _CfgNoBat:
            ib = _NoBat()

        result = try_start_gateway(_CfgNoBat())
        assert result is False, "Expected False when bat not configured"

        # 2. Bat path does not exist → returns False
        class _BadBat:
            ibc_startgateway_bat = "C:\\nonexistent\\StartGateway.bat"
            ibc_mode = "paper"
        class _CfgBadBat:
            ib = _BadBat()

        result = try_start_gateway(_CfgBadBat(), label="test")
        assert result is False, "Expected False for missing bat file"

        # 3. Real config loads without error
        _ = getattr(getattr(cfg, "ib", None), "ibc_startgateway_bat", None)

        print("[self-test] ibc_launcher: PASS")
        return True

    except Exception as e:
        print(f"[self-test] ibc_launcher: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("ibc_launcher — use try_start_gateway(cfg) in your component")
