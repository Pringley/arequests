import requests
import corolet

from .greenadapter import AIOHTTPAdapter

@corolet.corolet
def request(*args, **kwargs):
    session = requests.Session()
    session.mount('https://', AIOHTTPAdapter())
    session.mount('http://', AIOHTTPAdapter())
    return session.request(*args, **kwargs)
