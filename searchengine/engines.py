"""Module to perform a search."""

from . import importer  # isort: skip

import inspect
import json
import sys
from abc import ABC, abstractmethod
from html import unescape
from typing import Any, Optional
from urllib.parse import urlencode, urljoin

import jsonpath_ng
import jsonpath_ng.ext
import searx
import searx.data
import searx.enginelib
import searx.engines
from curl_cffi.requests import AsyncSession, Response
from lxml import etree, html

from .query import ParsedQuery, QueryExtensions, SearchMode
from .results import AnswerResult, ImageResult, Result, WebResult
from .url import Url


class StatusCodeError(Exception):
    """Exception that is raised if a request to an engine doesn't return 2xx."""

    def __init__(self, response: Response) -> None:
        """Initialize the exception w/ an Response object."""
        super().__init__(f"{response.status_code} {response.reason}")


_DEFAULT_QUERY_EXTENSIONS = QueryExtensions(0)


class Engine(ABC):
    """Base class for a search engine."""

    def __init__(
        self,
        name: str,
        *,
        mode: SearchMode = SearchMode.WEB,
        weight: float = 1.0,
        supported_languages: frozenset[str] = frozenset(),
        query_extensions: QueryExtensions = _DEFAULT_QUERY_EXTENSIONS,
        method: str = "GET",
    ) -> None:
        """Initialize engine."""
        self.name = name
        self.mode = mode
        self.weight = weight
        self.supported_languages = supported_languages
        self.query_extensions = query_extensions
        self._method = method

    def _log(self, msg: str, tag: Optional[str] = None) -> None:
        if tag is None:
            print(f"[!] [{self.name}] {msg}")
        else:
            print(f"[!] [{self.name}] [{tag}] {msg}")

    @abstractmethod
    def _request(self, query: ParsedQuery, params: dict[str, Any]) -> dict[str, Any]:
        pass

    @abstractmethod
    def _response(self, response: Response) -> list[Result]:
        pass

    async def search(
        self,
        session: AsyncSession,
        query: ParsedQuery,
    ) -> list[Result]:
        """Perform a search and return the results."""
        params = {
            "searxng_locale": query.lang,
            "language": query.lang,
            "time_range": None,
            "pageno": query.page,
            "safesearch": 2,
            "method": self._method,
            "headers": {},
            "data": None,
            "cookies": {},
        }
        params = self._request(query, params)

        assert isinstance(params["method"], str)
        assert isinstance(params["url"], str)
        assert isinstance(params["headers"], dict)
        assert isinstance(params["cookies"], dict)
        assert isinstance(params["data"], str) or params["data"] is None

        response = await session.request(
            params["method"],
            params["url"],
            headers=params["headers"],
            data=params["data"],
            cookies=params["cookies"],
        )

        if not (200 <= response.status_code < 300):
            raise StatusCodeError(response)

        response.search_params = params  # type: ignore[attr-defined]
        response.url = Url(response.url)
        return self._response(response)


_DEFAULT_PARAMS: dict[str, str] = {}


class _CstmEngine[Path, Element](Engine):
    def __init__(
        self,
        name: str,
        *,
        mode: SearchMode = SearchMode.WEB,
        weight: float = 1.0,
        supported_languages: frozenset[str] = frozenset(),
        query_extensions: QueryExtensions = _DEFAULT_QUERY_EXTENSIONS,
        method: str = "GET",
        url: str,
        query_key: str = "q",
        params: dict[str, str] = _DEFAULT_PARAMS,
        result_path: Path,
        title_path: Path,
        url_path: Path,
        text_path: Path,
        src_path: Optional[Path] = None,
    ) -> None:
        super().__init__(
            name,
            mode=mode,
            weight=weight,
            supported_languages=supported_languages,
            query_extensions=query_extensions,
            method=method,
        )
        if self.mode == SearchMode.IMAGES and src_path is None:
            raise ValueError("src_path is required for image search")
        self._url = url
        self._query_key = query_key
        self._params = params
        self._result_path = result_path
        self._title_path = title_path
        self._url_path = url_path
        self._text_path = text_path
        self._src_path = src_path

    @staticmethod
    @abstractmethod
    def _parse_response(response: Response) -> Element:
        pass

    @staticmethod
    @abstractmethod
    def _iter(root: Element, path: Path) -> list[Element]:
        pass

    @staticmethod
    @abstractmethod
    def _get(root: Element, path: Optional[Path]) -> Optional[str]:
        pass

    def _request(self, query: ParsedQuery, params: dict[str, Any]) -> dict[str, Any]:
        data = {self._query_key: str(query), **self._params}

        params["url"] = (
            f"{self._url}?{urlencode(data)}" if self._method == "GET" else self._url
        )
        params["data"] = json.dumps(data) if self._method == "POST" else None

        return params

    def _response(self, response: Response) -> list[Result]:
        root = self._parse_response(response)

        results: list[Result] = []

        for result in self._iter(root, self._result_path):
            title = self._get(result, self._title_path)
            assert title

            _url = self._get(result, self._url_path)
            assert _url
            url = Url(urljoin(str(response.url), _url))

            text = self._get(result, self._text_path)

            if self.mode == SearchMode.IMAGES:
                src = self._get(result, self._src_path)
                assert src
                results.append(ImageResult(title, url, text or None, Url(src)))
                continue

            results.append(WebResult(title, url, text or None))

        return results


class _XPathEngine(_CstmEngine[etree.XPath, html.HtmlElement]):
    @staticmethod
    def _parse_response(response: Response) -> html.HtmlElement:
        return html.document_fromstring(response.text)

    @staticmethod
    def _iter(root: html.HtmlElement, path: etree.XPath) -> list[html.HtmlElement]:
        elems = path(root)
        assert isinstance(elems, list)
        assert all(isinstance(elem, html.HtmlElement) for elem in elems)
        return elems

    @staticmethod
    def _get(root: html.HtmlElement, path: Optional[etree.XPath]) -> Optional[str]:
        if path is None or not (elems := path(root)):
            return None
        assert isinstance(elems, list)
        if isinstance(elems[0], str):
            return elems[0]
        return html.tostring(
            elems[0],
            encoding="unicode",
            method="text",
            with_tail=False,
        )


class _JSONEngine(_CstmEngine[jsonpath_ng.JSONPath, dict]):
    @staticmethod
    def _parse_response(response: Response) -> dict:
        return response.json()

    @staticmethod
    def _iter(root: dict, path: jsonpath_ng.JSONPath) -> list[dict]:
        return path.find(root)

    @staticmethod
    def _get(root: dict, path: Optional[jsonpath_ng.JSONPath]) -> Optional[str]:
        if path is None or not (elems := path.find(root)):
            return None
        assert isinstance(elems, list)
        return elems[0].value


class _SearxEngine(Engine):
    def __init__(
        self,
        name: str,
        *,
        mode: SearchMode = SearchMode.WEB,
        weight: float = 1.0,
        supported_languages: frozenset[str] = frozenset(),
        query_extensions: QueryExtensions = _DEFAULT_QUERY_EXTENSIONS,
        method: str = "GET",
    ) -> None:
        super().__init__(
            name,
            mode=mode,
            weight=weight,
            supported_languages=supported_languages,
            query_extensions=query_extensions,
            method=method,
        )
        for engine in searx.settings["engines"]:
            if engine["name"] == self.name:
                self._engine = searx.engines.load_engine(engine)
                if self._engine is None:
                    raise ValueError(f"Failed to load searx engine {self.name}")
                break
        else:
            raise ValueError(f"Searx engine {self.name} not found")
        if self._engine.paging:
            self.query_extensions |= QueryExtensions.PAGING

    def _request(self, query: ParsedQuery, params: dict[str, Any]) -> dict[str, Any]:
        self._engine.search_type = self.mode.value  # type: ignore[attr-defined]
        if self.name == "mojeek" and self.mode == SearchMode.WEB:
            self._engine.search_type = ""  # type: ignore[attr-defined]
        return self._engine.request(str(query), params)

    def _response(self, response: Response) -> list[Result]:
        if not response.text:
            return []

        results: list[Result] = []

        for result in self._engine.response(response):
            if "number_of_results" in result or "suggestion" in result:
                continue

            assert "url" in result
            assert isinstance(result["url"], str)
            if not result["url"]:
                continue

            url = Url(result["url"])

            if "answer" in result:
                assert isinstance(result["answer"], str)
                assert result["answer"]
                results.append(AnswerResult(result["answer"], url))
                continue

            assert "title" in result
            assert isinstance(result["title"], str)

            if "img_src" in result:
                src = result.get("thumbnail_src", result["img_src"])
                assert isinstance(src, str)
                assert src
                assert result.get("template") == "images.html"
                results.append(
                    ImageResult(
                        result["title"],
                        url,
                        result.get("content") or None,
                        Url(unescape(src)),
                    )
                )
                continue

            assert result["title"]
            assert "content" in result
            assert isinstance(result["content"], str)
            results.append(
                WebResult(
                    result["title"],
                    url,
                    result["content"] or None,
                )
            )

        return results


_ALEXANDRIA = _JSONEngine(
    "alexandria",
    query_extensions=QueryExtensions.SITE,
    supported_languages=frozenset({"en"}),
    url="https://api.alexandria.org",
    params={"a": "1", "c": "a"},
    result_path=jsonpath_ng.ext.parse("results[*]"),
    title_path=jsonpath_ng.parse("title"),
    url_path=jsonpath_ng.parse("url"),
    text_path=jsonpath_ng.parse("snippet"),
)
_BING = _SearxEngine(
    "bing",
    weight=1.3,
    # TODO: check if bing does support quotation
    query_extensions=QueryExtensions.SITE,
)
_BING_IMAGES = _SearxEngine(
    "bing images",
    mode=SearchMode.IMAGES,
    weight=1.3,
    query_extensions=QueryExtensions.SITE,
)
_GOOGLE = _SearxEngine(
    "google", weight=1.3, query_extensions=QueryExtensions.QUOTES | QueryExtensions.SITE
)
_GOOGLE_IMAGES = _SearxEngine(
    "google images",
    mode=SearchMode.IMAGES,
    weight=1.3,
    query_extensions=QueryExtensions.QUOTES | QueryExtensions.SITE,
)
_MOJEEK = _SearxEngine("mojeek", query_extensions=QueryExtensions.SITE)
_MOJEEK_IMAGES = _SearxEngine("mojeek", mode=SearchMode.IMAGES)
_REDDIT = _SearxEngine("reddit", weight=0.7)
_RIGHT_DAO = _SearxEngine(
    "right dao",
    supported_languages=frozenset({"en"}),
    query_extensions=QueryExtensions.QUOTES | QueryExtensions.SITE,
)
_SESE = _JSONEngine(
    "sese",
    query_extensions=QueryExtensions.SITE,
    url="https://se-proxy.azurewebsites.net/api/search",
    params={"slice": "0:12"},
    result_path=jsonpath_ng.ext.parse("'结果'[?'信息'.'标题' != '']"),
    url_path=jsonpath_ng.parse("'网址'"),
    title_path=jsonpath_ng.parse("'信息'.'标题'"),
    text_path=jsonpath_ng.parse("'信息'.'描述'"),
)
_STRACT = _SearxEngine(
    "stract",
    supported_languages=frozenset({"en"}),
    query_extensions=QueryExtensions.QUOTES
    | QueryExtensions.SITE
    | QueryExtensions.PAGING,
)
_YEP = _SearxEngine("yep", query_extensions=QueryExtensions.SITE)
_YEP_IMAGES = _SearxEngine(
    "yep", mode=SearchMode.IMAGES, query_extensions=QueryExtensions.SITE
)


def get_engines(query: ParsedQuery) -> set[Engine]:
    """Return list of enabled engines for the language."""
    return {
        engine
        for _, engine in inspect.getmembers(sys.modules[__name__])
        if isinstance(engine, Engine)
        if engine.mode == query.mode
        if not engine.supported_languages or query.lang in engine.supported_languages
        if query.required_extensions() in engine.query_extensions
    }
