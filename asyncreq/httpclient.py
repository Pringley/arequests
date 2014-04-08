import asyncio
import collections
import email.parser

from urllib3.connection import HTTPConnection as _HTTPConnection
from http.client import (HTTPResponse as _HTTPResponse,
        _MAXHEADERS, _MAXLINE, LineTooLong, BadStatusLine, HTTPMessage,
        HTTPException, IncompleteRead, MAXAMOUNT, _CS_IDLE, _CS_REQ_SENT,
        _UNKNOWN, ResponseNotReady, CONTINUE, NO_CONTENT, NOT_MODIFIED,
        NotConnected)

class MockSock:

    def makefile(*args, **kwargs):
        pass

@asyncio.coroutine
def parse_headers(async_reader, _class=HTTPMessage):
    headers = []
    while True:
        line = yield from async_reader.readline()
        if len(line) > _MAXLINE:
            raise LineTooLong("header line")
        headers.append(line)
        if len(headers) > _MAXHEADERS:
            raise HTTPException("got more than %d headers" % _MAXHEADERS)
        if line in (b'\r\n', b'\n', b''):
            break
    hstring = b''.join(headers).decode('iso-8859-1')
    return email.parser.Parser(_class=_class).parsestr(hstring)

class HTTPResponse(_HTTPResponse):

    def __init__(self, async_reader, *args, **kwargs):
        self.async_reader = async_reader
        super().__init__(MockSock(), *args, **kwargs)

    @asyncio.coroutine
    def _read_status(self):
        bline = yield from self.async_reader.readline()
        line = str(bline, "iso-8859-1")
        if len(line) > _MAXLINE:
            raise LineTooLong("status line")
        if self.debuglevel > 0:
            print("reply:", repr(line))
        if not line:
            # Presumably, the server closed the connection before
            # sending a valid response.
            raise BadStatusLine(line)
        try:
            version, status, reason = line.split(None, 2)
        except ValueError:
            try:
                version, status = line.split(None, 1)
                reason = ""
            except ValueError:
                # empty version will cause next test to fail.
                version = ""
        if not version.startswith("HTTP/"):
            self._close_conn()
            raise BadStatusLine(line)

        # The status code is a three-digit number
        try:
            status = int(status)
            if status < 100 or status > 999:
                raise BadStatusLine(line)
        except ValueError:
            raise BadStatusLine(line)
        return version, status, reason

    @asyncio.coroutine
    def begin(self):
        if self.headers is not None:
            # we've already started reading the response
            return

        # read until we get a non-100 response
        while True:
            version, status, reason = yield from self._read_status()
            if status != CONTINUE:
                break
            # skip the header from the 100 response
            while True:
                skip = yield from self.async_reader.readline()
                if len(skip) > _MAXLINE:
                    raise LineTooLong("header line")
                skip = skip.strip()
                if not skip:
                    break
                if self.debuglevel > 0:
                    print("header:", skip)

        self.code = self.status = status
        self.reason = reason.strip()
        if version in ("HTTP/1.0", "HTTP/0.9"):
            # Some servers might still return "0.9", treat it as 1.0 anyway
            self.version = 10
        elif version.startswith("HTTP/1."):
            self.version = 11   # use HTTP/1.1 code for HTTP/1.x where x>=1
        else:
            raise UnknownProtocol(version)

        self.headers = self.msg = yield from parse_headers(self.async_reader)

        if self.debuglevel > 0:
            for hdr in self.headers:
                print("header:", hdr, end=" ")

        # are we using the chunked-style of transfer encoding?
        tr_enc = self.headers.get("transfer-encoding")
        if tr_enc and tr_enc.lower() == "chunked":
            self.chunked = True
            self.chunk_left = None
        else:
            self.chunked = False

        # will the connection close at the end of the response?
        self.will_close = self._check_close()

        # do we have a Content-Length?
        # NOTE: RFC 2616, S4.4, #3 says we ignore this if tr_enc is "chunked"
        self.length = None
        length = self.headers.get("content-length")

         # are we using the chunked-style of transfer encoding?
        tr_enc = self.headers.get("transfer-encoding")
        if length and not self.chunked:
            try:
                self.length = int(length)
            except ValueError:
                self.length = None
            else:
                if self.length < 0:  # ignore nonsensical negative lengths
                    self.length = None
        else:
            self.length = None

        # does the body have a fixed length? (of zero)
        if (status == NO_CONTENT or status == NOT_MODIFIED or
            100 <= status < 200 or      # 1xx codes
            self._method == "HEAD"):
            self.length = 0

        # if the connection remains open, and we aren't using chunked, and
        # a content-length was not provided, then assume that the connection
        # WILL close.
        if (not self.will_close and
            not self.chunked and
            self.length is None):
            self.will_close = True

    def _close_conn(self):
        # async_reader has no close method
        self.async_reader = None

    def close(self):
        super(_HTTPResponse, self).close()

    def flush(self):
        super(_HTTPResponse, self).flush()

    def isclosed(self):
        return self.async_reader is None

    @asyncio.coroutine
    def read(self, amt=None):
        if self.async_reader is None:
            return b""

        if self._method == "HEAD":
            self._close_conn()
            return b""

        if amt is not None:
            result = yield from self.async_reader.read(amt)
            return result
        else:
            # Amount is not given (unbounded read) so we must check self.length
            # and self.chunked

            if self.chunked:
                result = yield from self._readall_chunked()
                return result

            if self.length is None:
                s = self.fp.read()
                s = yield from self.async_reader.read()
            else:
                try:
                    s = yield from self._safe_read(self.length)
                except IncompleteRead:
                    self._close_conn()
                    raise
                self.length = 0
            self._close_conn()        # we read everything
            return s

    @asyncio.coroutine
    def readinto(self, b):
        if self.async_reader is None:
            return 0

        if self._method == "HEAD":
            self._close_conn()
            return 0

        if self.chunked:
            result = yield from self._readinto_chunked(b)
            return result

        if self.length is not None:
            if len(b) > self.length:
                # clip the read to the "end of response"
                b = memoryview(b)[0:self.length]

        # we do not use _safe_read() here because this may be a .will_close
        # connection, and the user is reading more bytes than will be provided
        # (for example, reading in 1k chunks)

        # crappy workaround: read the data then copy it into buffer
        result = yield from self.async_reader.read(len(b))
        n = len(result)
        minlen = min(n, len(b))
        b[0:minlen] = result[0:minlen]

        if not n and b:
            # Ideally, we would raise IncompleteRead if the content-length
            # wasn't satisfied, but it might break compatibility.
            self._close_conn()
        elif self.length is not None:
            self.length -= n
            if not self.length:
                self._close_conn()
        return n

    @asyncio.coroutine
    def _read_next_chunk_size(self):
        # Read the next chunk size from the file
        line = yield from self.async_reader.readline()
        if len(line) > _MAXLINE:
            raise LineTooLong("chunk size")
        i = line.find(b";")
        if i >= 0:
            line = line[:i] # strip chunk-extensions
        try:
            return int(line, 16)
        except ValueError:
            # close the connection as protocol synchronisation is
            # probably lost
            self._close_conn()
            raise

    @asyncio.coroutine
    def _read_and_discard_trailer(self):
        # read and discard trailer up to the CRLF terminator
        ### note: we shouldn't have any trailers!
        while True:
            line = yield from self.async_reader.readline()
            if len(line) > _MAXLINE:
                raise LineTooLong("trailer line")
            if not line:
                # a vanishingly small number of sites EOF without
                # sending the trailer
                break
            if line in (b'\r\n', b'\n', b''):
                break

    @asyncio.coroutine
    def _readall_chunked(self):
        assert self.chunked != _UNKNOWN
        chunk_left = self.chunk_left
        value = []
        while True:
            if chunk_left is None:
                try:
                    chunk_left = yield from self._read_next_chunk_size()
                    if chunk_left == 0:
                        break
                except ValueError:
                    raise IncompleteRead(b''.join(value))
            result = yield from self._safe_read(chunk_left)
            value.append(result)

            # we read the whole chunk, get another
            yield from self._safe_read(2)      # toss the CRLF at the end of the chunk
            chunk_left = None

        yield from self._read_and_discard_trailer()

        # we read everything; close the "file"
        self._close_conn()

        return b''.join(value)

    @asyncio.coroutine
    def _readinto_chunked(self, b):
        assert self.chunked != _UNKNOWN
        chunk_left = self.chunk_left

        total_bytes = 0
        mvb = memoryview(b)
        while True:
            if chunk_left is None:
                try:
                    chunk_left = yield from self._read_next_chunk_size()
                    if chunk_left == 0:
                        break
                except ValueError:
                    raise IncompleteRead(bytes(b[0:total_bytes]))

            if len(mvb) < chunk_left:
                n = yield from self._safe_readinto(mvb)
                self.chunk_left = chunk_left - n
                return total_bytes + n
            elif len(mvb) == chunk_left:
                n = yield from self._safe_readinto(mvb)
                yield from self._safe_read(2)  # toss the CRLF at the end of the chunk
                self.chunk_left = None
                return total_bytes + n
            else:
                temp_mvb = mvb[0:chunk_left]
                n = yield from self._safe_readinto(temp_mvb)
                mvb = mvb[n:]
                total_bytes += n

            # we read the whole chunk, get another
            yield from self._safe_read(2)      # toss the CRLF at the end of the chunk
            chunk_left = None

        yield from self._read_and_discard_trailer()

        # we read everything; close the "file"
        yield from self._close_conn()

        return total_bytes

    @asyncio.coroutine
    def _safe_read(self, amt):
        """Read the number of bytes requested, compensating for partial reads.

        Normally, we have a blocking socket, but a read() can be interrupted
        by a signal (resulting in a partial read).

        Note that we cannot distinguish between EOF and an interrupt when zero
        bytes have been read. IncompleteRead() will be raised in this
        situation.

        This function should be used when <amt> bytes "should" be present for
        reading. If the bytes are truly not available (due to EOF), then the
        IncompleteRead exception can be used to detect the problem.
        """
        s = []
        while amt > 0:
            chunk = yield from self.async_reader.read(min(amt, MAXAMOUNT))
            if not chunk:
                raise IncompleteRead(b''.join(s), amt)
            s.append(chunk)
            amt -= len(chunk)
        return b"".join(s)

    @asyncio.coroutine
    def _safe_readinto(self, b):
        """Same as _safe_read, but for reading into a buffer."""
        total_bytes = 0
        mvb = memoryview(b)
        while total_bytes < len(b):
            if MAXAMOUNT < len(mvb):
                temp_mvb = mvb[0:MAXAMOUNT]

                # crappy workaround: read the data then copy it into buffer
                result = yield from self.async_reader.read(len(temp_mvb))
                n = len(result)
                minlen = min(n, len(temp_mvb))
                temp_mvb[0:minlen] = result[0:minlen]

            else:

                # crappy workaround: read the data then copy it into buffer
                result = yield from self.async_reader.read(len(mvb))
                n = len(result)
                minlen = min(n, len(mvb))
                mvb[0:minlen] = result[0:minlen]

            if not n:
                raise IncompleteRead(bytes(mvb[0:total_bytes]), len(b))
            mvb = mvb[n:]
            total_bytes += n
        return total_bytes

class HTTPConnection(_HTTPConnection):

    response_class = HTTPResponse

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.async_reader = self.async_writer = None

    @asyncio.coroutine
    def connect(self):
        conn = self._new_conn()
        rw = asyncio.open_connection(sock=conn)
        self.async_reader, self.async_writer = yield from rw

        if self._tunnel_host:
            yield from self._tunnel()

    @asyncio.coroutine
    def _tunnel(self):
        self._set_hostport(self._tunnel_host, self._tunnel_port)
        connect_str = "CONNECT %s:%d HTTP/1.0\r\n" % (self.host, self.port)
        connect_bytes = connect_str.encode("ascii")
        self.send(connect_bytes)
        for header, value in self._tunnel_headers.items():
            header_str = "%s: %s\r\n" % (header, value)
            header_bytes = header_str.encode("latin-1")
            self.send(header_bytes)
        self.send(b'\r\n')

        response = self.response_class(self.async_reader, method=self._method)
        (version, code, message) = yield from response._read_status()

        if code != 200:
            self.close()
            raise OSError("Tunnel connection failed: %d %s" % (code,
                                                               message.strip()))
        while True:
            line = yield from response.async_reader.readline()
            if len(line) > _MAXLINE:
                raise LineTooLong("header line")
            if not line:
                # for sites which EOF without sending a trailer
                break
            if line in (b'\r\n', b'\n', b''):
                break

    def close(self):
        if self.async_writer:
            self.async_writer.close()
            self.async_writer = None
        if self.__response:
            self.__response.close()
            self.__response = None
        self.__state = _CS_IDLE

    @asyncio.coroutine
    def request(self, *args, **kwargs):

        if self.async_writer is None:
            yield from self.connect()

        super().request(*args, **kwargs)

    def send(self, data):

        if self.async_writer is None:
            raise NotConnected()

        if self.debuglevel > 0:
            print("send:", repr(data))
        blocksize = 8192
        if hasattr(data, "read") :
            if self.debuglevel > 0:
                print("sendIng a read()able")
            encode = False
            try:
                mode = data.mode
            except AttributeError:
                # io.BytesIO and other file-like objects don't have a `mode`
                # attribute.
                pass
            else:
                if "b" not in mode:
                    encode = True
                    if self.debuglevel > 0:
                        print("encoding file using iso-8859-1")
            while 1:
                datablock = data.read(blocksize)
                if not datablock:
                    break
                if encode:
                    datablock = datablock.encode("iso-8859-1")
                self.async_writer.write(datablock)
            return
        try:
            self.async_writer.write(data)
        except TypeError:
            if isinstance(data, collections.Iterable):
                for d in data:
                    self.async_writer.write(d)
            else:
                raise TypeError("data should be a bytes-like object "
                                "or an iterable, got %r" % type(data))

    @asyncio.coroutine
    def getresponse(self):

        # if a prior response has been completed, then forget about it.
        if self.__response and self.__response.isclosed():
            self.__response = None

        # if a prior response exists, then it must be completed (otherwise, we
        # cannot read this response's header to determine the connection-close
        # behavior)
        #
        # note: if a prior response existed, but was connection-close, then the
        # socket and response were made independent of this HTTPConnection
        # object since a new request requires that we open a whole new
        # connection
        #
        # this means the prior response had one of two states:
        #   1) will_close: this connection was reset and the prior socket and
        #                  response operate independently
        #   2) persistent: the response was retained and we await its
        #                  isclosed() status to become true.
        #
        if self.__state != _CS_REQ_SENT or self.__response:
            raise ResponseNotReady(self.__state)

        if self.debuglevel > 0:
            response = self.response_class(self.async_reader, self.debuglevel,
                                           method=self._method)
        else:
            response = self.response_class(self.async_reader, method=self._method)

        yield from response.begin()
        assert response.will_close != _UNKNOWN
        self.__state = _CS_IDLE

        if response.will_close:
            # this effectively passes the connection to the response
            self.close()
        else:
            # remember this, so we can tell when it is complete
            self.__response = response

        return response

@asyncio.coroutine
def test():
    conn = HTTPConnection(host='www.google.com')
    yield from conn.connect()
    print('connected')

    conn.request('GET', '/')
    response = yield from conn.getresponse()
    print(response.status)

    body = yield from response.read()
    print(body)

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(test())
