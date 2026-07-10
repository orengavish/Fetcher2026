"""
lib/logger.py
Shared logging setup for all Galao components.
Each component gets its own log file in logs/.
Format: YYYY-MM-DD HH:MM:SS UTC | LEVEL | component | message

Usage:
    from lib.logger import get_logger
    log = get_logger("broker")
    log.info("Broker started")
    log.debug("Polling DB for PENDING commands")
    log.error("IB connection failed")

Self-test:
    python lib/logger.py --self-test
"""

import sys
import logging
import argparse
from pathlib import Path
from datetime import timezone

_loggers: dict = {}


class _UTCFormatter(logging.Formatter):
    converter = lambda *args: __import__('datetime').datetime.now(
        __import__('datetime').timezone.utc).timetuple()

    def formatTime(self, record, datefmt=None):
        import datetime
        dt = datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    def format(self, record):
        record.utctime = self.formatTime(record)
        return f"{record.utctime} | {record.levelname:<8} | {record.name:<20} | {record.getMessage()}"


def get_logger(component: str, log_dir: str = None) -> logging.Logger:
    """
    Get or create a logger for a component.
    Writes to logs/{component}.log and stdout (INFO+ only on stdout).
    """
    if component in _loggers:
        return _loggers[component]

    if log_dir:
        log_path = Path(log_dir)
    else:
        from lib.config_loader import get_config
        try:
            cfg = get_config()
            log_path = Path(cfg.paths.logs)
        except Exception:
            log_path = Path("logs")

    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(component)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fmt = _UTCFormatter()

    # File handler — DEBUG and above
    fh = logging.FileHandler(log_path / f"{component}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler — INFO and above
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    _loggers[component] = logger
    return logger


def reset_loggers():
    """Clear cached loggers — used in tests."""
    for name, lgr in _loggers.items():
        lgr.handlers.clear()
    _loggers.clear()
    logging.getLogger().handlers.clear()


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile, os
    try:
        reset_loggers()

        with tempfile.TemporaryDirectory() as tmp:
            from lib.config_loader import reset_cache
            reset_cache()

            log = get_logger("test_component", log_dir=tmp)

            log.debug("debug message")
            log.info("info message")
            log.warning("warning message")
            log.error("error message")

            log_file = Path(tmp) / "test_component.log"
            assert log_file.exists(), "Log file not created"

            content = log_file.read_text()
            assert "debug message"   in content, "DEBUG missing from log file"
            assert "info message"    in content, "INFO missing from log file"
            assert "warning message" in content, "WARNING missing from log file"
            assert "error message"   in content, "ERROR missing from log file"
            assert "UTC"             in content, "UTC timestamp missing"
            assert "test_component"  in content, "Component name missing"

            reset_loggers()  # close file handles before temp dir cleanup (Windows)
        print("[self-test] logger: PASS")
        return True

    except Exception as e:
        print(f"[self-test] logger: FAIL — {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    log = get_logger("logger_demo")
    log.info("Logger demo — check logs/logger_demo.log")
