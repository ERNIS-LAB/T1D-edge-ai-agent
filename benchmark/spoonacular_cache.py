# benchmark/spoonacular_cache.py
# Read-through cache wrapping the Spoonacular tools for benchmark use.
#
# Spoonacular's free tier is rate/quota limited, so re-calling the API on every
# benchmark run is wasteful and flaky. These wrappers mirror the real tools in
# agent/spoonacular.py (same names, same signatures) but serve responses from a
# local JSON cache. On a cache miss they call the real tool once, store the
# result, and return it — so the first run populates the cache and every run
# afterwards is offline and deterministic.
#
# Set BENCHMARK_SPOONACULAR_OFFLINE=1 to forbid live calls entirely (a cache
# miss then returns a clear marker instead of hitting the network) — useful in
# CI where no API key is configured.
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from agent.spoonacular import (
    get_glycemic_index as _real_get_glycemic_index,
)
from agent.spoonacular import (
    get_ingredient_nutrition as _real_get_ingredient_nutrition,
)
from agent.spoonacular import (
    search_food_nutrition as _real_search_food_nutrition,
)

CACHE_PATH = Path(
    os.environ.get(
        "BENCHMARK_SPOONACULAR_CACHE", "benchmark_cache/spoonacular_cache.json"
    )
)


def _offline() -> bool:
    return os.environ.get("BENCHMARK_SPOONACULAR_OFFLINE", "").strip() not in ("", "0")


def _load_cache() -> dict[str, str]:
    if CACHE_PATH.exists():
        try:
            data = json.loads(CACHE_PATH.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict[str, str]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _norm_ingredients(ingredients: str) -> str:
    """Order- and case-insensitive normalization so "a, b" and "B,A" share a key."""
    parts = sorted(i.strip().lower() for i in ingredients.split(",") if i.strip())
    return ",".join(parts)


def _cached_call(key: str, real_tool: Any, tool_args: dict[str, Any]) -> str:
    cache = _load_cache()
    if key in cache:
        return cache[key]
    if _offline():
        return (
            f"[spoonacular cache miss in offline mode for '{key}'. "
            "Run benchmark/spoonacular_cache.py to populate the cache.]"
        )
    result = str(real_tool.invoke(tool_args))
    cache[key] = result
    _save_cache(cache)
    return result


# ---------------------------------------------------------------------------
# Cached tool surfaces — names/signatures match agent/spoonacular.py so that
# task `expected_tools` matching is unaffected.
# ---------------------------------------------------------------------------


@tool
def get_glycemic_index(ingredients: str) -> str:
    """Get the Glycemic Index (GI) and Glycemic Load (GL) for one or more foods.

    Pass a comma-separated list of ingredient names (e.g. "apple, white rice, oats").
    The tool returns the GI and GL for each item as well as the total GL.
    """
    key = f"get_glycemic_index:{_norm_ingredients(ingredients)}"
    return _cached_call(key, _real_get_glycemic_index, {"ingredients": ingredients})


@tool
def search_food_nutrition(query: str, number: int = 5) -> str:
    """Search for a food or ingredient and return nutritional information per serving.

    Use this when the user asks about the nutrition, calories, or carbs in a food item.
    """
    key = f"search_food_nutrition:{query.strip().lower()}:{number}"
    return _cached_call(
        key, _real_search_food_nutrition, {"query": query, "number": number}
    )


@tool
def get_ingredient_nutrition(
    ingredient_id: int, amount: float = 100, unit: str = "grams"
) -> str:
    """Get detailed nutritional info for a specific ingredient by its Spoonacular ID.

    Use search_food_nutrition first to find the ingredient ID.
    """
    key = f"get_ingredient_nutrition:{ingredient_id}:{amount}:{unit.strip().lower()}"
    return _cached_call(
        key,
        _real_get_ingredient_nutrition,
        {"ingredient_id": ingredient_id, "amount": amount, "unit": unit},
    )


CACHED_SPOONACULAR_TOOLS = [
    get_glycemic_index,
    search_food_nutrition,
    get_ingredient_nutrition,
]


# ---------------------------------------------------------------------------
# Cache pre-warming — run this module directly to populate the cache for the
# food items referenced by the food-glycemic benchmark tasks.
# ---------------------------------------------------------------------------

# Food pairs/sets used by the food_gi_* benchmark tasks. Each food is also
# warmed individually in case the agent queries them one at a time.
_PREWARM_GI_GROUPS: list[list[str]] = [
    ["oats", "white bread"],
    ["lentils", "white rice"],
    ["apple", "watermelon"],
    ["baked potato", "sweet potato"],
    ["oats", "white bread", "lentils"],
]


def prewarm() -> dict[str, str]:
    """Populate the cache for every GI query the food tasks may issue."""
    if _offline():
        raise RuntimeError(
            "Refusing to prewarm in offline mode "
            "(unset BENCHMARK_SPOONACULAR_OFFLINE)."
        )

    foods: set[str] = set()
    for group in _PREWARM_GI_GROUPS:
        foods.update(group)
        # combined query (the natural way to compare)
        get_glycemic_index.invoke({"ingredients": ", ".join(group)})
    # individual queries
    for food in sorted(foods):
        get_glycemic_index.invoke({"ingredients": food})

    return _load_cache()


if __name__ == "__main__":
    cache = prewarm()
    print(f"Spoonacular cache populated: {len(cache)} entries at {CACHE_PATH}")
    for k in sorted(cache):
        print(f"  - {k}")
