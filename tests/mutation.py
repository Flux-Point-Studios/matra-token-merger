"""One function recompiled from its own source with one piece of it changed,
for the mutation matrices."""

from __future__ import annotations

import inspect
import textwrap
from typing import Callable


def recompiled(function: Callable, original: str, changed: str) -> Callable:
    """``function`` with the one occurrence of ``original`` in its source
    replaced by ``changed``, compiled against its own module's globals."""
    source = textwrap.dedent(inspect.getsource(function))
    assert source.count(original) == 1, f"{original!r} is no longer in {function.__qualname__}"
    namespace: dict = {}
    exec(compile(source.replace(original, changed), inspect.getfile(function), "exec"),
         function.__globals__, namespace)
    return namespace[function.__name__]
