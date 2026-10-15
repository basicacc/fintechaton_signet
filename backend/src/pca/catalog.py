"""Demo product catalogue.

All products are fictional and their return figures are illustrative. They are not
real products or real performance data, and the UI labels them as such.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources

from .models import Product


@cache
def load_catalog() -> dict[str, Product]:
    raw = json.loads(resources.files("pca.data").joinpath("products.json").read_text(encoding="utf-8"))
    return {p["id"]: Product.model_validate(p) for p in raw}
