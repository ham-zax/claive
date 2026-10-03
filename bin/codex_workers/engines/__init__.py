"""Production worker engine registry."""
import importlib

DEFAULT_ENGINE = "muse"
_ENGINES = {}


def get_engine(name):
    if name not in _ENGINES and name == DEFAULT_ENGINE:
        try:
            module = importlib.import_module("codex_workers.engines.muse")
            _ENGINES[name] = module.MuseEngine()
        except ImportError as error:
            raise ValueError("worker engine muse is unavailable") from error
    try:
        return _ENGINES[name]
    except KeyError as error:
        raise ValueError(f"unknown worker engine: {name}") from error


def register_engine(name, engine):
    _ENGINES[name] = engine


def unregister_engine(name):
    if name != "muse":
        _ENGINES.pop(name, None)


__all__ = ["DEFAULT_ENGINE", "get_engine", "register_engine", "unregister_engine"]
