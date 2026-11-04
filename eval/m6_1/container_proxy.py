"""TCP relay to the user-approved host loopback proxy, no direct upstream path."""
import asyncio,json,os
from pathlib import Path
async def connection(reader,writer):
    remote=None
    try:
        headers=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),5)
        if len(headers)>16384:return
        first=headers.split(b'\r\n',1)[0].decode('ascii').split()
        if len(first)!=3:return
        if first[0]=='GET' and first[1] in {'/__r26_proxy_health','http://proxy:7892/__r26_proxy_health'}:
            _,remote=await asyncio.wait_for(asyncio.open_connection('host.docker.internal',7892),5)
            payload=b'{"host_proxy_tcp_connected":true,"provider_requests":0}'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(payload)).encode()+b'\r\n\r\n'+payload)
            await writer.drain();return
        mode=json.loads(Path(os.environ.get('SHOPRELAY_PROXY_MODE_FILE',
            '/opt/shoprelay/data/eval/formal-trust/mode.json')).read_text())
        if mode['engineering_only'] or not mode['real_provider_allowed'] or first[:2]!=['CONNECT','api.deepseek.com:443']:
            writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n');await writer.drain();return
        upstream,remote=await asyncio.open_connection('host.docker.internal',7892)
        remote.write(headers);await remote.drain()
        async def copy(source,destination):
            while data:=await source.read(65536):
                destination.write(data);await destination.drain()
        await asyncio.gather(copy(reader,remote),copy(upstream,writer))
    finally:
        writer.close()
        if remote:remote.close()
async def main():
    server=await asyncio.start_server(connection,'0.0.0.0',7892)
    async with server:await server.serve_forever()
if __name__=='__main__':asyncio.run(main())
