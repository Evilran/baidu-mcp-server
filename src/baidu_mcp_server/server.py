from mcp.server.fastmcp import FastMCP, Context
import httpx
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass
import sys
import traceback
import asyncio
from datetime import datetime, timedelta
import re
import readabilipy.simple_json
import markdownify
import logging
from functools import wraps
from urllib.parse import urlencode

# Dynamic import of Playwright to avoid early import errors
def _import_playwright():
    from playwright.async_api import async_playwright

    return async_playwright


_browser = None
_browser_context = None
_playwright_instance = None
_browser_lock: Optional[asyncio.Lock] = None


async def _ensure_browser(
    user_agent: Optional[str] = None, extra_headers: Optional[Dict[str, str]] = None
):
    """Ensure a browser instance is available with SSL validation disabled"""
    global _browser, _browser_context, _playwright_instance, _browser_lock

    if _browser_lock is None:
        _browser_lock = asyncio.Lock()

    async with _browser_lock:
        if _browser is None or _browser_context is None:
            playwright_module = _import_playwright()
            _playwright_instance = await playwright_module().start()
            _browser = await _playwright_instance.chromium.launch()

            context_kwargs: Dict[str, Any] = {"ignore_https_errors": True}
            if user_agent:
                context_kwargs["user_agent"] = user_agent
            if extra_headers:
                context_kwargs["extra_http_headers"] = extra_headers
            _browser_context = await _browser.new_context(**context_kwargs)
        elif extra_headers:
            await _browser_context.set_extra_http_headers(extra_headers)

    return _browser, _browser_context


logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

@dataclass
class SearchResult:
    title: str
    link: str
    snippet: str
    position: int


class RateLimiter:
    def __init__(self, requests_per_minute: int = 30):
        self.requests_per_minute = requests_per_minute
        self.window = timedelta(minutes=1)
        self.requests: List[datetime] = []
    
    async def acquire(self):
        now = datetime.now()
        self.requests = [req for req in self.requests if now - req < self.window]
        
        if len(self.requests) >= self.requests_per_minute:
            wait_time = max(0, 60 - (now - self.requests[0]).total_seconds())
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                
        self.requests.append(now)

class BaiduSearcher:
    BASE_URL = "https://m.baidu.com/s"
    WEB_NORMAL = "1599"
    WEB_VIDEO_NORMAL = "48304"
    WEB_NOTE_NORMAL = "61570"
    WEB_KNOWLEDGE = "1529"
    WEB_WENKU = "1525"
    
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Mobile Safari/537.36",
        "Referer": "https://m.baidu.com",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9,zh;q=0.8,zh-CN;q=0.7",
        "Sec-Ch-Ua": '"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"',
        "Sec-Ch-Ua-Mobile": "?1",
        "Sec-ch-Ua-Platform": "Android",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"
    }

    def __init__(self, fetcher: Optional["WebContentFetcher"] = None):
        self.rate_limiter = RateLimiter()
        self.fetcher = fetcher

    async def _log_ctx(self, ctx: Optional[Context], level: str, message: str) -> None:
        """Safely emit MCP context logs when a context object is available."""
        if not ctx:
            return
        log_fn = getattr(ctx, level, None)
        if asyncio.iscoroutinefunction(log_fn):
            await log_fn(message)
        elif log_fn:
            log_fn(message)

    def _get_fetcher(self) -> "WebContentFetcher":
        if self.fetcher is None:
            self.fetcher = WebContentFetcher()
        return self.fetcher

    def handle_errors(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except httpx.HTTPError as e:
                logger.error("HTTP error occurred: %s", e, exc_info=True)
                return []
            except Exception as e:
                logger.error("Unexpected error: %s", e, exc_info=True)
                return []
        return wrapper

    def _extract_text(
        self,
        element: Any,
        selector: Optional[str] = None,
        class_name: Optional[Any] = None,
        attrs: Optional[Dict[str, Any]] = None,
    ) -> str:
        if element is None:
            return ""

        find_kwargs: Dict[str, Any] = {}
        if class_name is not None:
            find_kwargs["class_"] = class_name
        if attrs is not None:
            find_kwargs["attrs"] = attrs

        if selector:
            found = element.find(selector, **find_kwargs)
        elif find_kwargs:
            found = element.find(**find_kwargs)
        else:
            found = element

        if not found:
            return ""
        return found.get_text(strip=True)

    def _parse_labels(self, element: Any) -> List[str]:
        if element is None:
            return []

        labels = []
        for tag in element.find_all(["span", "div"], class_=["sc-tag", "cos-tag"]):
            if tag and tag.text:
                labels.append(tag.text.strip())
        return labels

    def _extract_url(self, element: Any) -> str:
        if element is None:
            return ""

        # Prefer Baidu provided redirect attribute when present
        direct = element.attrs.get("rl-link-href")
        if direct:
            return direct.strip()

        link = element.find("a")
        if not link:
            return ""

        href = link.get("href", "").strip()
        return href

    def _parse_abstract(self, element: Any) -> str:
        if element is None:
            return ""

        abstract_div = element.find(attrs={"data-module": "abstract"})
        if not abstract_div:
            # Try video abstract format
            abstract_span = element.find("span", class_=re.compile(r"^abstract-text_"))
            return abstract_span.text.strip() if abstract_span else ""
        
        text_parts = []
        for div in abstract_div.findAll("div", role="text"):
            for span in div.findAll("span"):
                if not span.find("span") and not span.get("class", ["c-color-gray"]):
                    text_parts.append(span.text.strip())
        return "\n".join(filter(None, text_parts))

    def _create_search_result(self, data: Dict[str, Any], position: Optional[int] = None) -> SearchResult:
        return SearchResult(
            title=data.get("title", ""),
            link=data.get("url", ""),
            snippet=self._format_snippet(data),
            position=position
        )

    def _format_snippet(self, data: Dict[str, Any]) -> str:
        parts = []
        if data.get("abstract"):
            parts.append(f"# Abstract\n{data['abstract']}")
        if data.get("labels"):
            parts.append(f"# Labels\n{','.join(data['labels'])}")
        if data.get("content"):
            parts.append(f"# Content Preview\n{data['content']}")
        return "\n\n".join(parts)

    def _parse_search_page(self, html: str, seen_urls: Set[str]) -> List[Dict[str, Any]]:
        results = []
        soup = BeautifulSoup(html, "html.parser")
        if not soup:
            return []
        res_normal_container = soup.findAll(
            "div", class_="c-result result", new_srcid=self.WEB_NORMAL
        )
        res_normal = []
        for res in res_normal_container:
            _ = res.find("div", class_="c-result-content").find("article")
            header = _.find("section").find("div")
            # 标签
            labels = []
            # 标题
            title = header.find(attrs={"data-module": "title"})
            if title:
                title = title.find("span", class_="tts-b-hl").text
            # 链接
            try:
                url = _["rl-link-href"]
            except Exception:
                url = header.find("a")["href"]
            __ = header.find(attrs={"data-module": "title"})
            if __:
                __ = __.find("span", class_="sc-tag")
            # “官方”标签
            if __:
                labels.append(__.text)
            section = _.find("section")
            # 简介
            des = ""
            # 可能有多个`span`标签，需要依次解析
            ___ = _.find(attrs={"data-module": "abstract"})
            if ___:
                for s in ___.findAll("div", role="text"):
                    for t in s.findAll("span"):
                        try:
                            if t.find("span").text:
                                continue
                        except Exception:
                            pass
                        try:
                            if "c-color-gray" in t["class"]:
                                continue
                        except Exception:
                            pass
                        des += t.text
                    des += "\n"
            des = des.strip("\n")
            # 来源（作者）
            origin = section.find("span", class_="cosc-source-text")
            if origin:
                origin = origin.text
            else:
                if __:
                    origin = __.find("div", class_="single-text")
                    if origin:
                        origin = origin.text
            
            
            res_normal.append(
                {
                    "title": title,
                    "url": url,
                    "labels": labels,
                    "abstract": des,
                    "origin": origin,
                    "type": "web",
                }
            )



        res_wenku_container = soup.findAll(
            "div", class_="c-result result", new_srcid=self.WEB_WENKU
        )
        res_wenku_normal = []
        for res in res_wenku_container:
            _ = res.find("div", class_="c-result-content").find("article")
            header = _.find("section").find("div")
            # 标签
            labels = []
            # 标题
            title = header.find(attrs={"data-module": "title"}).find("span", class_="tts-b-hl").text
            # 链接
            try:
                url = _["rl-link-href"]
            except Exception:
                url = header.find("a")["href"]
            
            __ = header.find(attrs={"data-module": "title"}).find("span", class_="sc-tag")
            # “官方”标签
            if __:
                labels.append(__.text)
            section = _.find("section")
            # 简介
            des = ""
            # 可能有多个`span`标签，需要依次解析
            for s in _.find(attrs={"data-module": "abstract"}).findAll("div", role="text"):
                for t in s.findAll("span"):
                    try:
                        if t.find("span").text:
                            continue
                    except Exception:
                        pass
                    try:
                        if "c-color-gray" in t["class"]:
                            continue
                    except Exception:
                        pass
                    des += t.text
                des += "\n"
            des = des.strip("\n")
            # 来源（作者）
            origin = section.find("span", class_="cosc-source-text")
            if origin:
                origin = origin.text
            else:
                origin = __.find("div", class_="single-text")
                if origin:
                    origin = origin.text
            
            

            res_wenku_normal.append(
                {
                    "title": title,
                    "url": url,
                    "labels": labels,
                    "abstract": des,
                    "origin": origin,
                    "type": "doc",
                }
            )

        res_video_normal_container = soup.findAll(
            "div", class_="c-result result", new_srcid=self.WEB_VIDEO_NORMAL
        )


        res_video_normal = []
        for res in res_video_normal_container:
            _ = res.find("div", class_="c-result-content").find("article")
            header = _.find("section").find("div")
            title = header.find("div", class_="title-container").find("p").find("span").text
            # 链接
            try:
                url = _["rl-link-href"]
            except Exception:
                url = header.find("a")["href"]
            __ = _.findAll("span", class_="cos-tag")
            labels = []
            for ___ in __:
                labels.append(___.text)
        
            
            pattern = re.compile(r"^abstract-text_")  # 匹配以 "abstract-text_" 开头的类名
            des = ""
            text = _.find("span", class_=pattern)

            if text:
                des = text.text.strip()
                
        
            origin = res.find("span", class_="cosc-source-text")
            if origin:
                origin = origin.text
            else:
                origin = __.find("div", class_="single-text")
                if origin:
                    origin = origin.text
                
            res_video_normal.append(
                {
                    "title": title,
                    "url": url,
                    "origin": origin,
                    "labels": labels,
                    "abstract": des,
                    "type": "video",
                }
            )
        

        res_note_normal_container = soup.findAll(
            "div", class_="c-result result", new_srcid=self.WEB_NOTE_NORMAL
        )


        res_note_normal = []
        for res in res_note_normal_container:
            _ = res.find("div", class_="c-result-content").find("article")
            __ = _.find("section").find("div").find("div", attrs={"data-module": "sc_lk"})
            try:
                url = __["rl-link-href"]
            except Exception:
                url = __.find("a")["href"]
        
            title = __.find(attrs={"data-module": "title"}).find("span", class_="cosc-title-slot").text
            if not header:
                continue
            des = ""
            labels = []

            source = __.find(attrs={"data-module": "source"})
            for label in source.findAll("div"):
                if not label.find("div") and len(label.text) > 0:
                    labels.append(label.text)



            origin = __.find("div", class_=re.compile(r"^source-name"))
            if origin:
                origin = origin.text
            else:
                origin = __.find("div", class_="single-text")
                if origin:
                    origin = origin.text
            

            res_note_normal.append(
                {
                    "title": title,
                    "url": url,
                    "origin": origin,
                    "labels": labels,
                    "abstract": des,
                    "type": "note",
                }
            )

        res_knowledge_normal_container = soup.findAll(
            "div", class_="c-result result", new_srcid=self.WEB_KNOWLEDGE
        )


        res_knowledge_normal = []

        for res in res_knowledge_normal_container:
            _ = res.find("div", class_="c-result-content").find("article")
            __ = _.find("section").find("div", attrs={"data-module": "lgtte"})
            try:
                url = _["rl-link-href"]
            except Exception:
                url = __.find("a")["href"]
        
            title = __.find("div", class_="c-title").text
            des = ""
            labels = []
            lgtt = _.find("section").find("div", attrs={"data-module": "lgtt"})
            ___ = lgtt.find("div", class_=re.compile(r"^c-line-"))
            if ___:
                des = ___.text.strip()
            
            origin = _.find("div", class_="c-color-source")
            if origin:
                origin = origin.text
            else:
                origin = _.find("div", class_="single-text")
                if origin:
                    origin = origin.text
            
            
            res_knowledge_normal.append(
                {
                    "title": title,
                    "url": url,
                    "origin": origin,
                    "labels": labels,
                    "abstract": des,
                    "type": "knowledge",
                }
            )
        results.extend(res_normal)
        results.extend(res_wenku_normal)
        results.extend(res_knowledge_normal)
        results.extend(res_note_normal)
        results.extend(res_video_normal)
        return results

    async def _request_with_retries(
        self,
        browser_context: Any,
        params: Dict[str, Any],
        max_retries: int,
    ) -> Optional[str]:
        last_error: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            page = None
            try:
                await self.rate_limiter.acquire()
                page = await browser_context.new_page()
                target_url = f"{self.BASE_URL}?{urlencode(params)}"
                response = await page.goto(
                    target_url, wait_until="domcontentloaded", timeout=30_000
                )
                if response and not response.ok:
                    raise RuntimeError(
                        f"Baidu responded with status {response.status}"
                    )
                html = await page.content()
                return html
            except Exception as exc:
                last_error = exc
                logger.warning("Baidu request failed on attempt %d: %s", attempt, exc)
            finally:
                if page:
                    await page.close()

            await asyncio.sleep(min(5 * attempt, 15))

        if last_error:
            logger.error("All retries exhausted when querying Baidu: %s", last_error)
        return None

    async def _perform_search(
        self,
        query: str,
        max_results: int,
        deep_mode: bool,
        max_retries: int,
        ctx: Optional[Context] = None,
    ) -> List[SearchResult]:
        await self._log_ctx(ctx, "info", f"Searching Baidu for: {query}")

        params = {"word": query}
        results: List[Dict[str, Any]] = []
        seen_urls: Set[str] = set()
        page = 0

        user_agent = self.HEADERS.get("User-Agent")
        extra_headers = {
            key: value
            for key, value in self.HEADERS.items()
            if key.lower() != "user-agent"
        }
        _, browser_context = await _ensure_browser(
            user_agent=user_agent, extra_headers=extra_headers or None
        )

        if not browser_context:
            await self._log_ctx(ctx, "error", "Failed to initialize Playwright browser")
            return []

        while len(results) < max_results:
            params["pn"] = page * 10
            page += 1

            html = await self._request_with_retries(browser_context, params, max_retries)
            if html is None:
                await self._log_ctx(
                    ctx, "error", "Failed to fetch search results from Baidu"
                )
                break

            page_results = self._parse_search_page(html, seen_urls)
            if not page_results:
                break

            results.extend(page_results)

        limited_results = results[:max_results]
        if deep_mode and limited_results:
            tasks = [self.process_result(result, idx + 1) for idx, result in enumerate(limited_results)]
            enriched_results = await asyncio.gather(*tasks, return_exceptions=True)
            search_results: List[SearchResult] = []
            for item in enriched_results:
                if isinstance(item, Exception):
                    logger.error("Deep fetch failed for a result: %s", item, exc_info=True)
                    continue
                if isinstance(item, SearchResult):
                    search_results.append(item)
        else:
            search_results = [
                self._create_search_result(result, idx + 1)
                for idx, result in enumerate(limited_results)
            ]

        await self._log_ctx(ctx, "info", f"Successfully found {len(search_results)} results")
        return search_results

    @handle_errors
    async def search(
        self, query: str, ctx: Context, max_results: int = 10, deep_mode: bool = False, max_retries: int = 2,
    ) -> List[SearchResult]:
        return await self._perform_search(
            query=query,
            max_results=max_results,
            deep_mode=deep_mode,
            max_retries=max_retries,
            ctx=ctx,
        )

    @handle_errors
    async def search_fire(
        self, query: str, max_results: int = 10, deep_mode: bool = False, max_retries: int = 2,
    ) -> List[SearchResult]:
        return await self._perform_search(
            query=query,
            max_results=max_results,
            deep_mode=deep_mode,
            max_retries=max_retries,
            ctx=None,
        )

    async def process_result(self, result: Dict[str, Any], position: int) -> SearchResult:
        """Process a single search result with deep content fetching"""
        try:
            fetcher = self._get_fetcher()
            text, url = await fetcher.fetch_and_parse(result["url"])
            if text:
                result["content"] = text
            if url:
                result["url"] = url
        except Exception as e:
            logger.error("Error fetching content for %s: %s", result.get("url"), e, exc_info=True)

        return self._create_search_result(result, position)

    def format_results_for_llm(self, results: List[SearchResult]) -> str:
        """Format results in a natural language style for LLM processing"""
        if not results:
            return "No results were found for your search query. This could be due to Baidu's bot detection or the query returned no matches. Please try rephrasing your search or try again in a few minutes."

        output = [f"Found {len(results)} search results:\n"]
        for result in results:
            output.extend([
                f"{result.position}. {result.title}",
                f"   URL: {result.link}",
                f"   Summary: {result.snippet}",
                ""  # Empty line between results
            ])

        return "\n".join(output)

class WebContentFetcher:
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self):
        self.rate_limiter = RateLimiter(requests_per_minute=20)

    def extract_content_from_html(self, html: str) -> str:
        """Extract and convert HTML content to Markdown format."""
        ret = readabilipy.simple_json.simple_json_from_html_string(
            html, use_readability=True
        )
        raw_content = ret.get("content") if isinstance(ret, dict) else None

        if not raw_content:
            soup = BeautifulSoup(html, "html.parser")
            for element in soup(["script", "style", "nav", "header", "footer"]):
                element.decompose()

            text = soup.get_text(separator=" ")
            cleaned = re.sub(r"\s+", " ", text).strip()
        else:
            cleaned = markdownify.markdownify(
                raw_content,
                heading_style=markdownify.ATX,
            ).strip()

        if len(cleaned) > 150:
            cleaned = f"{cleaned[:150].rstrip()}..."
        return cleaned

    def _extract_client_redirect(self, html: str) -> Optional[str]:
        soup = BeautifulSoup(html, "html.parser")

        script_tag = soup.find("script", string=re.compile(r"window\.location\.replace"))
        if script_tag and script_tag.string:
            match = re.search(r'window\.location\.replace\("([^"]+)"\)', script_tag.string)
            if match:
                return match.group(1)

        meta_tag = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
        if meta_tag:
            content = meta_tag.get("content", "")
            if "url=" in content.lower():
                return content.split("url=", 1)[-1].strip()

        return None

    async def fetch_and_parse(self, url: str, max_redirects: int = 5) -> Tuple[str, str]:
        """Fetch and parse content from a webpage, returning a text preview and the resolved URL."""
        await self.rate_limiter.acquire()

        visited: Set[str] = set()
        target_url = url
        response: Optional[httpx.Response] = None

        async with httpx.AsyncClient(headers=self.HEADERS, timeout=30.0, follow_redirects=True) as client:
            for _ in range(max_redirects):
                if target_url in visited:
                    logger.debug("Detected redirect loop for %s", target_url)
                    break

                visited.add(target_url)
                try:
                    response = await client.get(target_url)
                    response.raise_for_status()
                except httpx.TimeoutException:
                    logger.warning("Timeout while fetching %s", target_url)
                    return "", target_url
                except httpx.HTTPError as exc:
                    logger.error("HTTP error while fetching %s: %s", target_url, exc)
                    return "", target_url

                content_type = response.headers.get("Content-Type", "")
                if "text/html" not in content_type:
                    break

                redirect_url = self._extract_client_redirect(response.text)
                if not redirect_url:
                    break

                target_url = redirect_url

        if not response:
            return "", url

        final_url = str(response.url)
        text = self.extract_content_from_html(response.text)
        return text, final_url
            
# Initialize FastMCP server
mcp = FastMCP("baidu-search")
searcher = BaiduSearcher(WebContentFetcher())


@mcp.tool()
async def search(query: str, ctx: Context, max_results: int = 6, deep_mode: bool = False) -> str:
    """
    Search Baidu and return formatted results.

    Args:
        query: The search query string
        max_results: Maximum number of results to return (default: 6)
        deep_mode: Deep search the web content (default: False)
        ctx: MCP context for logging
    """
    try:
        results = await searcher.search(query, ctx, max_results, deep_mode)
        return searcher.format_results_for_llm(results)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return f"An error occurred while searching: {str(e)}"



def main():
    mcp.run()


if __name__ == "__main__":
    main()
