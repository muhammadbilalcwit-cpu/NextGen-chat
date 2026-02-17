"""
Terminal colors and logging utilities for structured startup/shutdown output.

Mirrors the production NextGen FastAPI log_config pattern with colored formatter:
    2026-02-13 14:41:49,095 [INFO] __main__:  ✓ PostgreSQL (read-only, 45ms)

Colors in log prefix:
    - Timestamp  → dim gray
    - [INFO]     → green    [WARNING] → yellow    [ERROR] → red
    - __main__   → magenta
    - Message    → uses embedded Colors from caller
"""
import logging
import os
import sys
import time


class Colors:
    """ANSI color codes for terminal output."""
    GREEN = "\033[92m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    YELLOW = "\033[93m"
    WHITE = "\033[97m"
    DIM = "\033[90m"
    MAGENTA = "\033[95m"
    RESET = "\033[0m"

    @classmethod
    def disable(cls):
        """Disable colors (e.g. for non-TTY output)."""
        for attr in ("GREEN", "RED", "CYAN", "YELLOW", "WHITE", "DIM", "MAGENTA", "RESET"):
            setattr(cls, attr, "")


def _colors_supported() -> bool:
    """Check if the terminal supports ANSI colors."""
    if os.environ.get("FORCE_COLOR", "").strip() in ("1", "true"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass
        return True
    return sys.stdout.isatty()


_use_colors = _colors_supported()
if not _use_colors:
    Colors.disable()


# ── Colored Formatter ──

# Map log levels to colors (matches production NextGen style)
_LEVEL_COLORS = {
    "DEBUG": "\033[90m",     # dim
    "INFO": "\033[92m",      # green
    "WARNING": "\033[93m",   # yellow
    "ERROR": "\033[91m",     # red
    "CRITICAL": "\033[91m",  # red
}
_MAGENTA = "\033[95m"
_DIM = "\033[90m"
_RESET = "\033[0m"


class ColoredFormatter(logging.Formatter):
    """
    Log formatter that colorizes the prefix to match the production server:
        {dim}2026-02-13 14:41:49,095{reset} {green}[INFO]{reset} {magenta}__main__{reset}: message
    """

    def format(self, record: logging.LogRecord) -> str:
        # Let the base formatter build the timestamp
        record.asctime = self.formatTime(record, self.datefmt)

        if _use_colors:
            level_color = _LEVEL_COLORS.get(record.levelname, "")
            return (
                f"{_DIM}{record.asctime}{_RESET} "
                f"{level_color}[{record.levelname}]{_RESET} "
                f"{_MAGENTA}{record.name}{_RESET}: "
                f"{record.getMessage()}"
            )
        else:
            return (
                f"{record.asctime} "
                f"[{record.levelname}] "
                f"{record.name}: "
                f"{record.getMessage()}"
            )


def setup_logging(level: int = logging.INFO):
    """
    Configure root logger with colored formatter matching the production server.
    Also quiets noisy third-party loggers.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColoredFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    # Remove any existing handlers (avoid duplicates on reload)
    root.handlers.clear()
    root.addHandler(handler)

    # Quiet noisy libraries
    for name in ("aio_pika", "aiormq", "motor", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Keep uvicorn access logs but reduce engine noise
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)


def format_duration(ms: float) -> str:
    """Format duration in ms to a readable string."""
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms:.0f}ms"


async def time_async(coro):
    """Time an async coroutine. Returns (result, duration_ms, success)."""
    start = time.perf_counter()
    try:
        result = await coro
        success = True
    except Exception as e:
        result = e
        success = False
    duration_ms = (time.perf_counter() - start) * 1000
    return result, duration_ms, success
