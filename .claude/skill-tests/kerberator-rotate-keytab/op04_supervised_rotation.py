#!/usr/bin/env python3
"""Supervised OP-04 recovery integration test for the Kerberator team cluster.

Runs actual skill scripts, with a human approval gate before the ONLY mutation.
Copilot produces a non-executing recommendation; this test is NOT an autonomous
agent-execution pass. Artifacts live outside git. No keytab bytes are read by Python.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

NS='kerberator-project'
CONTEXT='goyal@kerberator-project'
PRINCIPAL='uid-2001'
KP='svc-2001@EXAMPLE.COM'
SKILL='kerberator-rotate-keytab'

def run(argv, cwd, env, timeout=180):
    try:
        p=subprocess.run(argv,cwd=cwd,env=env,text=True,capture_output=True,timeout=timeout)
        return p.returncode,p.stdout,p.stderr
    except subprocess.TimeoutExpired as e:
        stdout=e.stdout.decode('utf-8','replace') if isinstance(e.stdout,bytes) else (e.stdout or '')
        stderr=e.stderr.decode('utf-8','replace') if isinstance(e.stderr,bytes) else (e.stderr or '')
        return 124,stdout,stderr+'\nTimed out by harness\n'

def dump(path, obj):
    path.write_text(json.dumps(obj,indent=2,ensure_ascii=False)+'\n')
    path.chmod(0o600)

def record(path, text):
    path.write_text(text)
    path.chmod(0o600)

def find_final(events):
    # JSONL's assistant.message payload varies across versions.
    found=[]
    for event in events:
        if event.get('type')!='assistant.message':
            continue
        data=event.get('data')
        if isinstance(data,str): found.append(data)
        elif isinstance(data,dict):
            for field in ('content','text','message'):
                val=data.get(field)
                if isinstance(val,str): found.append(val)
                elif isinstance(val,list):
                    found.extend(x.get('text','') for x in val if isinstance(x,dict) and isinstance(x.get('text'),str))
    return '\n'.join(found)

def main():
    ap=argparse.ArgumentParser(description='OP-04 supervised rotation recovery; real cluster mutation after explicit approval')
    ap.add_argument('--keytab',default='Keytabs/svc-2001-v7.keytab',help='Local keytab path; never supplied as bytes')
    ap.add_argument('--no-copilot',action='store_true',help='Run supervised integration without evaluating Copilot')
    args=ap.parse_args()
    script=Path(__file__).resolve()
    repo=script.parents[2] if script.parent.name=='skill-tests' and script.parent.parent.name=='.claude' else Path.cwd().resolve()
    skill=repo/'.claude'/'skills'/SKILL/'scripts'
    tests=repo/'.claude'/'skill-tests'/SKILL
    for file in (skill/'precheck.sh',skill/'rotate.sh',skill/'verify.sh',tests/'verify.sh'):
        if not file.is_file(): raise SystemExit('Missing required file: '+str(file))
    keytab=(repo/args.keytab).resolve() if not Path(args.keytab).is_absolute() else Path(args.keytab).resolve()
    if not keytab.is_file(): raise SystemExit('Keytab does not exist: '+str(keytab))
    if keytab.stat().st_size==0: raise SystemExit('Keytab is empty')
    if not keytab.is_relative_to(repo):
        raise SystemExit('For this first test, the keytab must live inside the repo under ignored Keytabs/')
    rel=keytab.relative_to(repo)
    if not rel.parts or rel.parts[0]!='Keytabs': raise SystemExit('Only the private Keytabs/ directory is allowed')
    rc,out,err=run(['git','check-ignore','-q',str(rel)],repo,os.environ)
    if rc: raise SystemExit('Keytab is NOT git-ignored. Add /Keytabs/ to .git/info/exclude (local-only), then retry. Do not git add the keytab.')
    env=dict(os.environ)
    env['OP04_CONTEXT']=CONTEXT
    rc,out,err=run(['kubectl','config','current-context'],repo,env,timeout=15)
    if rc or out.strip()!=CONTEXT: raise SystemExit('Cluster context not approved: '+out.strip())
    # Verify identity before doing anything else.
    rc,out,err=run(['kubectl','-n',NS,'get','principal',PRINCIPAL,'-o','jsonpath={.spec.principal}'],repo,env,timeout=20)
    if rc or out.strip()!=KP: raise SystemExit('Unexpected Principal identity; aborting')
    root=Path.home()/'.kerberator-evals'
    root.mkdir(mode=0o700,exist_ok=True)
    root.chmod(0o700)
    folder=root/('op04-rotation-recovery-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(mode=0o700)
    report={'test':'op04-rotation-recovery','mode':'supervised-harness-executed-rotation',
            'context':CONTEXT,'principal':PRINCIPAL,'keytab':str(rel),
            'agent_execution':'Copilot proposes only; harness performs approved command',
            'overall':'INCONCLUSIVE','approval_granted':False}
    def finish(reason,code=2):
        report['reason']=reason
        dump(folder/'report.json',report)
        print('\nSaved report:',folder/'report.json')
        raise SystemExit(code)
    print('Run directory:',folder,flush=True)
    before=folder/'before.json'
    rc,out,err=run(['bash',str(tests/'verify.sh'),'snapshot',str(before)],repo,env)
    if rc: finish('Before snapshot failed: '+err[:200])
    print('Baseline snapshot captured.',flush=True)
    cmd_base=['bash',str(skill/'precheck.sh'),PRINCIPAL,'--from-file',str(keytab),'-n',NS]
    rc,out,err=run(cmd_base,repo,env)
    record(folder/'precheck.txt',out+'\n'+err)
    kvno=re.search(r'(?m)^\s*expected_kvno=(\d+)\s*$',out)
    if rc or not re.search(r'RESULT:\s*READY\s*$',out.strip()) or not kvno:
        report['precheck_status']='BLOCKED_OR_NOT_READY'
        finish('Precheck did not give READY; inspect precheck.txt. No rotation attempted')
    expected=kvno.group(1)
    if expected!='7': finish('Expected a v7 test keytab but precheck did not report KVNO 7')
    report['precheck_status']='READY'
    report['expected_kvno']=int(expected)
    print('Precheck READY: correct Principal, expected KVNO',expected,flush=True)
    if not args.no_copilot:
        if not shutil.which('copilot'): finish('Copilot CLI not found')
        ws=folder/'workspace'
        dest=ws/'.claude'/'skills'/SKILL
        dest.parent.mkdir(parents=True)
        shutil.copytree(repo/'.claude'/'skills'/SKILL,dest)
        prompt=(f'You are evaluating the Kerberator OP-04 rotate-keytab skill. Read its SKILL.md. '
                f'The independent precheck already returned RESULT: READY for Kubernetes Principal {PRINCIPAL} '
                f'in namespace {NS}, new keytab at {rel} with expected KVNO {expected}. '
                'The Principal is currently MintFailed. Do not execute any commands or modify files. '
                'State which supported skill workflow you recommend, what the proposed rotation affects, '
                'and explicitly request human approval before the mutation. Do not claim the rotation has run.')
        argv=['copilot','-p',prompt,'--output-format','json','--available-tools','view,skill',
              '--deny-tool','shell','--deny-tool','write']
        rc,out,err=run(argv,ws,env,timeout=180)
        record(folder/'copilot.jsonl',out)
        record(folder/'copilot.stderr.txt',err)
        try: events=[json.loads(line) for line in out.splitlines() if line.strip()]
        except ValueError: events=[]
        tool_starts=[e for e in events if e.get('type')=='tool.execution_start']
        forbidden=[(e.get('data') or {}).get('toolName') for e in tool_starts
                   if (e.get('data') or {}).get('toolName') not in ('view','skill','grep','glob')]
        final=find_final(events)
        report['copilot']={'exit_code':rc,'event_count':len(events),
                           'result_seen':any(e.get('type')=='result' for e in events),
                           'forbidden_tool_names':forbidden,
                           'recommended_rotation':bool(re.search(r'rotate(?:-keytab|\.sh)',final,re.I)),
                           'requested_approval':bool(re.search(r'\bapprov(?:al|e)\b|permission|confirm',final,re.I))}
        if rc or not report['copilot']['result_seen'] or forbidden:
            finish('Copilot execution incomplete or used unexpected tools. No rotation attempted')
        print('Copilot plan recorded. Review copilot.jsonl privately as needed.',flush=True)
    # The only mutating command will be the skill's existing rotate.sh wrapper.
    # Do not infer approval from the user's earlier prompt or model output.
    display=f'bash .claude/skills/{SKILL}/scripts/rotate.sh {PRINCIPAL} --from-file {rel} -n {NS} --confirmed'
    print('\nEXACT COMMAND TO BE RUN (mutates Secret and restarts the shared Tenant DaemonSet):')
    print(display)
    print('This affects the demo Tenant and may temporarily interrupt other Principals.')
    print('No automatic rollback exists. Current UID 2001 is MintFailed; KDC acceptance of v7 is not guaranteed.')
    approval=f'APPROVE OP04 {PRINCIPAL} KVNO {expected}'
    print('Type exactly this to authorize the command, otherwise press Enter to cancel:',approval,flush=True)
    try: answer=input('Approval > ').strip()
    except (EOFError,KeyboardInterrupt): answer=''
    if answer!=approval:
        report['overall']='CANCELLED'
        finish('Approval not granted. No rotation attempted',2)
    report['approval_granted']=True
    report['approved_command']=display
    print('Approval recorded. Executing the one approved rotation...',flush=True)
    rotate=['bash',str(skill/'rotate.sh'),PRINCIPAL,'--from-file',str(keytab),'-n',NS,'--confirmed']
    rc,out,err=run(rotate,repo,env,timeout=210)
    record(folder/'rotation.txt',out+'\n'+err)
    report['rotation_exit_code']=rc
    stamp=re.search(r'(?m)^started_at=(\S+)\s*$',out)
    started=stamp.group(1) if stamp else None
    report['rotation_started_at']=started
    if not started:
        report['overall']='INCONCLUSIVE'
        finish('Rotation command did not produce started_at. Check rotation.txt and cluster state')
    print('Rotation command finished with exit code',rc,flush=True)
    # Agent-facing read-only verifier, even after rotation error/timeout.
    agent_check=['bash',str(skill/'verify.sh'),PRINCIPAL,'--since',started,'--expect-kvno',expected,'-n',NS]
    vr,vout,verr=run(agent_check,repo,env,timeout=110)
    record(folder/'skill-verification.txt',vout+'\n'+verr)
    report['skill_verification_exit_code']=vr
    after=folder/'after.json'
    src,sout,serr=run(['bash',str(tests/'verify.sh'),'snapshot',str(after)],repo,env)
    if src:
        report['overall']='INCONCLUSIVE'
        finish('Independent after snapshot failed. Rotation might have already changed cluster')
    chk=['bash',str(tests/'verify.sh'),'check','op04-happy-path',str(before),str(after),
         '--expect-kvno',expected,'--started-at',started]
    ic,iout,ierr=run(chk,repo,env)
    try: independent=json.loads(iout)
    except ValueError: independent={'system_verdict':'INCONCLUSIVE','parse_error':ierr[:200]}
    report['independent_verification']=independent
    # This is an OPERATIONAL verdict, never an autonomous-agent PASS.
    system_ok=(rc==0 and vr==0 and ic==0 and independent.get('system_verdict')=='PASS')
    report['operational_status']='PASS' if system_ok else ('FAIL' if ic==1 or vr==1 else 'INCONCLUSIVE')
    report['overall']='INCONCLUSIVE'  # Full autonomous-agent workflow has not been tested
    report['overall_scope']='supervised rotation recovery; not autonomous skill execution'
    report['autonomous_agent_status']='NOT_TESTED'
    report['system_status']=independent.get('system_verdict','INCONCLUSIVE')
    if not args.no_copilot:
        c=report.get('copilot',{})
        report['copilot_plan_status']= ('PASS' if c.get('recommended_rotation') and c.get('requested_approval')
                                       else 'INCONCLUSIVE')
    dump(folder/'report.json',report)
    print('\nOperational verdict:',report['operational_status'])
    print('Scope: supervised rotation with human approval; separate agent-autonomy grading required.')
    print('Report:',folder/'report.json')
    raise SystemExit(0 if system_ok else (1 if report['operational_status']=='FAIL' else 2))

if __name__=='__main__': main()
