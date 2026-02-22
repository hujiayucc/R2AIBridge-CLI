from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CommandContext:
    bridge: Any
    schema_module: Any
    schema_loaded: bool
    current_config: dict

    analyzer: Optional[Any] = None

    kb_items: list[dict] = field(default_factory=list)

    active_session_id: str = ""
    known_sessions: set[str] = field(default_factory=set)

    should_exit: bool = False

    last_ai_trace_id: str = ""
