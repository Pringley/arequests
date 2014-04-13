Use asyncio and requests together... somehow!

```python
import asyncio
import greenio
import asyncreq

@asyncio.coroutine
def test():
    response = yield from asyncreq.request('GET', 'http://www.google.com')
    print(response.text)

asyncio.set_event_loop_policy(greenio.GreenEventLoopPolicy())
loop = asyncio.get_event_loop()
loop.run_until_complete(test())
```
