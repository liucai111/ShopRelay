"""Offline SDK substitute for explicitly engineering-only container image mode.

It accepts no Gold. Invalid/unknown-use cases remain failures, never sampled
again. This module is not evidence of real Provider or LLM task performance.
"""
from types import SimpleNamespace
import json

def install():
    from app.m0 import config
    class SDK:
        def __init__(self):self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self,**kwargs):
            if kwargs.get('max_retries',0)!=0:raise AssertionError('engineering SDK retries forbidden')
            return self
        def create(self,**kwargs):
            # Capture actual serialized input, not a hand-built request. Never
            # write hidden reasoning, credentials or expected labels.
            serialized=json.dumps(kwargs['messages'],ensure_ascii=False)
            if any(k in serialized for k in ['expected_outcome','required_tool_groups','DRAFT_SEMANTIC_REFERENCE','reference_answer']):
                raise AssertionError('Gold leaked into actual model request')
            from pathlib import Path
            from .contracts import canonical_json
            with Path('/opt/shoprelay/data/eval/formal-ledgers/sdk-requests.jsonl').open('a') as f:
                f.write(canonical_json({'messages':kwargs['messages'],'thinking':kwargs.get('extra_body'),'max_tokens':kwargs['max_tokens']})+'\n')
            if 'Classify' in kwargs['messages'][0]['content']:
                content=json.dumps({'strategy':'DIRECT','required_capabilities':['invalid-unregistered-tool']})
            else:
                content=json.dumps({'answer':'Fictional warranty lasts 18 months from recorded purchase.','citations':['fiction-warranty']})
            usage=SimpleNamespace(total_tokens=50,model_dump=lambda:{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50})
            if 'unknown usage canary' in serialized:usage=None
            return SimpleNamespace(model=kwargs['model'],usage=usage,choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=content))])
    config.create_deepseek_client=lambda *a,**k:SDK()
