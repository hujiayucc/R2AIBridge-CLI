import shutil
import sys
import time
from typing import Any, Optional, TYPE_CHECKING

import unicodedata

if TYPE_CHECKING:
    from rich.live import Live as RichLiveType
else:
    RichLiveType = Any

try:
    from rich.console import Console
    from rich.live import Live as RichLive
    from rich.markdown import Markdown
    from rich.text import Text

    RICH_AVAILABLE = True
    CONSOLE = Console()
except ImportError:
    Console = None
    RichLive = None
    Markdown = None
    Text = None
    RICH_AVAILABLE = False
    CONSOLE = None

_RICH_BROKEN = False


def _disable_rich_runtime() -> None:
    global _RICH_BROKEN
    _RICH_BROKEN = True


def print_info(text: str) -> None:
    if RICH_AVAILABLE and (not _RICH_BROKEN) and CONSOLE is not None:
        try:
            CONSOLE.print(text, markup=False, highlight=False, soft_wrap=True)
            return
        except (ModuleNotFoundError, ImportError, AttributeError, TypeError, ValueError, OSError):
            _disable_rich_runtime()
    print(text)


def print_markdown(text: str) -> None:
    if RICH_AVAILABLE and (not _RICH_BROKEN) and CONSOLE is not None and Markdown is not None:
        try:
            CONSOLE.print(Markdown(text))
            return
        except (ModuleNotFoundError, ImportError, AttributeError, TypeError, ValueError, OSError):
            _disable_rich_runtime()
    print(text)


class AdaptiveStreamWriter:
    def __init__(self, min_width: int = 24):
        self.current_col = 0
        self.min_width = min_width
        self._use_rich = bool(RICH_AVAILABLE and CONSOLE is not None and Text is not None)
        self._md_enabled = False
        self._md_buffer = ""
        self._md_last_ts = 0.0
        self._md_last_len = 0
        self._md_live: Optional[RichLiveType] = None

    def _terminal_width(self) -> int:
        try:
            cols = shutil.get_terminal_size(fallback=(100, 24)).columns
        except OSError:
            cols = 100
        return max(cols, self.min_width)

    @staticmethod
    def _char_width(ch: str) -> int:
        return 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1

    def enable_markdown_stream(self) -> bool:
        if _RICH_BROKEN:
            return False
        if (not self._use_rich) or (CONSOLE is None) or (Markdown is None) or (RichLive is None):
            return False
        try:
            if not sys.stdout.isatty():
                return False
        except (AttributeError, OSError):
            return False
        if self._md_enabled:
            return True
        self._md_enabled = True
        self._md_buffer = ""
        self._md_last_ts = 0.0
        self._md_last_len = 0
        try:
            self._md_live = RichLive(
                Markdown(""),
                console=CONSOLE,
                refresh_per_second=6,
                transient=True,
            )
            self._md_live.start()
        except (ModuleNotFoundError, ImportError, TypeError, ValueError, OSError, AttributeError):
            _disable_rich_runtime()
            self._md_live = None
            self._md_enabled = False
            return False
        return True

    def stop_markdown_stream(self) -> None:
        if not self._md_enabled:
            return
        if self._md_live is not None and Markdown is not None:
            try:
                self._md_live.update(Markdown(self._md_buffer or ""))
            except (ModuleNotFoundError, ImportError, TypeError, ValueError, OSError, AttributeError):
                _disable_rich_runtime()
                pass
            try:
                self._md_live.stop()
            except (ModuleNotFoundError, ImportError, TypeError, ValueError, OSError, AttributeError):
                _disable_rich_runtime()
                pass
        self._md_live = None
        self._md_enabled = False
        self._md_buffer = ""
        self._md_last_ts = 0.0
        self._md_last_len = 0

    def is_markdown_streaming(self) -> bool:
        return bool(self._md_enabled)

    def _maybe_render_markdown(self) -> None:
        if (not self._md_enabled) or (self._md_live is None) or (Markdown is None):
            return
        now = time.time()
        if (len(self._md_buffer) - self._md_last_len) < 80 and (now - self._md_last_ts) < 0.15:
            return
        try:
            self._md_live.update(Markdown(self._md_buffer))
        except (ModuleNotFoundError, ImportError, TypeError, ValueError, OSError, AttributeError):
            _disable_rich_runtime()
            return
        self._md_last_ts = now
        self._md_last_len = len(self._md_buffer)

    def write(self, text: str) -> None:
        if self._md_enabled:
            out = (text or "").replace("\r", "")
            if out:
                self._md_buffer += out
                self._maybe_render_markdown()
            return
        if self._use_rich and CONSOLE is not None:
            out = (text or "").replace("\r", "")
            if out:
                CONSOLE.print(out, end="", markup=False, highlight=False, soft_wrap=True)
            return
        for ch in text:
            if ch == "\r":
                continue
            if ch == "\n":
                sys.stdout.write("\n")
                self.current_col = 0
                continue
            width = self._char_width(ch)
            if self.current_col + width > self._terminal_width():
                sys.stdout.write("\n")
                self.current_col = 0
            sys.stdout.write(ch)
            self.current_col += width
        sys.stdout.flush()

    def write_prefix(self, prefix: str) -> None:
        if self.current_col != 0:
            self.write("\n")
        if self._use_rich and CONSOLE is not None and Text is not None:
            style = None
            if prefix.strip() == "[思考]":
                style = "yellow"
            elif prefix.strip() == "[回答]":
                style = "bold green"
            elif prefix.strip() == "[提示]":
                style = "cyan"
            elif prefix.strip() == "[工具调用]":
                style = "bold cyan"
            t = Text(prefix, style=style) if style else Text(prefix)
            CONSOLE.print(t, end="", highlight=False, soft_wrap=True)
        else:
            self.write(prefix)

    def newline(self) -> None:
        if self.current_col != 0:
            if self._use_rich and CONSOLE is not None:
                CONSOLE.print()
            else:
                sys.stdout.write("\n")
                sys.stdout.flush()
            self.current_col = 0


class UserInterruptError(Exception):
    pass
