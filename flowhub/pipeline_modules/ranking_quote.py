"""Exact SKU/seller asking quotes for ranking candidates; no publication writes."""
import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

ORIGIN = 'https://www.ozon.ru'
SOURCE = 'ozon-ranking-quote'


class QuoteHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.widgets = {'main': [], 'seller': [], 'price': []}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for name, prefix in [('main', 'state-webProductMainWidget-'),
                             ('seller', 'state-webCurrentSeller-'), ('price', 'state-webPrice-')]:
            if attrs.get('id', '').startswith(prefix):
                self.widgets[name].append(json.loads(attrs['data-state']))


def product_url(value, sku):
    parsed = urlsplit(urljoin(ORIGIN, value))
    return (parsed.scheme == 'https' and parsed.netloc == 'www.ozon.ru'
            and not parsed.fragment
            and bool(re.fullmatch(r'/product/(?:[^/]+-)?' + re.escape(sku) + r'/', parsed.path)))


def parse_quote(product, packet, now=None):
    now = time.time() if now is None else now
    sku, seller = str(product['sku']), str(product['seller_id'])
    stamp = packet.get('observed_at')
    if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp) or not 0 <= now - stamp < 21600):
        raise ValueError('stale_quote_page')
    if not product_url(packet.get('url', ''), sku):
        raise ValueError('quote_sku_mismatch')
    parser = QuoteHTML()
    parser.feed(packet['html'])
    if any(len(rows) != 1 for rows in parser.widgets.values()):
        raise ValueError('quote_widgets_missing_or_ambiguous')
    main, shop, price = (parser.widgets[k][0] for k in ('main', 'seller', 'price'))
    if str(main.get('sku')) != sku or not product_url(main.get('url', ''), sku):
        raise ValueError('quote_sku_mismatch')
    link = shop.get('sellerCell', {}).get('common', {}).get('action', {}).get('link', '')
    parsed = urlsplit(urljoin(ORIGIN, link))
    numeric_link = re.fullmatch(r'/seller/(?:[^/]+-)?([0-9]+)/?', parsed.path)
    action = shop.get('header', {}).get('badge', {}).get('subscribed', {}).get('common', {}).get('action', {})
    seller_ids = {numeric_link[1]} if numeric_link else set()
    if action.get('id') == 'sisUnlike' and action.get('params', {}).get('sellerId') is not None:
        seller_ids.add(str(action['params']['sellerId']))
    if (parsed.scheme != 'https' or parsed.netloc != 'www.ozon.ru'
            or not re.fullmatch(r'/seller/[^/]+/?', parsed.path) or seller_ids != {seller}):
        raise ValueError('quote_wrong_seller')
    bound = urlsplit(urljoin(ORIGIN, price.get('link', '')))
    if (bound.scheme != 'https' or bound.netloc != 'www.ozon.ru'
            or parse_qs(bound.query).get('product_id') != [sku]):
        raise ValueError('quote_sku_mismatch')
    if price.get('isAvailable') is not True:
        raise ValueError('quote_product_unavailable')
    # Ordinary asking price only: never card/member price, crossed-out price or sales average.
    raw = price.get('price')
    text = str(raw).replace(r'\u2009', '').replace(r'\u00a0', '')
    match = re.fullmatch(r'([0-9]+(?:[.,][0-9]{1,2})?)(₽|¥|￥)', re.sub(r'\s', '', text))
    if not match:
        raise ValueError('quote_currency_or_amount_invalid')
    value = float(match[1].replace(',', '.'))
    if not math.isfinite(value) or value <= 0:
        raise ValueError('quote_currency_or_amount_invalid')
    return {'value': value, 'currency': 'RUB' if match[2] == '₽' else 'CNY',
            'observed_at': stamp, 'source': SOURCE, 'sku': sku, 'seller_id': seller,
            'raw': raw, 'url': packet['url'],
            'body_sha256': hashlib.sha256(packet['html'].encode()).hexdigest()}


async def read(db, owner, product):
    step = {'source': SOURCE, 'fields': []}
    config_path = db.directory / 'source-loop.json'
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if not config.get('enabled') or config.get('owner') != owner or not config.get('profile'):
        return None, step | {'reason': 'quote_source_browser_unconfigured'}
    # The source loop holds its lock across unrelated ERP/database work. A separate
    # public-page profile avoids starving quote reads or delaying seed collection.
    with (db.directory / 'ranking-quote.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None, step | {'reason': 'quote_source_browser_busy', 'failure_class': 'remote_pending'}
        process = None
        try:
            env = os.environ | {'FLOWHUB_SOURCE_PROFILE': str(db.directory / 'ranking-quote-browser-profile'),
                                'FLOWHUB_DATA': str(db.directory.resolve())}
            for key, variable in [('extension_dir', 'FLOWHUB_SOURCE_EXTENSION_DIR'),
                                  ('chromium_executable', 'FLOWHUB_SOURCE_CHROMIUM_EXECUTABLE')]:
                if config.get(key):
                    env[variable] = config[key]
            bridge = Path(__file__).resolve().parents[2] / 'bridges/ranking-quote.mjs'
            process = await asyncio.create_subprocess_exec('node', str(bridge), str(product['sku']),
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, _ = await asyncio.wait_for(process.communicate(), 50)
            packet = json.loads(output)
            if process.returncode or packet.get('error'):
                reason = packet.get('error', 'quote_browser_failed')
                transient = reason in ('TimeoutError', 'Error', 'browser_http_429',
                                       'browser_http_500', 'browser_http_502', 'browser_http_503', 'browser_http_504')
                return None, step | {'reason': reason,
                                    **({'failure_class': 'network'} if transient else {})}
            quote = parse_quote(product, packet)
            return quote, step | {'reason': 'exact_sku_seller_asking_price',
                                   'fields': ['sale_price'], 'quote': quote}
        except (TimeoutError, OSError) as error:
            return None, step | {'reason': type(error).__name__, 'failure_class': 'network'}
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            return None, step | {'reason': str(error) if type(error) is ValueError else type(error).__name__}
        finally:
            # Cancellation must close the browser before releasing this profile's lock.
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
