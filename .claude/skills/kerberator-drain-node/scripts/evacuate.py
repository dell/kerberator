#!/usr/bin/env python3
"""Optional ordinary-pod evacuation; separate from `kubectl krb drain-node`.

Plans are read-only and pinned to a node and pod roster. Execute invokes exactly
one conservative `kubectl drain --ignore-daemonsets`; verify only observes state.
"""
import argparse
import json
import os
import subprocess
import sys

from common import Stop, checksum, getjson, kubectl, load_plan, output_error, resolve_context, run, validate_node


def classify(p):
    m, s = p.get('metadata') or {}, p.get('spec') or {}
    owner = next((o for o in m.get('ownerReferences') or [] if o.get('controller')), {})
    mirror = bool((m.get('annotations') or {}).get('kubernetes.io/config.mirror'))
    kind = owner.get('kind', '')
    return {'namespace':m.get('namespace',''), 'name':m.get('name',''), 'uid':m.get('uid',''),
            'phase':(p.get('status') or {}).get('phase','Unknown'),
            'owner_kind':kind, 'owner_name':owner.get('name',''), 'mirror':mirror,
            'emptydir':[v.get('name','?') for v in s.get('volumes') or [] if 'emptyDir' in v],
            'ordinary':not mirror and kind != 'DaemonSet', 'unmanaged':not mirror and kind != 'DaemonSet' and not owner}


def observe(base, name):
    nd = getjson(base,'get','node',name)
    node = {'name':nd.get('metadata',{}).get('name'), 'uid':nd.get('metadata',{}).get('uid'),
            'cordoned':bool(nd.get('spec',{}).get('unschedulable',False)),
            'ready':next((x.get('status')=='True' for x in (nd.get('status',{}).get('conditions') or []) if x.get('type')=='Ready'),None)}
    pods = getjson(base,'get','pods','-A','--field-selector',f'spec.nodeName={name}').get('items') or []
    return node,sorted((classify(p) for p in pods),key=lambda x:(x['namespace'],x['name'],x['uid']))


def can_i(base,verb,resource,namespace=None):
    cmd=[*base,'auth','can-i',verb,resource]
    if namespace=='*':cmd.append('--all-namespaces')
    elif namespace:cmd += ['-n',namespace]
    try:
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=15)
        answer=r.stdout.strip().lower()
        return {'verb':verb,'resource':resource,'namespace':namespace,'allowed':r.returncode==0 and answer=='yes','answer':answer if answer in ('yes','no') else 'unknown'}
    except (OSError,subprocess.TimeoutExpired):
        return {'verb':verb,'resource':resource,'namespace':namespace,'allowed':False,'answer':'unknown'}


def read_evac_plan(path):
    p=load_plan(path)
    if p.get('kind')!='workload-evacuation' or p.get('version')!=1:
        raise Stop('Not an OP-07 workload evacuation plan')
    return p


def make_plan(a):
    ctx=resolve_context(a.kubeconfig,a.context)
    base=kubectl(ctx,a.kubeconfig)
    if not a.node:
        nodes=getjson(base,'get','nodes').get('items') or []
        print(json.dumps({'status':'CHOICE_REQUIRED','context':ctx,'nodes':[x.get('metadata',{}).get('name') for x in nodes]},indent=2))
        return 2
    name=validate_node(a.node)
    node,pods=observe(base,name)
    candidates=[x for x in pods if x['ordinary'] and x['phase'] not in ('Succeeded','Failed')]
    checks=[can_i(base,'get','nodes'),can_i(base,'patch','nodes'),can_i(base,'list','pods','*')]
    checks += [can_i(base,'create','pods/eviction',ns) for ns in sorted({x['namespace'] for x in candidates})]
    blockers=[]
    if any(not x['allowed'] for x in checks):blockers.append('Cannot confirm node/pod/eviction RBAC')
    if any(x['unmanaged'] for x in candidates):blockers.append('Unmanaged pods: kubectl drain would require --force')
    if any(x['emptydir'] for x in candidates):blockers.append('emptyDir volumes: kubectl drain would require --delete-emptydir-data')
    try:
        pdbs=getjson(base,'get','pdb','-A').get('items') or []
        pdbs=[{'namespace':x.get('metadata',{}).get('namespace'), 'name':x.get('metadata',{}).get('name'),
               'disruptions_allowed':x.get('status',{}).get('disruptionsAllowed')} for x in pdbs]
    except Stop as e:
        pdbs=[];blockers.append('PDB inventory unavailable: '+str(e))
    p={'version':1,'kind':'workload-evacuation','context':ctx,
       'kubeconfig':os.path.abspath(a.kubeconfig) if a.kubeconfig else None,
       'node':name,'node_before':node,'pods_before':pods,'candidates':candidates,
       'pdbs':pdbs,'permissions':checks,'blockers':blockers,
       'status':'BLOCKED' if blockers else 'READY',
       'effects':['Cordon node and evict ordinary controlled pods (respect PDBs)',
                  'DaemonSet and mirror/static pods remain',
                  'No --force, --disable-eviction, --delete-emptydir-data, or automatic uncordon',
                  'Does NOT invoke kubectl krb drain-node',
                  'Workload rescheduling, storage, capacity, Kerberos credentials require separate review']}
    p['plan_id']=checksum(p)
    if a.out:
        with open(a.out,'w',encoding='utf-8') as f:json.dump(p,f,indent=2);f.write('\n')
    print(json.dumps(p,indent=2))
    return 1 if blockers else 0


def execute(a):
    p=read_evac_plan(a.plan)
    if p.get('status')!='READY' or p.get('blockers'):raise Stop('Preflight blocked')
    if a.approve!=p['node']:raise Stop('Approved node does not match plan')
    if not 30<=a.timeout_seconds<=3600:raise Stop('Timeout must be 30–3600 seconds')
    base=kubectl(p['context'],p['kubeconfig'])
    node,pods=observe(base,p['node'])
    if node['uid']!=p['node_before']['uid'] or node['cordoned']!=p['node_before']['cordoned'] or pods!=p['pods_before']:
        raise Stop('Node or pod state changed; run plan again')
    cmd=[*base,'drain',p['node'],'--ignore-daemonsets',f'--timeout={a.timeout_seconds}s']
    try:
        result=run(cmd,timeout=a.timeout_seconds+30)
        print(json.dumps({'status':'COMMAND_COMPLETED','output':result,'next':'Run evacuate.py verify; do not auto-uncordon'},indent=2))
        return 0
    except Stop as e:
        print(json.dumps({'status':'PARTIAL_OR_FAILED','reason':str(e),
          'next':'Verify current state; node may be cordoned and some pods evicted. Do not retry or uncordon blindly.'},indent=2))
        return 1


def verify(a):
    p=read_evac_plan(a.plan)
    node,pods=observe(kubectl(p['context'],p['kubeconfig']),p['node'])
    if node['uid']!=p['node_before']['uid']:raise Stop('Node identity changed')
    remaining=[x for x in pods if x['ordinary'] and x['phase'] not in ('Succeeded','Failed')]
    ok=node['cordoned'] and not remaining
    print(json.dumps({'status':'EVACUATED' if ok else 'NOT_FULLY_EVACUATED',
       'node':p['node'],'context':p['context'],'cordoned':node['cordoned'],
       'ordinary_pods_remaining':remaining,'daemonset_and_mirror_pods_remaining':[x for x in pods if not x['ordinary']],
       'note':'Node clearance only; does not verify replacement workload readiness, available destination capacity, storage or Kerberos minting.'},indent=2))
    return 0 if ok else 1


def main():
    pa=argparse.ArgumentParser(description='Optional guarded ordinary-workload evacuation')
    subs=pa.add_subparsers(dest='stage',required=True)
    a=subs.add_parser('plan');a.add_argument('--node');a.add_argument('--context');a.add_argument('--kubeconfig');a.add_argument('--out')
    a=subs.add_parser('execute');a.add_argument('--plan',required=True);a.add_argument('--approve',required=True);a.add_argument('--timeout-seconds',type=int,default=600)
    a=subs.add_parser('verify');a.add_argument('--plan',required=True)
    args=pa.parse_args()
    return {'plan':make_plan,'execute':execute,'verify':verify}[args.stage](args)

if __name__=='__main__':
    try:sys.exit(main())
    except Stop as e:sys.exit(output_error(e))
