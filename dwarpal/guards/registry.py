"""Maps guard names (the `guard:` field in policy YAML) to Guard classes."""

import importlib
import pkgutil
from collections.abc import Callable

from dwarpal.guards.base import Guard

_REGISTRY: dict[str, type[Guard]] = {}
_SKIP_MODULES = {"base", "registry"}


def register(name: str) -> Callable[[type[Guard]], type[Guard]]:
    def decorator(cls: type[Guard]) -> type[Guard]:
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            raise ValueError(f"guard {name!r} is already registered by {_REGISTRY[name]}")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def discover() -> None:
    """Import every module in dwarpal.guards so their @register decorators run."""
    import dwarpal.guards as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name not in _SKIP_MODULES:
            importlib.import_module(f"{pkg.__name__}.{mod.name}")


def get_guard_class(name: str) -> type[Guard]:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise KeyError(f"unknown guard {name!r} (registered: {known})") from None


def registered_guards() -> dict[str, type[Guard]]:
    return dict(_REGISTRY)
