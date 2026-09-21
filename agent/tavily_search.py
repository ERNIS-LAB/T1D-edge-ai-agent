# agent/tavily_search.py
# LangChain tools that wrap the Tavily Search API for web search.
# Requires TAVILY_API_KEY to be set in config.py.
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from langchain_core.tools import tool

from config import TAVILY_API_KEY

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"


def _tavily_post(url: str, body: dict[str, Any]) -> Any:
    """Make a POST request to the Tavily API and return parsed JSON."""
    if not TAVILY_API_KEY:
        raise RuntimeError(
            "TAVILY_API_KEY is not set. "
            "Add your API key in config.py to use web search tools."
        )

    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TAVILY_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"Tavily API error {exc.code}: {resp_body}") from exc


@tool
def web_search(query: str, max_results: int = 5, topic: str = "general") -> str:
    """Search the web for current information using the Tavily search API.

    Use this when the user asks a question that requires up-to-date information
    from the internet, such as recent news, current guidelines, or facts you
    are unsure about. topic can be 'general', 'news', or 'finance'.
    """
    if max_results <= 0 or max_results > 10:
        max_results = 5

    try:
        resp = _tavily_post(TAVILY_SEARCH_URL, {
            "query": query,
            "max_results": max_results,
            "topic": topic,
            "search_depth": "basic",
            "include_answer": "basic",
        })
    except RuntimeError as exc:
        return str(exc)

    lines: list[str] = []

    answer = resp.get("answer")
    if answer:
        lines.append(f"Summary: {answer}\n")

    results = resp.get("results", [])
    if results:
        lines.append("Sources:")
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            content = r.get("content", "")
            snippet = content[:200].replace("\n", " ").strip()
            lines.append(f"- {title} ({url}): {snippet}")
    elif not answer:
        return f"No results found for '{query}'."

    return "\n".join(lines)


@tool
def web_search_diabetes(query: str, max_results: int = 5) -> str:
    """Search the web specifically for diabetes-related information.

    Automatically scopes results to trusted health and diabetes sources.
    Use this for diabetes management questions, dietary guidelines,
    medication info, or clinical recommendations.
    """
    if max_results <= 0 or max_results > 10:
        max_results = 5

    try:
        resp = _tavily_post(TAVILY_SEARCH_URL, {
            "query": query,
            "max_results": max_results,
            "topic": "general",
            "search_depth": "advanced",
            "include_answer": "advanced",
            "include_domains": [
                "diabetes.org",
                "ncbi.nlm.nih.gov",
                "mayoclinic.org",
                "niddk.nih.gov",
                "cdc.gov",
                "who.int",
                "webmd.com",
                "healthline.com",
                "diabetesforecast.org",
                "joslin.org",
            ],
        })
    except RuntimeError as exc:
        return str(exc)

    lines: list[str] = []

    answer = resp.get("answer")
    if answer:
        lines.append(f"Summary: {answer}\n")

    results = resp.get("results", [])
    if results:
        lines.append("Sources:")
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            content = r.get("content", "")
            snippet = content[:200].replace("\n", " ").strip()
            lines.append(f"- {title} ({url}): {snippet}")
    elif not answer:
        return f"No diabetes-related results found for '{query}'."

    return "\n".join(lines)


@tool
def web_extract(url: str) -> str:
    """Extract the main content from a web page URL as readable text.

    Use this when you have a specific URL and need to read its content,
    for example a link from a previous web search result.
    """
    try:
        resp = _tavily_post(TAVILY_EXTRACT_URL, {
            "urls": [url],
            "format": "markdown",
        })
    except RuntimeError as exc:
        return str(exc)

    results = resp.get("results", [])
    if not results:
        failed = resp.get("failed_results", [])
        if failed:
            error = failed[0].get("error", "unknown error")
            return f"Failed to extract content from {url}: {error}"
        return f"No content extracted from {url}."

    page = results[0]
    raw = page.get("raw_content", "")
    # Truncate very long pages to stay within useful context
    if len(raw) > 4000:
        raw = raw[:4000] + "\n\n[... content truncated]"

    return f"Content from {url}:\n\n{raw}"


TAVILY_TOOLS = [
    web_search,
    web_search_diabetes,
    web_extract,
]
