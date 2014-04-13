import greenio
import requests

from .greenadapter import AIOHTTPAdapter

@greenio.task
def request(*args, **kwargs):
    session = requests.Session()
    session.mount('https://', AIOHTTPAdapter())
    session.mount('http://', AIOHTTPAdapter())
    return session.request(*args, **kwargs)
