import asyncio

import aiohttp
import corolet
import requests
import requests.adapters
import requests.structures

class AIOHTTPAdapter(requests.adapters.BaseAdapter):

    def send(self, request, stream=False, timeout=None, **kwargs):

        aiohttp_request = asyncio.Task(aiohttp.request(
            request.method,
            request.url,
            headers=request.headers.items(),
            data=request.body,
            allow_redirects=False,
            chunked=False,
            timeout=timeout,
            conn_timeout=timeout))

        aiohttp_response = corolet.yield_from(aiohttp_request)

        response = requests.models.Response()
        response.status_code = getattr(aiohttp_response, 'status', None)

        headers = getattr(aiohttp_response, 'headers', {})
        response.headers = requests.structures.CaseInsensitiveDict(headers)

        response.encoding = requests.utils.get_encoding_from_headers(response.headers)
        response.reason = aiohttp_response.reason
        response.raw = aiohttp_response

        if isinstance(request.url, bytes):
            response.url = request.url.decode('utf-8')
        else:
            response.url = request.url

        response.request = request
        response.connection = self

        response.history = []

        if not stream:
            read_task = asyncio.Task(aiohttp_response.read_and_close())
            response._content = bytes(corolet.yield_from(read_task))
            response._content_consumed = True

        return response

    def close(self):
        pass
