"""Production worker engine registry."""
import importlib

DEFAULT_ENGINE = "muse"
_ENGINES = {}
_MODULES = {"muse": ("muse", "MuseEngine"), "pi": ("pi", "PiEngine")}


def get_engine(name):
    if name not in _ENGINES and name in _MODULES:
        module_name, class_name = _MODULES[name]
        try:
            module = importlib.import_module(f"codex_workers.engines.{module_name}")
            _ENGINES[name] = getattr(module, class_name)()
        except ImportError as error:
            raise ValueError(f"worker engine {name} is unavailable") from error
    try:
        return _ENGINES[name]
    except KeyError as error:
        raise ValueError(f"unknown worker engine: {name}") from error


def register_engine(name, engine):
    _ENGINES[name] = engine


def unregister_engine(name):
    _ENGINES.pop(name, None)


__all__ = ["DEFAULT_ENGINE", "get_engine", "register_engine", "unregister_engine"]
