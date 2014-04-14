import requests
import corolet

from .adapter import AIOHTTPAdapter

def _make_clet(method_name):

    @corolet.corolet
    def clet(*args, **kwargs):
        session = requests.Session()
        session.mount('https://', AIOHTTPAdapter())
        session.mount('http://', AIOHTTPAdapter())

        method = getattr(session, method_name)
        return method(*args, **kwargs)

    clet.__name__ = method_name
    clet.__qualname__ = method_name

    return clet

request = _make_clet('request')
get = _make_clet('get')
options = _make_clet('options')
head = _make_clet('head')
post = _make_clet('post')
put = _make_clet('put')
patch = _make_clet('patch')
delete = _make_clet('delete')

del _make_clet
