import asyncio
import arequests

@asyncio.coroutine
def test():
    response = yield from arequests.get('http://www.google.com')
    print(response.text)

loop = asyncio.get_event_loop()
loop.run_until_complete(test())
