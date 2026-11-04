from copy import deepcopy
import pytest
from eval.m6_2.personal_closeout_r30 import validate_ai

def values():
    rows=[{'case':{'case_id':'isolated-case'},'raw_sha256':'original-raw','raw':{'application_archive':{'result':{'run_id':'actual-run'}},'code_assessment':{'task_outcome':'FAIL'}}}]
    review={'method':'AI_ASSISTED_POSTHOC_OUTPUT_REVIEW_NOT_HUMAN','items':[{'case_id':'isolated-case','run_id':'actual-run','raw_sha256':'original-raw','task_outcome':'FAIL','rationale':'No delivered output;candidate quality is separate','human_decision':None,'reviewer':None,'reviewed_at':None}]}
    return rows,review

def test_bound_review_keeps_delivery_failure_and_no_human_signature():
    rows,review=values();assert validate_ai(rows,review)['isolated-case']['task_outcome']=='FAIL'

@pytest.mark.parametrize('field,value',[('run_id','another-run'),('raw_sha256','changed-raw'),('task_outcome','PASS'),('reviewer','AI_IMPERSONATING_HUMAN')])
def test_wrong_review_binding_or_fabricated_human_decision_rejected(field,value):
    rows,review=values();review['items'][0][field]=value
    with pytest.raises(ValueError):validate_ai(rows,review)

def test_missing_or_duplicate_case_review_rejected():
    rows,review=values();review['items'].append(deepcopy(review['items'][0]))
    with pytest.raises(ValueError):validate_ai(rows,review)
