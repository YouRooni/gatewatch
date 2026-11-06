from importlib.metadata import entry_points
from typing import AsyncIterator, Protocol

from .model import Event


class Source(Protocol):
    def __init__(self, config: dict): ...
    def records(self, cursor: str | None) -> AsyncIterator[tuple[str, list[Event]]]: ...


class Channel(Protocol):
    def __init__(self, config: dict, store, channel_id: str): ...
    async def deliver(self, node: dict, card: dict, receipt: dict | None) -> dict: ...
    async def close(self) -> None: ...


def plugin(group, name):
    builtins = {
        ("sources", "journal"): ("gatewatch.sources.journal", "JournalSource"),
        ("sources", "file"): ("gatewatch.sources.file", "FileSource"),
        ("sources", "demo"): ("gatewatch.sources.demo", "DemoSource"),
        ("channels", "telegram"): ("gatewatch.channels.telegram", "TelegramChannel"),
    }
    if (group, name) in builtins:
        import importlib
        module, symbol = builtins[group, name]
        return getattr(importlib.import_module(module), symbol)
    found = entry_points().select(group=f"gatewatch.{group}", name=name)
    if not found:
        raise ValueError(f"Unknown {group} plugin: {name}")
    return next(iter(found)).load()
