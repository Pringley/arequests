import asyncio

from requests.models import Response as _Response
from requests.utils import iter_slices

class Response(_Response):

    def __iter__(self):
        raise NotImplementedError("cannot iterate over a coroutine")

    def iter_content(self, chunk_size=1, decode_unicode=False):
        raise NotImplementedError("cannot iterate over a coroutine")

    def iter_lines(self, chunk_size=ITER_CHUNK_SIZE, decode_unicode=None):
        raise NotImplementedError("cannot iterate over a coroutine")

    @property
    def content(self):
        """Content of the response, in bytes."""

        if self._content is False:
            raise RuntimeError("need to load content with fetch_content before reading it")

        return self._content

    @asyncio.coroutine
    def fetch_content(self):

        if self._content is False:

            try:
                if self._content_consumed:
                    raise RuntimeError(
                        'The content for this response was already consumed')
                if self.status_code == 0:
                    self._content = None
                else:
                    # TODO: chunked read
                    self._content = yield from self.raw.read(decode_content=True)
            except AttributeError:
                self._content = None

        self._content_consumed = True
        return self._content
