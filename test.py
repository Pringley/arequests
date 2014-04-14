import asyncio
import asyncreq

@asyncio.coroutine
def test():
    response = yield from asyncreq.request('GET', 'http://www.google.com')
    print(response.text)

loop = asyncio.get_event_loop()
loop.run_until_complete(test())
