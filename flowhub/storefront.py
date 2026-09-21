"""Browser-exported Ozon storefront packets; no browser cookie or script execution.

The browser supplies HTML. This module validates identity/cursors and atomically
commits one packet. A missing continuation is a schema error, never proof of EOF.
"""

import ast
import hashlib
import json
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

from .source_library import identity

ORIGIN = "https://www.ozon.ru"


class StorefrontError(ValueError):
    pass


def shop_url(url, seller, trusted_path=None):
    value = urljoin(ORIGIN, url)
    parsed = urlsplit(value)
    match = re.fullmatch(r"/seller/(?:[^/]+-)?(\d+)/products/", parsed.path)
    slug = re.fullmatch(r"/seller/([^/]+)/products/", parsed.path)
    bound_slug = bool(not match and slug and parsed.path == trusted_path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.ozon.ru"
        or parsed.fragment
        or not (match or bound_slug)
        or (match and match[1] != str(seller))
    ):
        raise StorefrontError("identity_mismatch")
    return value


class PacketHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.widgets = {}
        self.canonical = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href")
        key = attrs.get("id", "")
        if "data-state" in attrs and re.match(
            r"state-(tileGridDesktop|infiniteVirtualPaginator|sellerTransparency)-", key
        ):
            self.widgets[key] = json.loads(attrs["data-state"])


def parse_packet(html, seller, requested_url):
    seller = identity(seller)
    requested_url = shop_url(requested_url, seller, urlsplit(requested_url).path)
    try:
        parser = PacketHTML()
        parser.feed(html)
        match = re.search(r"window\.__NUXT__\.state=('(?:\\.|[^'\\])*')", html)
        if not match:
            raise StorefrontError("captcha_or_missing_state")
        # Literal decoding only. Never eval or execute the page's scripts.
        state = json.loads(ast.literal_eval(match[1]))
        info = state["pageInfo"]
        if info.get("pageType") != "seller" or str(info["analyticsInfo"]["sellerId"]) != seller:
            raise StorefrontError("identity_mismatch")
        actual_url = shop_url(info["url"], seller, urlsplit(info["url"]).path)
        wanted_query = parse_qs(urlsplit(requested_url).query)
        actual_query = parse_qs(urlsplit(actual_url).query)
        if actual_query != wanted_query:
            raise StorefrontError("cursor_mismatch")
        if parser.canonical:
            shop_url(parser.canonical, seller, urlsplit(actual_url).path)
        page = int(actual_query.get("page", ["1"])[0])
        grids = [(k, v) for k, v in parser.widgets.items() if k.startswith("state-tileGridDesktop-")]
        # Ozon's explicit empty sold-out tail is a valid end packet. Require
        # both counters plus the bound URL and empty, separator-only layout;
        # an ordinary missing grid/nextPage remains a schema failure.
        catalog = state.get("shared", {}).get("catalog", {})
        layout = state.get("layout")
        empty_sold_out_end = (
            not grids
            and actual_query.get("sold_out_page") == ["1"]
            and catalog.get("totalPages") == page
            and catalog.get("currentSoldOutPage") == page
            and "nextPage" not in state
            and isinstance(layout, list)
            and bool(layout)
            and all(
                x.get("component") == "separator" and str(x.get("stateId", "")).endswith("-" + str(page))
                for x in layout
            )
        )
        if empty_sold_out_end:
            observed_at = datetime.fromisoformat(
                state["resolveParams"]["current.time"].replace("Z", "+00:00")
            ).timestamp()
            return dict(
                seller_id=seller,
                page=page,
                actual_url=actual_url,
                next_url=None,
                observed_at=observed_at,
                products=[],
                widgets=[],
                explicit_end=True,
                terminal_evidence=dict(kind="empty_sold_out_tail", totalPages=page, currentSoldOutPage=page),
            )

        if not grids or any(not k.endswith("-" + str(page)) for k, _ in grids):
            raise StorefrontError("missing_or_wrong_product_grid")
        continuations = [
            v["nextPage"]
            for k, v in parser.widgets.items()
            if k.startswith("state-infiniteVirtualPaginator-") and "nextPage" in v
        ]
        if "nextPage" in state:
            continuations.append(state["nextPage"])
        if not continuations or len(set(continuations)) != 1:
            raise StorefrontError("missing_or_conflicting_continuation")
        continuation = continuations[0]
        if continuation is not None and not isinstance(continuation, str):
            raise StorefrontError("invalid_continuation")
        next_url = shop_url(continuation, seller, urlsplit(actual_url).path) if continuation else None
        if next_url:
            next_page = int(parse_qs(urlsplit(next_url).query).get("page", ["0"])[0])
            if next_page != page + 1:
                raise StorefrontError("nonadvancing_continuation")
        observed_at = datetime.fromisoformat(
            state["resolveParams"]["current.time"].replace("Z", "+00:00")
        ).timestamp()
        products = {}
        for widget, grid in grids:
            if not isinstance(grid.get("items"), list):
                raise StorefrontError("invalid_product_grid")
            for item in grid["items"]:
                sku = identity(item["sku"])
                path = urlsplit(urljoin(ORIGIN, item["action"]["link"]))
                if (
                    path.netloc != "www.ozon.ru"
                    or not re.search(r"(?:/|-)" + sku + r"/$", path.path)
                    or str(item["id"]) != sku
                ):
                    raise StorefrontError("product_identity_mismatch")
                title, price, price_display = None, None, None
                for bit in item.get("mainState", []):
                    if bit.get("id") == "name":
                        title = bit["textDS"]["text"]
                    for value in bit.get("priceV2", {}).get("price", []):
                        if value.get("textStyle") == "PRICE":
                            price_display = value["text"]
                            # Browser locale/account settings can display CNY or other
                            # currencies. Only explicit ruble prices populate RUB.
                            if "₽" in price_display:
                                amount = re.sub(r"[\s₽]", "", price_display).replace(",", ".")
                                price = float(amount)
                images = item.get("tileImage", {}).get("items", [])
                products[sku] = dict(
                    sku=sku,
                    seller_id=seller,
                    title=title,
                    image=images[0].get("image", {}).get("link") if images else None,
                    current_price_rub=price,
                    current_price_display=price_display,
                    url=ORIGIN + path.path,
                    collected_at=observed_at,
                    coverage="storefront-page",
                    discovery_state="needs_erp_review",
                    widget=widget,
                )
        if not products and next_url:
            raise StorefrontError("empty_continuing_page")
        return dict(
            seller_id=seller,
            page=page,
            actual_url=actual_url,
            next_url=next_url,
            observed_at=observed_at,
            products=list(products.values()),
            widgets=[k for k, _ in grids],
            explicit_end=not next_url,
        )
    except StorefrontError:
        raise
    except (KeyError, TypeError, ValueError, SyntaxError, AttributeError, OverflowError) as error:
        raise StorefrontError("schema:" + type(error).__name__) from None


class StorefrontCollector:
    def __init__(self, library):
        self.library = library
        self.db = library.db
        with self.db.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS storefront_tasks(
              owner TEXT NOT NULL,seller TEXT NOT NULL,roots TEXT NOT NULL,
              next_url TEXT,page INTEGER NOT NULL DEFAULT 1,state TEXT NOT NULL DEFAULT 'paused',
              error TEXT,updated REAL NOT NULL,PRIMARY KEY(owner,seller));
            CREATE TABLE IF NOT EXISTS storefront_attempts(
              id INTEGER PRIMARY KEY,owner TEXT NOT NULL,seller TEXT NOT NULL,page INTEGER NOT NULL,
              at REAL NOT NULL,state TEXT NOT NULL,digest TEXT NOT NULL,body TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS storefront_commit_once
              ON storefront_attempts(owner,seller,digest) WHERE state='committed';
            """)

    def prepare(self, owner, manifest):
        with self.db.connect() as c:
            for seller, roots in manifest.items():
                seller = identity(seller)
                c.execute(
                    "INSERT OR IGNORE INTO storefront_tasks(owner,seller,roots,next_url,updated) VALUES(?,?,?,?,?)",
                    (
                        owner,
                        seller,
                        json.dumps(roots),
                        shop_url(f"/seller/{seller}/products/", seller),
                        time.time(),
                    ),
                )
        return self.tasks(owner)

    def tasks(self, owner):
        with self.db.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT seller,next_url,page,state,error,updated FROM storefront_tasks WHERE owner=? ORDER BY seller",
                    (owner,),
                )
            ]

    def control(self, owner, seller, action):
        if action not in ("pause", "resume", "retry"):
            raise StorefrontError("invalid_action")
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM storefront_tasks WHERE owner=? AND seller=?", (owner, seller)
            ).fetchone()
            if not row:
                raise KeyError(seller)
            if row["state"] == "done":
                return dict(row)
            if action == "resume" and row["state"] == "blocked":
                raise StorefrontError("retry_required")
            c.execute(
                "UPDATE storefront_tasks SET state=?,updated=? WHERE owner=? AND seller=?",
                ("paused" if action == "pause" else "ready", time.time(), owner, seller),
            )
        return next(r for r in self.tasks(owner) if r["seller"] == seller)

    def ingest(self, owner, seller, html, requested_url, artifact="browser-upload"):
        digest = hashlib.sha256(html.encode()).hexdigest()
        now = time.time()
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            task = c.execute(
                "SELECT * FROM storefront_tasks WHERE owner=? AND seller=?", (owner, seller)
            ).fetchone()
            if not task:
                raise KeyError(seller)
            prior = c.execute(
                "SELECT id FROM storefront_attempts WHERE owner=? AND seller=? AND digest=? AND state='committed'",
                (owner, seller, digest),
            ).fetchone()
            if prior:
                return dict(state="replay", added=0, attempt_id=prior[0], next_url=task["next_url"])
            if task["state"] != "ready":
                raise StorefrontError("task_" + task["state"])
            evidence = dict(
                requested_url=requested_url,
                artifact=artifact,
                sha256=digest,
                imported_at=now,
                channel="browser-page-source",
                root_seeds=json.loads(task["roots"]),
            )
            try:
                if shop_url(requested_url, seller, urlsplit(task["next_url"]).path) != task["next_url"]:
                    raise StorefrontError("checkpoint_mismatch")
                packet = parse_packet(html, seller, requested_url)
                if packet["page"] != task["page"]:
                    raise StorefrontError("page_mismatch")
                if packet["observed_at"] > now + 300 or now - packet["observed_at"] > 21600:
                    raise StorefrontError("stale_page")
                content_hash = hashlib.sha256(
                    ",".join(sorted(p["sku"] for p in packet["products"])).encode()
                ).hexdigest()
                for r in c.execute(
                    "SELECT body FROM storefront_attempts WHERE owner=? AND seller=? AND state='committed'",
                    (owner, seller),
                ):
                    if json.loads(r[0]).get("content_hash") == content_hash:
                        raise StorefrontError("repeated_page")
            except StorefrontError as error:
                evidence["reason"] = str(error)
                c.execute(
                    "INSERT INTO storefront_attempts(owner,seller,page,at,state,digest,body) VALUES(?,?,?,?,?,?,?)",
                    (owner, seller, task["page"], now, "failed", digest, json.dumps(evidence)),
                )
                c.execute(
                    "UPDATE storefront_tasks SET state='blocked',error=?,updated=? WHERE owner=? AND seller=?",
                    (str(error), now, owner, seller),
                )
                return dict(state="failed", reason=str(error), added=0, next_url=task["next_url"])
            added = 0
            for product in packet["products"]:
                existing = c.execute(
                    "SELECT 1 FROM sourcing_products WHERE owner=? AND sku=? AND seller=?",
                    (owner, product["sku"], seller),
                ).fetchone()
                # Keep older, richer ERP measurements at their original observation time.
                if not existing:
                    product["source_relation"] = dict(
                        kind="same_seller", seller_id=seller, root_seeds=json.loads(task["roots"])
                    )
                    added += self.library.put(owner, product, evidence, connection=c)
            evidence.update({k: v for k, v in packet.items() if k != "products"})
            evidence.update(
                content_hash=content_hash, skus=[p["sku"] for p in packet["products"]], added=added
            )
            c.execute(
                "INSERT INTO storefront_attempts(owner,seller,page,at,state,digest,body) VALUES(?,?,?,?,?,?,?)",
                (owner, seller, task["page"], now, "committed", digest, json.dumps(evidence)),
            )
            c.execute(
                "UPDATE storefront_tasks SET next_url=?,page=page+1,state=?,error=NULL,updated=? WHERE owner=? AND seller=?",
                (packet["next_url"], "done" if packet["explicit_end"] else "ready", now, owner, seller),
            )
            return dict(
                state="committed", added=added, rows=len(packet["products"]), next_url=packet["next_url"]
            )
