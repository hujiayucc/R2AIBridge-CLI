from __future__ import annotations

from typing import Callable, List

import requests

from lib.bridge import JsonRpcError
from lib.commands.context import CommandContext

Handler = Callable[[str, CommandContext], bool]


class CommandRegistry:
    def __init__(self) -> None:
        self._handlers: List[Handler] = []

    def add(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def dispatch(self, raw: str, ctx: CommandContext) -> bool:
        for h in self._handlers:
            try:
                if h(raw, ctx):
                    return True
            except KeyboardInterrupt:
                raise
            except (
                    requests.RequestException,
                    JsonRpcError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                    OSError,
                    RuntimeError,
                    AssertionError,
            ):
                return True
        return False
