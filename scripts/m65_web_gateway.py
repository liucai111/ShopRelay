"""Loopback-published HTTP gateway; application remains on internal networks.

Fixed upstream only, no CONNECT, no dynamic proxy destination, no DB/secret mount.
"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Gateway(BaseHTTPRequestHandler):
    def forward(self):
        if not self.path.startswith('/'):
            self.send_error(400)
            return
        upstream=http.client.HTTPConnection('delivery',8080,timeout=60)
        excluded={'host','connection','transfer-encoding','proxy-connection','content-length'}
        headers={k:v for k,v in self.headers.items() if k.casefold() not in excluded}
        size=int(self.headers.get('content-length','0'))
        if size>131072:
            self.send_error(413)
            return
        body=self.rfile.read(size) if size else None
        try:
            upstream.request(self.command,self.path,body=body,headers=headers)
            response=upstream.getresponse()
            self.send_response(response.status)
            for key,value in response.getheaders():
                if key.casefold() not in {'connection','transfer-encoding'}:
                    self.send_header(key,value)
            self.send_header('Connection','close')
            self.end_headers()
            while data:=response.read1(65536):
                self.wfile.write(data)
                self.wfile.flush()
        finally:
            upstream.close()
        self.close_connection=True

    do_GET=forward
    do_POST=forward
    do_DELETE=forward

    def log_message(self,*args):
        pass  # No credentials, request payloads or query strings in gateway logs.


if __name__=='__main__':
    ThreadingHTTPServer(('0.0.0.0',8080),Gateway).serve_forever()
