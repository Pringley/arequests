import asyncio

from requests.packages.urllib3.poolmanager import PoolManager as _PoolManager, SSL_KEYWORDS
from requests.packages.urllib3.connectionpool import port_by_scheme
from requests.packages.urllib3.util import parse_url

from .connectionpool import HTTPConnectionPool

pool_classes_by_scheme = {
    'http': HTTPConnectionPool,
    'https': None, # TODO: https
}

class PoolManager(_PoolManager):

    def _new_pool(self, scheme, host, port):
        pool_cls = pool_classes_by_scheme[scheme]
        kwargs = self.connection_pool_kw
        if scheme == 'http':
            kwargs = self.connection_pool_kw.copy()
            for kw in SSL_KEYWORDS:
                kwargs.pop(kw, None)

        return pool_cls(host, port, **kwargs)

    @asyncio.coroutine
    def urlopen(self, method, url, redirect=True, **kw):
        u = parse_url(url)
        conn = self.connection_from_host(u.host, port=u.port, scheme=u.scheme)

        kw['assert_same_host'] = False
        kw['redirect'] = False
        if 'headers' not in kw:
            kw['headers'] = self.headers

        if self.proxy is not None and u.scheme == "http":
            response = yield from conn.urlopen(method, url, **kw)
        else:
            response = yield from conn.urlopen(method, u.request_uri, **kw)

        redirect_location = redirect and response.get_redirect_location()
        if not redirect_location:
            return response

        # Support relative URLs for redirecting.
        redirect_location = urljoin(url, redirect_location)

        # RFC 2616, Section 10.3.4
        if response.status == 303:
            method = 'GET'

        log.info("Redirecting %s -> %s" % (url, redirect_location))
        kw['retries'] = kw.get('retries', 3) - 1  # Persist retries countdown
        kw['redirect'] = redirect
        result = yield from self.urlopen(method, redirect_location, **kw)
        return result
