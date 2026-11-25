"""Load the integration modules without installing Home Assistant.

The integration files use relative imports, so they must be loaded as a
package. HA is not importable in a plain test run, and the package
__init__.py pulls it in, so the package object is built by hand.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PACKAGE_DIR = os.path.join(ROOT, "custom_components", "kmf_series")
PACKAGE_NAME = "kmf_under_test"


def _ensure_package() -> types.ModuleType:
    """Register a bare package that points at the integration folder."""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [PACKAGE_DIR]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = package
    return package


def load(module_name: str) -> types.ModuleType:
    """Import one module of the integration, e.g. load("parser")."""
    full_name = f"{PACKAGE_NAME}.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    _ensure_package()
    path = os.path.join(PACKAGE_DIR, f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module
