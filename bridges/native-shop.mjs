// Ordinary native HTTP session handling; no browser, imported cookies, or ERP credentials.
const ORIGIN = 'https://www.ozon.ru';

class SessionCookies {
  constructor() { this.cookies = new Map(); }
  receive(headers, url) {
    for (const line of headers.getSetCookie()) {
      const [pair, ...parts] = line.split(';');
      const split = pair.indexOf('=');
      if (split < 1) continue;
      const name = pair.slice(0, split).trim(), value = pair.slice(split + 1);
      const attrs = Object.fromEntries(parts.map(part => {
        const i = part.indexOf('=');
        return i < 0 ? [part.trim().toLowerCase(), true]
          : [part.slice(0, i).trim().toLowerCase(), part.slice(i + 1).trim()];
      }));
      const domain = String(attrs.domain || url.hostname).replace(/^\./, '').toLowerCase();
      if (domain !== url.hostname && domain !== 'ozon.ru') continue;
      const defaultPath = url.pathname.slice(0, url.pathname.lastIndexOf('/')) || '/';
      const path = String(attrs.path || defaultPath);
      if (!path.startsWith('/')) continue;
      if (name.startsWith('__Secure-') && !attrs.secure) continue;
      if (name.startsWith('__Host-') && (!attrs.secure || attrs.domain || path !== '/')) continue;
      const expires = attrs['max-age'] !== undefined ? Date.now() + Number(attrs['max-age']) * 1000
        : attrs.expires ? Date.parse(attrs.expires) : Infinity;
      const key = `${name}:${domain}:${path}`;
      if (expires <= Date.now()) this.cookies.delete(key);
      else this.cookies.set(key, {name, value, path, expires});
    }
  }
  header(url) {
    return [...this.cookies.values()].filter(c => c.expires > Date.now() &&
      (url.pathname === c.path || url.pathname.startsWith(c.path.endsWith('/') ? c.path : c.path + '/')))
      .sort((a, b) => b.path.length - a.path.length).map(c => `${c.name}=${c.value}`).join('; ');
  }
  names() { return [...new Set([...this.cookies.values()].map(c => c.name))]; }
}

export async function nativeShopPage(seller, page = 1, fetchImpl = globalThis.fetch) {
  if (!/^[1-9][0-9]*$/.test(String(seller)) || !Number.isSafeInteger(page) || page < 1)
    throw Error('invalid_shop_request');
  let url = new URL('/api/entrypoint-api.bx/page/json/v2', ORIGIN);
  url.searchParams.set('url', `/seller/${seller}/products/?page=${page}`);
  const diagnostic = {endpoint: url.href, seller_id: String(seller), page,
    observed_at: new Date().toISOString(), coverage: 'native-shop-unverified', steps: []};
  const jar = new SessionCookies();
  const signal = AbortSignal.timeout(15000);
  const fail = error => {
    const retryable=error==='native_network'||error==='native_http_429'||/^native_http_5[0-9]{2}$/.test(error);
    return {ok:false,error,diagnostic:{...diagnostic,retryable,
      recovery:retryable?'retry_same_page_with_backoff':'blocked_requires_source_resolution',
      checkpoint_advance:false}};
  };
  try {
    for (let redirects = 0; redirects <= 3; redirects++) {
      const cookie = jar.header(url);
      const response = await fetchImpl(url, {redirect: 'manual', signal,
        headers: {Accept: 'application/json', ...(cookie ? {Cookie: cookie} : {})}});
      jar.receive(response.headers, url);
      diagnostic.http_status = response.status;
      diagnostic.steps.push({url: url.href, status: response.status,
        observed_at: new Date().toISOString(), cookie_names: jar.names()});
      if (response.status >= 300 && response.status < 400) {
        const location = response.headers.get('location');
        await response.body?.cancel();
        if (!location) return fail('native_redirect_missing_location');
        const next = new URL(location, url);
        // No session cookies sent to another host or arbitrary next-page destination.
        if (next.origin !== ORIGIN || next.username || next.password) return fail('native_redirect_origin');
        if (next.pathname.includes('captcha')) return fail('native_captcha_required');
        if (next.pathname !== url.pathname || next.searchParams.get('url') !== url.searchParams.get('url'))
          return fail('native_redirect_identity');
        diagnostic.redirect = next.href;
        if (redirects === 3) return fail('native_redirect_limit');
        url = next;
        continue;
      }
      const text = await response.text();
      let data;
      try { data = JSON.parse(text); } catch { data = null; }
      if (data?.captchaURL || data?.captchaUrl || /<title>[^<]*captcha/i.test(text)) {
        diagnostic.incident_id = data?.incidentId || null;
        return fail('native_captcha_required');
      }
      if (!response.ok) return fail(`native_http_${response.status}`);
      if (!data || typeof data !== 'object' || Array.isArray(data) ||
          !data.widgetStates || typeof data.widgetStates !== 'object' || Array.isArray(data.widgetStates) ||
          !Object.keys(data.widgetStates).length) return fail('native_schema_unverified');
      // Transport success only: the caller must validate seller membership and pagination.
      return {ok: true, data, diagnostic};
    }
  } catch (error) {
    Object.assign(diagnostic, {exception: error.name,
      cause_code: error.cause?.code || error.code || null, cause: error.cause?.message || error.message});
    return fail('native_network');
  }
}
