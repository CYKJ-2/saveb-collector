# 线下订单件数补充：仅访问规则允许的商品站点，图片计数失败时保留数量回退逻辑。
"""Bounded legacy offline item-count requests to operator-approved HTTPS hosts."""

import asyncio
import html
import json
import re
from urllib.parse import urljoin, urlsplit

import httpx

from app.domain.orders import classify, domain


def image_count(text):
    match = re.search(
        r"""class="[^"]*lightgallery-product-images[^"]*"[^>]*data-images=(?:'([^']*)'|"([^"]*)")""",
        text,
        re.IGNORECASE,
    )
    if match:
        decoded = html.unescape(match[1] or match[2])
        try:
            images = json.loads(decoded)
            if isinstance(images, list) and images:
                return len(images)
        except ValueError:
            pass
        count = len(re.findall(r'"src"\s*:', decoded))
        if count:
            return count
    return len(
        {html.unescape(s) for s in re.findall(r'data-largeimg="([^"]+)"', text, re.IGNORECASE)}
    )


async def enrich(rows, context):
    allowed = {domain(host) for host in context["rules"].get("productImageHosts", [])}
    semaphore = asyncio.Semaphore(4)
    cache = {}
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:

        async def fetch(url):
            if url in cache:
                return cache[url]
            count = 0
            original = url
            async with semaphore:
                try:
                    for _ in range(4):
                        parsed = urlsplit(url)
                        if (
                            parsed.scheme != "https"
                            or domain(url) not in allowed
                            or parsed.username
                            or parsed.password
                            or parsed.port not in (None, 443)
                        ):
                            break
                        async with client.stream("GET", url) as response:
                            if response.status_code in (301, 302, 303, 307, 308):
                                url = urljoin(url, response.headers.get("location", ""))
                                continue
                            if response.status_code != 200:
                                break
                            data = bytearray()
                            async for part in response.aiter_bytes():
                                data.extend(part)
                                if len(data) > 2_000_000:
                                    return 0
                            count = image_count(data.decode("utf-8", "replace"))
                            break
                except (httpx.HTTPError, ValueError):
                    pass
            cache[original] = count
            return count

        for row in rows:
            products = row.get("productList") or []
            category, *_ = classify(
                row["order"].get("clientSite"),
                [str(p.get("productName") or "") for p in products],
                context,
            )
            if category != "offline":
                continue
            counts = await asyncio.gather(
                *(fetch(str(p.get("productUrl") or "")) for p in products)
            )
            row["_collector_image_counts"] = counts
            row["_collector_items_method"] = (
                "product-images" if sum(counts) else "quantity-fallback"
            )
    return rows
