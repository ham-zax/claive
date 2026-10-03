"""Production worker engine registry."""
from codex_workers.engines.muse import MuseEngine, MUSE_MODEL

_ENGINES = {"muse": MuseEngine()}


def get_engine(name):
    try:
        return _ENGINES[name]
    except KeyError as error:
        raise ValueError(f"unknown worker engine: {name}") from error


def register_engine(name, engine):
    _ENGINES[name] = engine


def unregister_engine(name):
    if name != "muse":
        _ENGINES.pop(name, None)


__all__ = ["get_engine", "register_engine", "unregister_engine", "MUSE_MODEL"]
