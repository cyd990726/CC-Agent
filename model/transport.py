"""urllib transport with a socket shutdown hook for cancelled requests."""

import http.client
import socket
import urllib.request
from contextlib import ExitStack, contextmanager


@contextmanager
def cancellable_response(request, timeout, token):
    with ExitStack() as stack:
        def connection_factory(base):
            class Connection(base):
                def connect(self):
                    token.check()
                    super().connect()
                    sock = self.sock

                    def abort():
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

                    stack.enter_context(token.on_cancel(abort))
                    token.check()

            return Connection

        class HTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(connection_factory(http.client.HTTPConnection), req)

        class HTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(
                    connection_factory(http.client.HTTPSConnection), req,
                    context=self._context,
                )

        opener = urllib.request.build_opener(HTTPHandler(), HTTPSHandler())
        with opener.open(request, timeout=timeout) as response:
            token.check()
            yield response
