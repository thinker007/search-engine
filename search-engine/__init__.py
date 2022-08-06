"""Custom (meta) search engine."""

import asyncio

import httpx
from flask import Flask, make_response, render_template, request

from .engines import get_lang_engines
from .lang import detect_lang
from .query import parse_query
from .results import order_results

application = Flask(__name__)


def error(message: str):
    """Return error page."""
    return render_template("index.html", title="Error", error_message=message)


@application.errorhandler(404)
def page_not_found(_):
    """Return 404 error page."""
    if "text/html" in request.headers.get("Accept", ""):
        return error("Die Seite wurde nicht gefunden"), 404

    response = make_response("404 Not Found", 404)
    response.headers["Content-Type"] = "text/plain"
    return response


@application.route("/")
def index():
    """Return the start page."""
    return render_template("index.html", title="Suche")


@application.route("/search")
def search():
    """Perform a search and return the search result page."""
    query = request.args.get("q", None, str)

    if query is None:
        return error("Wir haben keinen Suchbegriff empfangen können"), 404

    query = query.strip()

    if not query:
        return error("Der Suchbegriff ist leer")

    parsed_query = parse_query(query)

    lang_engines = get_lang_engines(parsed_query.lang)

    async def async_results():
        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=10),
            timeout=httpx.Timeout(5, pool=None),
        ) as client:
            return await asyncio.gather(
                *[
                    engine(client).search(parsed_query)
                    for engine in lang_engines
                ]
            )

    results = asyncio.run(async_results())
    results = order_results(results, parsed_query.lang)

    return render_template(
        "search.html", title=parsed_query.query, query=query, results=results
    )
