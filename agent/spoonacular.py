# agent/spoonacular.py
# LangChain tools that wrap the Spoonacular Food API for nutritional lookups.
# Uses the official OpenAPI-generated spoonacular SDK.
from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool
from spoonacular import ApiClient, Configuration, IngredientsApi, RecipesApi
from spoonacular.models.compute_glycemic_load_request import ComputeGlycemicLoadRequest
from spoonacular.rest import ApiException

from config import SPOONACULAR_API_KEY, SPOONACULAR_BASE_URL

# Shared timeout in seconds for all Spoonacular API calls.
_TIMEOUT = 15


def _get_apis() -> tuple[IngredientsApi, RecipesApi]:
    """Return initialized IngredientsApi and RecipesApi clients."""
    if not SPOONACULAR_API_KEY:
        raise RuntimeError(
            "SPOONACULAR_API_KEY is not set. "
            "Add your API key in config.py to use nutritional lookup tools."
        )

    configuration = Configuration(host=SPOONACULAR_BASE_URL)
    configuration.api_key["apiKeyScheme"] = SPOONACULAR_API_KEY

    api_client = ApiClient(configuration)
    return IngredientsApi(api_client), RecipesApi(api_client)


def _format_nutrients(
    nutrients: list[dict[str, Any]] | list[Any],
    keys: set[str] | None = None,
) -> str:
    """Format a list of nutrient dicts or model instances into a readable string."""
    lines: list[str] = []
    for n in nutrients:
        if isinstance(n, dict):
            name = n.get("name", n.get("title", ""))
            amount = n.get("amount", "?")
            unit = n.get("unit", "")
            pct = n.get("percentOfDailyNeeds")
        else:
            name = getattr(n, "name", getattr(n, "title", ""))
            amount = getattr(n, "amount", "?")
            unit = getattr(n, "unit", "")
            pct = getattr(n, "percent_of_daily_needs", None)
        if keys and name.lower() not in keys:
            continue
        pct_str = f" ({pct:.0f}% DV)" if pct is not None else ""
        lines.append(f"{name}: {amount}{unit}{pct_str}")
    return ", ".join(lines) if lines else "No nutrient data available."


MACRO_KEYS = {
    "calories",
    "fat",
    "saturated fat",
    "carbohydrates",
    "sugar",
    "fiber",
    "protein",
    "sodium",
}


@tool
def search_food_nutrition(query: str, number: int = 5) -> str:
    """Search for a food or ingredient and return nutritional information per serving.

    Use this when the user asks about the nutrition, calories, or carbs in a food item.
    """
    if number <= 0 or number > 20:
        number = 5

    try:
        ingredients_api, _ = _get_apis()
        results = ingredients_api.ingredient_search(
            query=query, number=number, _request_timeout=_TIMEOUT
        )
    except (RuntimeError, ApiException) as exc:
        return str(exc)

    items = results.results or []
    if not items:
        return f"No foods found matching '{query}'."

    lines: list[str] = []
    for item in items:
        ingredient_id = item.id
        name = item.name or "unknown"

        try:
            info = ingredients_api.get_ingredient_information(
                id=ingredient_id,
                amount=100,
                unit="grams",
                _request_timeout=_TIMEOUT,
            )
        except ApiException:
            lines.append(f"- {name} (id:{ingredient_id}): nutrition unavailable")
            continue

        nutrients = info.nutrition.nutrients if info.nutrition else []
        summary = _format_nutrients(nutrients, MACRO_KEYS)
        lines.append(f"- {name} (per 100g): {summary}")

    return f"Nutritional info for '{query}':\n" + "\n".join(lines)


@tool
def get_ingredient_nutrition(
    ingredient_id: int, amount: float = 100, unit: str = "grams"
) -> str:
    """Get detailed nutritional info for a specific ingredient by its Spoonacular ID.

    Use search_food_nutrition first to find the ingredient ID.
    """
    try:
        ingredients_api, _ = _get_apis()
        info = ingredients_api.get_ingredient_information(
            id=ingredient_id,
            amount=amount,
            unit=unit,
            _request_timeout=_TIMEOUT,
        )
    except (RuntimeError, ApiException) as exc:
        return str(exc)

    name = info.name or "unknown"
    nutrients = info.nutrition.nutrients if info.nutrition else []
    if not nutrients:
        return f"No nutritional data found for ingredient {ingredient_id}."

    macro = _format_nutrients(nutrients, MACRO_KEYS)
    all_nutrients = _format_nutrients(nutrients)
    return (
        f"Nutrition for {name} ({amount} {unit}):\n"
        f"Macros: {macro}\n"
        f"Full: {all_nutrients}"
    )


@tool
def search_recipes_by_nutrients(
    max_carbs: float | None = None,
    min_protein: float | None = None,
    max_calories: float | None = None,
    max_fat: float | None = None,
    number: int = 5,
) -> str:
    """Search for recipes filtered by nutritional constraints (per serving).

    Useful for finding low-carb or diabetes-friendly meal ideas.
    All nutrient values are in grams except calories (kcal).
    """
    if number <= 0 or number > 20:
        number = 5

    try:
        _, recipes_api = _get_apis()
        results = recipes_api.search_recipes_by_nutrients(
            max_carbs=max_carbs,
            min_protein=min_protein,
            max_calories=max_calories,
            max_fat=max_fat,
            number=number,
            _request_timeout=_TIMEOUT,
        )
    except (RuntimeError, ApiException) as exc:
        return str(exc)

    if not results:
        return "No recipes found matching those nutritional criteria."

    lines: list[str] = []
    for r in results:
        title = r.title or "untitled"
        cals = r.calories or "?"
        carbs = r.carbs or "?"
        fat = r.fat or "?"
        protein = r.protein or "?"
        lines.append(
            f"- {title}: {cals} cal, {carbs} carbs, {protein} protein, {fat} fat"
        )

    filters = []
    if max_carbs is not None:
        filters.append(f"max {max_carbs}g carbs")
    if min_protein is not None:
        filters.append(f"min {min_protein}g protein")
    if max_calories is not None:
        filters.append(f"max {max_calories} cal")
    if max_fat is not None:
        filters.append(f"max {max_fat}g fat")
    filter_str = f" ({', '.join(filters)})" if filters else ""

    return f"Recipes{filter_str}:\n" + "\n".join(lines)


@tool
def get_recipe_nutrition(query: str, number: int = 3) -> str:
    """Search for a recipe by name and return its full nutritional breakdown per serving.

    Use this when the user asks about nutrition in a specific dish or meal.
    """
    if number <= 0 or number > 10:
        number = 3

    try:
        _, recipes_api = _get_apis()
        # The generated SDK response model strips the extra nutrition fields that
        # the API returns when add_recipe_nutrition=True. We use the raw HTTP
        # response body to preserve that data while still leveraging the SDK for
        # authentication, base URL management, and request building.
        response = recipes_api.search_recipes_with_http_info(
            query=query,
            number=number,
            add_recipe_nutrition=True,
            _request_timeout=_TIMEOUT,
        )
        raw = json.loads(response.raw_data.decode())
    except (RuntimeError, ApiException) as exc:
        return str(exc)

    items = raw.get("results", [])
    if not items:
        return f"No recipes found matching '{query}'."

    lines: list[str] = []
    for recipe in items:
        title = recipe.get("title", "untitled")
        servings = recipe.get("servings", "?")
        nutrition = recipe.get("nutrition", {})
        nutrients = nutrition.get("nutrients", [])
        macro = _format_nutrients(nutrients, MACRO_KEYS)
        lines.append(f"- {title} ({servings} servings, per serving): {macro}")

    return f"Recipe nutrition for '{query}':\n" + "\n".join(lines)


@tool
def get_glycemic_index(ingredients: str) -> str:
    """Get the Glycemic Index (GI) and Glycemic Load (GL) for one or more foods.

    Pass a comma-separated list of ingredient names (e.g. "apple, white rice, oats").
    The tool returns the GI and GL for each item as well as the total GL.
    """
    ingredient_list = [i.strip() for i in ingredients.split(",") if i.strip()]
    if not ingredient_list:
        return "Please provide at least one ingredient."

    try:
        _, recipes_api = _get_apis()
        request = ComputeGlycemicLoadRequest(ingredients=ingredient_list)
        result = recipes_api.compute_glycemic_load(
            compute_glycemic_load_request=request,
            _request_timeout=_TIMEOUT,
        )
    except (RuntimeError, ApiException) as exc:
        return str(exc)

    lines: list[str] = []
    for item in result.ingredients:
        name = item.original or "unknown"
        gi = item.glycemic_index if item.glycemic_index is not None else "?"
        gl = item.glycemic_load if item.glycemic_load is not None else "?"
        lines.append(f"- {name}: GI={gi}, GL={gl}")

    total = (
        f"Total Glycemic Load: {result.total_glycemic_load:.2f}"
        if result.total_glycemic_load is not None
        else "Total Glycemic Load: ?"
    )
    return f"Glycemic data for '{ingredients}':\n" + "\n".join(lines) + f"\n{total}"


SPOONACULAR_TOOLS = [
    search_food_nutrition,
    get_ingredient_nutrition,
    search_recipes_by_nutrients,
    get_recipe_nutrition,
    get_glycemic_index,
]
