"""Trusted local Docker operations;never a second evaluation Runner.

Stages explicit source files,excluding env files,datasets and candidate builders
from the app image. Uses only the independent shoprelay-m62-eval Compose project.
"""
from pathlib import Path
import ast,json,shutil,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
CONTEXT=ROOT/'data/eval/container-build/r26'
COMPOSE=ROOT/'deploy/m62-eval/compose.yaml'

def docker(*args):
    result=subprocess.run(['docker',*args],cwd=ROOT,check=True,capture_output=True,text=True)
    return result.stdout

def compose(*args):return docker('compose','-f',str(COMPOSE),*args)

def stage_m64_tests():
    """Minimal cached-image overlay; no assets, live secrets or trust mounted."""
    context=ROOT/'data/eval/container-build/m64'
    paths={'app/m1/chat.py','app/m2/self_rag.py','app/m2/policy_execution.py','app/orchestration/persistence.py',
        'docs/probes/run_m64_container_tests.py','tests/test_m6_4_core.py','tests/test_m6_4_postgres.py',
        'tests/test_m6_4_policy_scope.py','tests/test_m2_self_rag.py','tests/test_m2_policy_execution.py',
        'tests/pytest_workspace_tmp_path_r1.py',
        'tests/test_m6_2_r25_persistent_actions.py','tests/test_m1_api.py','data/seed.json',
        'docs/eval/m6.3-personal-execution-20261101/final-stage-closeout-r1.json',
        'docs/eval/m6.3-personal-execution-20261101/candidate-ablation-observations-final.json'}
    # Follow explicit Python test-support imports, never enumerate asset trees.
    pending=[p for p in paths if p.startswith('tests/')]
    paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT/'migrations').rglob('*') if p.is_file() and p.suffix in {'.py','.sql','.mako'})
    paths.update({'alembic.ini','data/index.json'})
    while pending:
        name=pending.pop()
        tree=ast.parse((ROOT/name).read_text(encoding='utf8'))
        for node in ast.walk(tree):
            modules=[node.module] if isinstance(node,ast.ImportFrom) else [a.name for a in node.names] if isinstance(node,ast.Import) else []
            for module in modules:
                if not module or not module.startswith('test_'):continue
                candidate='tests/'+module+'.py'
                if (ROOT/candidate).is_file() and candidate not in paths:
                    paths.add(candidate);pending.append(candidate)
    for path in sorted(paths):
        dst=context/'files'/path;dst.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/path,dst)
    marker=context/'files/data/eval/formal-targets/container.json'
    marker.parent.mkdir(parents=True,exist_ok=True)
    marker.write_text(json.dumps({'profile':'M6.2-FORMAL-POSTGRES','identity':'ca3b0b3d027448a09cc58caa49139862',
        'host':'m62-eval-postgres','port':5432,'database':'shoprelay_m11_test'}),encoding='utf8')
    (context/'Dockerfile').write_text('FROM shoprelay-m62-eval-app:m63-s2\nUSER 0\nCOPY files/ /opt/shoprelay/\n'
        'ENV PYTHONPATH=/opt/shoprelay:/opt/shoprelay/tests\nUSER 10002:10002\n',encoding='utf8')
    print(json.dumps({'build_context':str(context.relative_to(ROOT)),'files':len(paths),
        'base':'shoprelay-m62-eval-app:m63-s2','Provider_secrets':False,'protected_assets':False}))


def stage_m65_delivery(*, interactive=False):
    """Production-source overlay plus a disclosed local demo, never Gold/tests."""
    import hashlib
    context=ROOT/'data/eval/container-build'/('customer-experience-r1' if interactive else 'm65')
    if interactive:
        # Only generated frontend staging bytes are replaced. Prior images and
        # source manifests remain available; never accumulate obsolete bundles.
        staged_dist=(context/'files/frontend/dist').resolve()
        if not staged_dist.is_relative_to((ROOT/'data/eval/container-build').resolve()):
            raise ValueError('frontend staging path escaped its build directory')
        if staged_dist.exists():shutil.rmtree(staged_dist)
    paths={'app/m1/chat.py','app/m2/self_rag.py','app/m2/policy_execution.py','app/orchestration/persistence.py',
        'scripts/m65_local_delivery.py','scripts/m65_web_gateway.py','scripts/probe_m5_4_a_browser_server.py','scripts/g2_test_database.py',
        'alembic.ini','data/index.json'}
    if interactive:
        from app.m0.data import ingest
        ingest(ROOT/'data/delivery-index.json', ROOT/'knowledge/policies.json')
        paths.update({'data/delivery-index.json','knowledge/policies.json','app/m1/routing.py',
            'app/m1/api.py','app/m3/order_tools.py','app/m3/agents.py','app/m3/contracts.py','app/m3/composition.py',
              'app/orchestration/runtime.py','scripts/cleanup_m65_delivery.py','scripts/verify_customer_orders.py',
            'app/orchestration/graph.py','app/orchestration/adapters.py','app/m1/delivery_provider.py','eval/m6_1/container_proxy.py',
            'eval/m6_1/budget.py','eval/m6_1/store.py','eval/m6_1/contracts.py',
            'scripts/verify_customer_experience.py'})
    paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT/'migrations').rglob('*')
        if p.is_file() and p.suffix in {'.py','.sql','.mako'})
    paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT/'frontend/dist').rglob('*') if p.is_file())
    for name in sorted(paths):
        destination=context/'files'/name;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/name,destination)
    manifest={'base':'shoprelay-m62-eval-app:m63-s2','files':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sorted(paths)},
        'secrets_or_gold_or_whole_repository':False,'model_mode':'REAL_MODEL' if interactive else 'DETERMINISTIC_EVIDENCE_STUB'}
    (context/'source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf8')
    (context/'Dockerfile').write_text('FROM shoprelay-m62-eval-app:m63-s2\nUSER 0\nCOPY files/ /opt/shoprelay/\n'
        'RUN mkdir -p /delivery-state && chown 10002:10002 /delivery-state\nUSER 10002:10002\n',encoding='utf8')
    print(json.dumps({'context':str(context.relative_to(ROOT)),'files':len(paths),'protected_assets':False,'provider_credentials':False}))


def stage_m65_reproduction():
    """Standalone test/control image, distinct from the deployed application."""
    stage_m64_tests()
    import hashlib,re
    context=ROOT/'data/eval/container-build/m65-reproduction'
    shutil.copytree(ROOT/'data/eval/container-build/m64/files',context/'files',dirs_exist_ok=True)
    paths={'eval/m6_2/robustness_m64.py','scripts/finalize_m6_2_r10_closeout.py','scripts/verify_m65_reproduction.py','tests/test_langgraph_g2.py',
        'scripts/manage_m6_2_container.py','docs/probes/run_m62_r22_offline.py','docs/probes/recompute_m64_r2.py',
        'docs/probes/m64-pg-health-completion.json','docs/probes/m64-completion-image.txt','docs/probes/m64-completion-runtime.txt',
        'docs/probes/m64-policy-final.xml','docs/probes/m64-monitor-final.xml','docs/probes/m64-pg-completion.xml',
        'docs/probes/m64-n01-final-r2.xml','docs/probes/m64-n01-final-r2.log',
        'docs/probes/m64-n01-before.xml','docs/probes/m64-n01-before.log','docs/progress.md',
        'docs/eval/m6.3-personal-execution-20261101/final-closeout-verification-r1.json'}
    prior=ROOT/'docs/eval/m6.4-lean-acceptance-20261101'
    paths.update(p.relative_to(ROOT).as_posix() for p in prior.iterdir() if p.is_file())
    snapshot=json.loads((prior/'source-r1/snapshot.json').read_text())['files']
    paths.update('docs/eval/m6.4-lean-acceptance-20261101/source-r1/'+name for name in (*snapshot,'snapshot.json'))
    paths.update(f'docs/probes/m64-n01-iteration{i}.{extension}' for i in range(1,7) for extension in ('log','xml'))
    evidence=ROOT/'docs/probes/m64-pg-cli-completion'
    from eval.m6_1.store import ResultStore
    store=ResultStore(evidence/'ledger/results.sqlite3')
    paths.update({'docs/probes/m64-pg-cli-completion/r25-proof.json','docs/probes/m64-pg-cli-completion/ledger/results.sqlite3'})
    for trial in store.trials('unprotected-full-app-test'):
        raw=store.raw_result(trial['trial_id'])
        for name in (trial['raw_result_ref'],str(Path(trial['raw_result_ref']).with_suffix('.cleanup.json')),raw['case_path'],
                'ledger/application/'+raw['application_archive']['result']['run_id']+'.checkpoint-cleanup.json'):
            paths.add((evidence/name).relative_to(ROOT).as_posix())
    for name in sorted(paths):
        data=(ROOT/name).read_bytes()
        # Explicit public Python test sources contain fake URI literals (for
        # example the serializer's user:secret fixture). Connection-bearing
        # JSON/SQLite evidence must never contain live credential URLs.
        if not name.endswith('.py') and re.search(rb'postgres(?:ql)?(?:\+psycopg)?://[^\s/]+:[^\s@]+@',data):
            raise ValueError('reproduction refuses embedded database credentials: '+name)
        destination=context/'files'/name;destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(data)
    (context/'Dockerfile').write_text('FROM shoprelay-m62-eval-app:m65\nUSER 0\nCOPY files/ /opt/shoprelay/\n'
        'RUN mkdir -p /results /opt/shoprelay/.tmp /opt/shoprelay/data/test-tmp /opt/shoprelay/data/eval/offline-tests /opt/shoprelay/docs/eval/m6.4-lean-acceptance-r2-20261101 && '
        'chown -R 10002:10002 /results /opt/shoprelay/.tmp /opt/shoprelay/data/test-tmp /opt/shoprelay/docs/probes '
        '/opt/shoprelay/data/eval/offline-tests /opt/shoprelay/docs/eval/m6.4-lean-acceptance-r2-20261101\n'
        'ENV PYTHONPATH=/opt/shoprelay:/opt/shoprelay/tests\nUSER 10002:10002\n',encoding='utf8')
    print(json.dumps({'context':str(context.relative_to(ROOT)),'control_evidence_files':len(paths),
        'purpose':'offline reproduction only; never deploy this test/reference image','private_DB_connection_files_copied':False}))

def stage():
    paths=set(p.relative_to(ROOT).as_posix() for p in (ROOT/'app').rglob('*.py') if '__pycache__' not in p.parts)
    paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT/'eval/m6_1').glob('*.py'))
    paths.update(['requirements-formal-eval.lock','requirements-langgraph-g1.lock','requirements-langgraph-g2.lock','pyproject.toml'])
    for name,fn in [('eval/m6_1/personal_dev.py','implementation_digest'),('eval/m6_1/grants.py','formal_runtime_digest')]:
        tree=ast.parse((ROOT/name).read_text(encoding='utf8'))
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==fn)
        paths.update(n.value for n in ast.walk(node) if isinstance(n,ast.Constant) and isinstance(n.value,str) and n.value.endswith(('.py','.lock','.toml')) and (ROOT/n.value).is_file())
    paths.update(['scripts/run_m6_1_b_synthetic.py','eval/registry/m6.2-container-governance-r4.json','eval/registry/m6.1-a-governance-overlay-r3.json'])
    personal_governance='eval/registry/m6.2-personal-real-eval-governance-r6.json'
    if (ROOT/personal_governance).exists():paths.add(personal_governance)
    CONTEXT.mkdir(parents=True,exist_ok=True)
    for p in sorted(paths):
        dst=CONTEXT/'code'/p;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/p,dst)
    for p in ['requirements-formal-eval.lock','requirements-langgraph-g1.lock','requirements-langgraph-g2.lock','pyproject.toml']:
        shutil.copyfile(ROOT/p,CONTEXT/Path(p).name)
    for p in ['eval/m6_2/__init__.py','eval/m6_2/candidate_construction_r26.py','eval/m6_2/candidate_revision_r27.py','eval/m6_2/container_operations_r26.py','eval/m6_2/personal_execution_r30.py','deploy/m62-eval/issue-approved-formal.py',
            'tests/test_m6_2_r24_formal_application.py','tests/test_m6_2_personal_dev_batch_r7.py']:
        if not (ROOT/p).exists():continue
        dst=CONTEXT/'control'/p;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/p,dst)
    shutil.copyfile(ROOT/'deploy/m62-eval/Dockerfile',CONTEXT/'Dockerfile')
    print(json.dumps({'staged_runtime_files':len(paths),'candidate_builder_in_application':False,'whole_repo_or_secrets_copied':False}))

if __name__=='__main__':
    if sys.argv[1]=='stage':stage()
    elif sys.argv[1]=='stage-m64-tests':stage_m64_tests()
    elif sys.argv[1]=='stage-m65-delivery':stage_m65_delivery()
    elif sys.argv[1]=='stage-customer-experience':stage_m65_delivery(interactive=True)
    elif sys.argv[1]=='stage-m65-reproduction':stage_m65_reproduction()
    elif sys.argv[1]=='up':
        print(compose('up','-d','postgres','proxy','application','control'))
    elif sys.argv[1]=='status':print(compose('ps','--format','json'))
    else:raise ValueError('supported operations:stage/up/status;no destructive or business commands')
