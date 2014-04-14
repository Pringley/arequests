import asyncio
import arequests

@asyncio.coroutine
def test():
    print('[test] reading from internet')
    response = yield from arequests.get('http://www.google.com')
    print('[test] finished reading', len(response.text), 'bytes, status', response.status_code)

@asyncio.coroutine
def loopy():
    for _ in range(25):
        print('[loopy] ping')
        yield from asyncio.sleep(.02)

tasks = map(asyncio.Task, [loopy()] + [test() for _ in range(10)])
metatask = asyncio.wait(tasks)

loop = asyncio.get_event_loop()
loop.run_until_complete(metatask)
