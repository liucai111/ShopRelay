"""Provision only the interactive demo's secret/ledger volumes. Never prints secrets.

Image/build/Compose remain with the existing deployment entry points. No model
request is made by this command and no existing session or database row is changed.
"""
import json
from pathlib import Path
import subprocess
from app.m0.config import load_settings

IMAGE='shoprelay-m62-eval-app:customer-experience-r1'
SECRET='shoprelay-m62-eval_delivery-provider-secret'
LEDGER='shoprelay-m62-eval_delivery-ledger'

def main():
    settings=load_settings(require_api_key=True)
    from urllib.parse import urlparse
    if settings.model!='deepseek-flash' or urlparse(settings.base_url).hostname!='api.deepseek.com':
        raise RuntimeError('existing provider binding differs from approved destination')
    for volume in (SECRET,LEDGER):
        subprocess.run(['docker','volume','create',volume],check=True,capture_output=True)
    code="""
import os,sys
from pathlib import Path
from datetime import datetime,timezone
secret=Path('/secret/api-key')
if not secret.exists():
    secret.write_bytes(sys.stdin.buffer.read());secret.chmod(0o400);os.chown(secret,10002,10002)
ledger=Path('/ledger');os.chown(ledger,10002,10002)
for name,value in [('scope','AUTOMATED_SYNTHETIC'),('enabled-at',datetime.now(timezone.utc).isoformat())]:
    path=ledger/name
    if not path.exists():path.write_text(value);os.chown(path,10002,10002);path.chmod(0o600)
print('Dedicated secret and persistent ledger prepared; no model request')
"""
    result=subprocess.run(['docker','run','--rm','-i','--network','none','--user','0',
        '--mount','type=volume,src='+SECRET+',dst=/secret',
        '--mount','type=volume,src='+LEDGER+',dst=/ledger',
        '--entrypoint','/opt/evalvenv/bin/python',IMAGE,'-c',code],
        input=settings.api_key.encode(),capture_output=True,check=True)
    print(result.stdout.decode().strip())
    print(json.dumps({'provider':'DeepSeek','destination':'api.deepseek.com','model':settings.model,
        'key_in_arguments_or_image':False,'provider_requests':0}))

if __name__=='__main__':main()
