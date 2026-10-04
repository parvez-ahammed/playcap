"""Adapter loading.

An adapter is any importable module that defines a class named `Adapter`
subclassing playcap.adapters.base.Adapter. config.json picks it by dotted path:

    "adapter": "playcap.adapters.html5_video"
    "adapter": "mysite_adapter"            # any module on sys.path
"""
import importlib

from playcap.adapters.base import Adapter, Item  # noqa: F401  (re-exported)


def load(dotted):
    module = importlib.import_module(dotted)
    cls = getattr(module, "Adapter", None)
    if not (isinstance(cls, type) and issubclass(cls, Adapter) and cls is not Adapter):
        raise ImportError(f"{dotted} does not define an Adapter subclass named 'Adapter'")
    return cls()
