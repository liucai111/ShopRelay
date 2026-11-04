"""Run the documented CI core in a fresh, network-none engineering container."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import os

os.environ['PYTHONPYCACHEPREFIX']='/tmp/m65-pycache'

ROOT=Path('/opt/shoprelay')
OUT=Path('/results')
steps=[('static',[sys.executable,'-m','compileall','-q','app','scripts/m65_local_delivery.py','scripts/m65_web_gateway.py']),
    ('unit',[sys.executable,'docs/probes/run_m62_r22_offline.py','tests/test_m6_4_policy_scope.py','-k','not through_actual_cli',
        '-p','pytest_workspace_tmp_path_r1','-p','no:cacheprovider','--junitxml=/results/unit.xml','-q','--tb=short']),
    ('api',[sys.executable,'docs/probes/run_m62_r22_offline.py','--allow-loopback','tests/test_m1_api.py',
        '-k','not single_chat_can_use_real_rrf_retrieval_service',
        '-p','pytest_workspace_tmp_path_r1','-p','no:cacheprovider','--junitxml=/results/api.xml','-q','--tb=short']),
    ('cli',[sys.executable,'docs/probes/run_m62_r22_offline.py','tests/test_m6_4_core.py','-k','complete_policy_answers_through_actual_cli',
        '-p','pytest_workspace_tmp_path_r1','-p','no:cacheprovider','--junitxml=/results/cli.xml','-q','--tb=short']),
    ('report',[sys.executable,'docs/probes/recompute_m64_r2.py'])]
results=[]
for name,command in steps:
    with (OUT/(name+'.log')).open('w') as log:
        result=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
    results.append({'step':name,'exit_code':result.returncode,'command':command})
    (OUT/'steps.json').write_text(json.dumps(results,indent=2))
    if result.returncode:
        raise SystemExit('offline reproduction failed: '+name+'; see /results/'+name+'.log')
print(json.dumps({'all_steps_passed':True,'steps':results,'Provider':0,'Judge':0,'Embedding':0,
    'environment':'new container layer; network none; no host project, secrets, DB or Gold mounts'}))
