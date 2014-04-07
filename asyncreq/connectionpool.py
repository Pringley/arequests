import asyncio

from queue import Empty
from socket import error as SocketError, timeout as SocketTimeout
from requests.packages.urllib3.connectionpool import HTTPConnectionPool as _HTTPConncectionPool, _Default
from requests.packages.urllib3.exceptions import (ReadTimeoutError, HostChangedError,
        EmptyPoolError, TimeoutError, MaxRetryError)
from requests.packages.urllib3.connection import HTTPException, BaseSSLError
from requests.packages.urllib3.packages.ssl_match_hostname import CertificateError
from requests.packages.urllib3.util import get_host
from .httpclient import HTTPConnection

class HTTPConnectionPool(_HTTPConncectionPool):
    ConnectionCls = HTTPConnection

    @asyncio.coroutine
    def _make_request(self, conn, method, url, timeout=_Default,
                      **httplib_request_kw):
        self.num_requests += 1

        timeout_obj = self._get_timeout(timeout)

        try:
            timeout_obj.start_connect()
            conn.timeout = timeout_obj.connect_timeout
            # conn.request() calls httplib.*.request, not the method in
            # urllib3.request. It also calls makefile (recv) on the socket.
            yield from conn.request(method, url, **httplib_request_kw)
        except SocketTimeout:
            raise ConnectTimeoutError(
                self, "Connection to %s timed out. (connect timeout=%s)" %
                (self.host, timeout_obj.connect_timeout))

        # Reset the timeout for the recv() on the socket
        read_timeout = timeout_obj.read_timeout

        # App Engine doesn't have a sock attr
        if hasattr(conn, 'sock'):
            # In Python 3 socket.py will catch EAGAIN and return None when you
            # try and read into the file pointer created by http.client, which
            # instead raises a BadStatusLine exception. Instead of catching
            # the exception and assuming all BadStatusLine exceptions are read
            # timeouts, check for a zero timeout before making the request.
            if read_timeout == 0:
                raise ReadTimeoutError(
                    self, url,
                    "Read timed out. (read timeout=%s)" % read_timeout)
            if read_timeout is Timeout.DEFAULT_TIMEOUT:
                conn.sock.settimeout(socket.getdefaulttimeout())
            else: # None or a value
                conn.sock.settimeout(read_timeout)

        # Receive the response from the server
        try:
            try: # Python 2.7+, use buffering of HTTP responses
                httplib_response = yield from conn.getresponse(buffering=True)
            except TypeError: # Python 2.6 and older
                httplib_response = yield from conn.getresponse()
        except SocketTimeout:
            raise ReadTimeoutError(
                self, url, "Read timed out. (read timeout=%s)" % read_timeout)

        except BaseSSLError as e:
            # Catch possible read timeouts thrown as SSL errors. If not the
            # case, rethrow the original. We need to do this because of:
            # http://bugs.python.org/issue10272
            if 'timed out' in str(e) or \
               'did not complete (read)' in str(e):  # Python 2.6
                raise ReadTimeoutError(self, url, "Read timed out.")

            raise

        except SocketError as e: # Platform-specific: Python 2
            # See the above comment about EAGAIN in Python 3. In Python 2 we
            # have to specifically catch it and throw the timeout error
            if e.errno in _blocking_errnos:
                raise ReadTimeoutError(
                    self, url,
                    "Read timed out. (read timeout=%s)" % read_timeout)

            raise

        # AppEngine doesn't have a version attr.
        http_version = getattr(conn, '_http_vsn_str', 'HTTP/?')
        log.debug("\"%s %s %s\" %s %s" % (method, url, http_version,
                                          httplib_response.status,
                                          httplib_response.length))
        return httplib_response

    @asyncio.coroutine
    def urlopen(self, method, url, body=None, headers=None, retries=3,
                redirect=True, assert_same_host=True, timeout=_Default,
                pool_timeout=None, release_conn=None, **response_kw):
        if headers is None:
            headers = self.headers

        if retries < 0 and retries is not False:
            raise MaxRetryError(self, url)

        if release_conn is None:
            release_conn = response_kw.get('preload_content', True)

        # Check host
        if assert_same_host and not self.is_same_host(url):
            raise HostChangedError(self, url, retries - 1)

        conn = None

        # Merge the proxy headers. Only do this in HTTP. We have to copy the
        # headers dict so we can safely change it without those changes being
        # reflected in anyone else's copy.
        if self.scheme == 'http':
            headers = headers.copy()
            headers.update(self.proxy_headers)

        # Must keep the exception bound to a separate variable or else Python 3
        # complains about UnboundLocalError.
        err = None

        try:
            # Request a connection from the queue
            conn = self._get_conn(timeout=pool_timeout)

            # Make the request on the httplib connection object
            httplib_response = yield from self._make_request(conn, method, url,
                                                  timeout=timeout,
                                                  body=body, headers=headers)

            # If we're going to release the connection in ``finally:``, then
            # the request doesn't need to know about the connection. Otherwise
            # it will also try to release it and we'll have a double-release
            # mess.
            response_conn = not release_conn and conn

            # Import httplib's response into our own wrapper object
            response = HTTPResponse.from_httplib(httplib_response,
                                                 pool=self,
                                                 connection=response_conn,
                                                 **response_kw)

            # else:
            #     The connection will be put back into the pool when
            #     ``response.release_conn()`` is called (implicitly by
            #     ``response.read()``)

        except Empty:
            # Timed out by queue.
            raise EmptyPoolError(self, "No pool connections are available.")

        except (BaseSSLError, CertificateError) as e:
            # Release connection unconditionally because there is no way to
            # close it externally in case of exception.
            release_conn = True
            raise SSLError(e)

        except (TimeoutError, HTTPException, SocketError) as e:
            if conn:
                # Discard the connection for these exceptions. It will be
                # be replaced during the next _get_conn() call.
                conn.close()
                conn = None

            if not retries:
                if isinstance(e, TimeoutError):
                    # TimeoutError is exempt from MaxRetryError-wrapping.
                    # FIXME: ... Not sure why. Add a reason here.
                    raise

                # Wrap unexpected exceptions with the most appropriate
                # module-level exception and re-raise.
                if isinstance(e, SocketError) and self.proxy:
                    raise ProxyError('Cannot connect to proxy.', e)

                if retries is False:
                    raise ConnectionError('Connection failed.', e)

                raise MaxRetryError(self, url, e)

            # Keep track of the error for the retry warning.
            err = e

        finally:
            if release_conn:
                # Put the connection back to be reused. If the connection is
                # expired then it will be None, which will get replaced with a
                # fresh connection during _get_conn.
                self._put_conn(conn)

        if not conn:
            # Try again
            log.warning("Retrying (%d attempts remain) after connection "
                        "broken by '%r': %s" % (retries, err, url))
            result = yield from self.urlopen(method, url, body, headers, retries - 1,
                                redirect, assert_same_host,
                                timeout=timeout, pool_timeout=pool_timeout,
                                release_conn=release_conn, **response_kw)
            return result

        # Handle redirect?
        redirect_location = redirect and response.get_redirect_location()
        if redirect_location and retries is not False:
            if response.status == 303:
                method = 'GET'
            log.info("Redirecting %s -> %s" % (url, redirect_location))
            result = yield from self.urlopen(method, redirect_location, body, headers,
                                retries - 1, redirect, assert_same_host,
                                timeout=timeout, pool_timeout=pool_timeout,
                                release_conn=release_conn, **response_kw)
            return result

        return response

def connection_from_url(url, **kw):
    scheme, host, port = get_host(url)
    if scheme == 'https':
        # return HTTPSConnectionPool(host, port=port, **kw)
        raise NotImplementedError("https not yet supported")
    else:
        return HTTPConnectionPool(host, port=port, **kw)
