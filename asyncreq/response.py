import asyncio
import zlib

from requests.packages.urllib3.response import HTTPResponse as _HTTPResponse

class HTTPResponse(_HTTPResponse):
    
    def __init__(self, *args, **kwargs):
        if kwargs['preload_content']:
            raise NotImplementedError("cannot asynchronously preload in constructor")

        super().__init__(*args, **kwargs)
        del self._fp
        body = args[0] if args else kwargs['body']
        if hasattr(body, 'async_reader'):
            self._async_reader = body.async_reader

    @property
    @asyncio.coroutine
    def data(self):
        # For backwords-compat with earlier urllib3 0.4 and earlier.
        if self._body:
            return self._body

        if self._async_reader:
            result = yield from self.read(cache_content=True)
            return result

    @asyncio.coroutine
    def read(self, amt=None, decode_content=None, cache_content=False):
        # Note: content-encoding value should be case-insensitive, per RFC 2616
        # Section 3.5
        content_encoding = self.headers.get('content-encoding', '').lower()
        if self._decoder is None:
            if content_encoding in self.CONTENT_DECODERS:
                self._decoder = _get_decoder(content_encoding)
        if decode_content is None:
            decode_content = self.decode_content

        if self._async_reader is None:
            return

        flush_decoder = False

        try:
            if amt is None:
                # cStringIO doesn't like amt=None
                data = yield from self._async_reader.read()
                flush_decoder = True
            else:
                cache_content = False
                data = yield from self._async_reader.read(amt)
                if amt != 0 and not data:  # Platform-specific: Buggy versions of Python.
                    # Close the connection when no data is returned
                    #
                    # This is redundant to what httplib/http.client _should_
                    # already do.  However, versions of python released before
                    # December 15, 2012 (http://bugs.python.org/issue16298) do not
                    # properly close the connection in all cases. There is no harm
                    # in redundantly calling close.
                    flush_decoder = True

            self._fp_bytes_read += len(data)

            try:
                if decode_content and self._decoder:
                    data = self._decoder.decompress(data)
            except (IOError, zlib.error) as e:
                raise DecodeError(
                    "Received response with content-encoding: %s, but "
                    "failed to decode it." % content_encoding,
                    e)

            if flush_decoder and decode_content and self._decoder:
                buf = self._decoder.decompress(binary_type())
                data += buf + self._decoder.flush()

            if cache_content:
                self._body = data

            return data

        finally:
            if self._original_response and self._original_response.isclosed():
                self.release_conn()

    def stream(self, amt=2**16, decode_content=None):
        raise NotImplementedError("can't implement generator with coroutine")

    def close(self):
        # async_reader has no close method
        pass

    @property
    def closed(self):
        if self._async_reader is None:
            return True
        elif self._async_reader.at_eof():
            return True
        return False

    def fileno(self):
        raise IOError("HTTPResponse has no file to get a fileno from")

    def flush(self):
        # async_reader has no close method
        pass
