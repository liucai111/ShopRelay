# -*- coding: utf-8 -*-
# Read-only standard-library integrity verifier. Run inside the bundle folder.
import csv,hashlib,json,pathlib,collections
p=pathlib.Path(__file__).parent
a=json.loads((p/'route-m2.2-five-way-candidate-20261021-r1.json').read_text('utf-8'))
b=json.loads((p/'route-m2.2-five-way-candidate-20261021-r2.json').read_text('utf-8'))
m=json.loads((p/'route-m2.2-r2-sha256-manifest.json').read_text('utf-8'))
def h(x): return hashlib.sha256(x).hexdigest()
def canon(x): return h(json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode())
def casesha(c): return canon({k:v for k,v in c.items() if k not in ('case_sha256','review_history')})
assert len(a['cases'])==len(b['cases'])==100
assert canon(a['cases'])==a['dataset_sha256_canonical_cases']
assert canon(b['cases'])==b['dataset_sha256_canonical_cases']
assert h((p/'route-m2.2-five-way-candidate-20261021-r1.json').read_bytes())==m['original_r1']['file_sha256']
assert h((p/'route-m2.2-five-way-candidate-20261021-r2.json').read_bytes())==m['r2']['file_sha256']
old={c['case_id']:c for c in a['cases']};new={c['case_id']:c for c in b['cases']}
assert set(old)==set(new)
changes=[]
for cid,x in new.items():
 y=old[cid]
 assert x['case_sha256']==casesha(x)
 assert x['user_query']==y['user_query'] and x['dataset_split']==y['dataset_split'] and x['group_id']==y['group_id']
 assert x['review_status']=='PENDING_HUMAN_REVIEW'
 if x['expected_strategy']!=y['expected_strategy']:
  assert y['expected_strategy']=='MULTI' and x['expected_strategy']=='SINGLE'
  changes.append(cid)
 else:
  assert x['case_sha256']==y['case_sha256']
assert len(changes)==13
assert dict(collections.Counter(x['expected_strategy'] for x in new.values()))=={'DIRECT':20,'SINGLE':33,'MULTI':7,'AGENT':20,'REFUSE':20}
with (p/'route-m2.2-five-way-candidate-review-r2-user-confirmed.csv').open(encoding='utf-8-sig',newline='') as f:
 rows=list(csv.DictReader(f))
assert len(rows)==100 and {r['case_id'] for r in rows}==set(old)
for r in rows:
 x=new[r['case_id']]
 assert r['base_dataset_sha256']==b['dataset_sha256_canonical_cases']
 assert r['base_case_sha256']==x['case_sha256']
 assert r['reviewer_decision']=='APPROVE_AS_IS' and r['corrected_strategy']==''
 assert r['reviewer']=='CHAT_USER_SELF_CONFIRMED' and r['reviewed_at'].endswith('Z')
print('PASS: 100 reviewed rows, 13 revisions, source r1/r2 case and dataset hashes valid, 0 scoring calls')
