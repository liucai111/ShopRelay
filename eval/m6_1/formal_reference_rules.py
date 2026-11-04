"""Versioned reference comparisons; never production routing or expected answers."""
V2 = 'formal-reference-constraints-v2'
V3 = 'formal-reference-constraints-v3'
ROUTE_KEYS = ('strategy','sub_strategy','safety_decision','needs_clarification','required_capabilities')

def route_reference(case, observed, *, rejected=False):
    if rejected:return 'FAIL'
    if observed is None:return 'UNKNOWN'
    expected=case.expected_outcome['route']
    if not {V2,V3}.intersection(case.rubric_refs):
        return 'PASS' if all((set(observed.get(k,[]))==set(v) if k in {'required_capabilities','intent_labels'} else observed.get(k)==v)
            for k,v in expected.items()) else 'FAIL'
    references=[expected,*case.expected_outcome['task'].get('route_equivalents',[])]
    def matches(ref):
        requested=set(observed['required_capabilities']);needed=set(ref['required_capabilities'])
        return (all(observed[k]==ref[k] for k in ROUTE_KEYS if k!='required_capabilities') and
            needed<=requested<=set(case.input['allowed_capabilities']) and
            (not requested if ref['strategy'] in {'DIRECT','REFUSE'} else True))
    return 'PASS' if any(matches(ref) for ref in references) else 'FAIL'

def resource_coverage(case, invocations):
    required=case.expected_outcome['task'].get('required_resources',[])
    if not required:return 'NOT_APPLICABLE'
    unknown=False
    for constraint in required:
        matching=[r for r in invocations if r['operation']==constraint['tool'] and r['status']=='RETURNED']
        if not matching:return 'FAIL'
        # Missing arguments in old evidence means unknown resource coverage,
        # not proven zero calls. New adapters persist these before execution.
        known=[r['arguments'][constraint['argument']] for r in matching
            if 'arguments' in r and constraint['argument'] in r['arguments']]
        missing=set(constraint['values'])-set(known)
        if missing:
            if len(known)!=len(matching):unknown=True
            else:return 'FAIL'
    return 'UNKNOWN' if unknown else 'PASS'

def result_row_coverage(case,invocations):
    """Verify returned source rows;answer semantics remain separate human items."""
    constraints=case.expected_outcome['task'].get('required_result_rows',[])
    if not constraints:return {'status':'NOT_APPLICABLE','rows':[]}
    assessments=[]
    for constraint in constraints:
        calls=[r for r in invocations if r['operation']==constraint['tool'] and r['status']=='RETURNED']
        observed=[];unreadable=False
        for call in calls:
            if 'read_result' not in call:unreadable=True;continue
            value=call['read_result']
            for field in constraint['result_path']:
                if not isinstance(value,dict) or field not in value:unreadable=True;break
                value=value[field]
            if not isinstance(value,list) or any(not isinstance(r,dict) for r in value):unreadable=True
            else:observed.extend(value)
        for required in constraint['rows']:
            matches=[r for r in observed if r.get(constraint['source_field'])==required['source_id']]
            status=('UNKNOWN' if unreadable or len(matches)>1 else 'FAIL' if not matches else
                'UNKNOWN' if any(k not in matches[0] for k in required['fields']) else
                'PASS' if all(matches[0][k]==v for k,v in required['fields'].items()) else 'FAIL')
            assessments.append({'source_id':required['source_id'],'expected_fields':dict(required['fields']),
                'observed_fields':{k:matches[0].get(k) for k in required['fields']} if len(matches)==1 else None,'status':status})
    status='UNKNOWN' if any(r['status']=='UNKNOWN' for r in assessments) else 'FAIL' if any(r['status']=='FAIL' for r in assessments) else 'PASS'
    return {'status':status,'rows':assessments,'scope':'TOOL_RESULT_SOURCE_ROWS;does not prove every row was described in user output'}

def direct_source_facts(case,labels=None):
    """Compare same-source direct fields only;derived/policy semantics stay separate."""
    labels=case.expected_outcome if labels is None else labels
    sources={}
    for records in case.input['fixture'].values():
        if isinstance(records,(list,tuple)):
            for record in records:
                if 'id' in record:sources[record['id']]=record
                elif 'evidence_id' in record:sources[record['evidence_id']]=record
    checked=[];mismatches=[];remaining=[]
    for fact in labels['atomic_facts']:
        ids=fact['source_ids'];predicate=fact['predicate']
        if len(ids)==1 and ids[0] in sources and predicate in sources[ids[0]]:
            source=sources[ids[0]][predicate]
            row={'fact_id':fact['fact_id'],'source_id':ids[0],'predicate':predicate,'reference_value':fact['value'],'source_value':source}
            checked.append(row)
            if fact['value']!=source:mismatches.append(row)
        else:remaining.append(fact['fact_id'])
    return {'checked':checked,'mismatches':mismatches,'non_direct_facts':remaining}
