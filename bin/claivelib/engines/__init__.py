"""Production worker engine registry."""
import importlib

from claivelib import config

# Built-in default. Stored records always carry their engine (v1 records are Muse), so this
# constant is also the fallback when reading state; new launches use default_engine() instead.
DEFAULT_ENGINE = "muse"
_ENGINES = {}
_MODULES = {"muse": ("muse", "MuseEngine"), "pi": ("pi", "PiEngine"),
            "claude": ("claude", "ClaudeEngine"),
            "codex": ("codex", "CodexEngine")}


def default_engine():
    """Engine for new workers that name neither --engine nor a role (config / CLAIVE_ENGINE)."""
    return config.default_engine(DEFAULT_ENGINE)


def get_engine(name):
    if name not in _ENGINES and name in _MODULES:
        module_name, class_name = _MODULES[name]
        try:
            module = importlib.import_module(f"claivelib.engines.{module_name}")
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


__all__ = ["DEFAULT_ENGINE", "default_engine", "get_engine", "register_engine", "unregister_engine"]
