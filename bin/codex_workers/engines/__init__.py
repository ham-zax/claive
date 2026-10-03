"""Production worker engine registry."""
from codex_workers.engines.muse import MuseEngine, MUSE_MODEL

_ENGINES = {"muse": MuseEngine()}


def get_engine(name):
    try:
        return _ENGINES[name]
    except KeyError as error:
        raise ValueError(f"unknown worker engine: {name}") from error


__all__ = ["get_engine", "MUSE_MODEL"]
