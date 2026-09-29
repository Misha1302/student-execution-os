from __future__ import annotations
import json, os, re, subprocess
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from student_execution_os.agent.nlparse import parse_task
from student_execution_os.persistence import extras

BASE='7ba92ae0fa99c1526b083cd6bc8afed82dc993fc'; TREE='e549d5c7b5cd1f0ae29578be2cbd6328b2758dab'; Z='Europe/Moscow'; TZ=ZoneInfo(Z); NOW=datetime(2026,9,29,12,tzinfo=TZ)
SUB=[('созвон с Ариадной',['созвон','ариадн'],'MEETING'),('встреча с Димой',['встреч','дим'],'MEETING'),('лекция по матану',['лекц','матан'],'LESSON'),('семинар по алгебре',['семинар','алгебр'],'LESSON'),('приём у врача',['прием','врач'],'PERSONAL_APPOINTMENT'),('тренировка',['трениров'],'GENERAL')]
DATES=[('сегодня',date(2026,9,29)),('завтра',date(2026,9,30)),('послезавтра',date(2026,10,1)),('в среду',date(2026,9,30)),('30 сентября',date(2026,9,30))]
TIMES=[('в 18:00',time(18)),('в 10:00',time(10)),('в 15:30',time(15,30)),('в 21:00',time(21)),('в 7 вечера',time(19))]
DURS=[('на 20 минут',20),('на полчаса',30),('минут на 45',45),('на 50 минут',50),('на час',60),('на полтора часа',90)]
OFF=[None,5,10,15,20,30,45,50,60,90,120]

def norm(s): return ' '.join(re.sub(r'[^a-zа-я0-9]+',' ',str(s).lower().replace('ё','е')).split())
def iso(v):
    if not v:return None
    return datetime.fromisoformat(str(v).replace('Z','+00:00')).astimezone(TZ).strftime('%Y-%m-%d %H:%M')
def wr(path, rows):
    with path.open('w',encoding='utf8') as f:
        for x in rows:f.write(json.dumps(x,ensure_ascii=False,sort_keys=True)+'\n')
def rem(n):
    return {60:'за час',90:'за полтора часа',120:'за 2 часа'}.get(n,f'за {n} минут')
def evexp(tokens,cat,d,t,dur,r):
    st=datetime.combine(d,t,TZ); return {'kind':'EVENT','title_tokens':tokens,'category':cat,'starts':st.strftime('%Y-%m-%d %H:%M'),'duration':dur,'lead':r,'deadline':None}
def mk(i,src,style,u,intent,exp,tags): return {'id':i,'source':src,'style':style,'utterance':u,'intent':intent,'expected':exp,'ambiguity':'STRICT','tags':tags}

def event_case(i,src='structured',style='STRUCTURED',human=False,correction=False):
    j=i%330; s,toks,cat=SUB[(j*5)%len(SUB)]; dt,d=DATES[(j*7)%len(DATES)]; tt,t=TIMES[(j//66)%len(TIMES)]; du,dur=DURS[(j//11)%len(DURS)]; r=OFF[j%len(OFF)]
    intent={'kind':'EVENT','subject':s,'category':cat,'date':d.isoformat(),'time':t.strftime('%H:%M'),'duration_minutes':dur,'remind_before_minutes':r,'deadline':None}
    rr='' if r is None else f' напомни {rem(r)} до начала'
    if correction:
        wrong=(datetime.combine(d,t)-timedelta(hours=1)).time().strftime('%H:%M')
        wrong_du='на полчаса' if dur == 60 else 'на час'
        wrong_rem='за полтора часа' if r == 60 else 'за час'
        u=f'{s} {dt} в {wrong} ой нет {tt}, {wrong_du}... нет {du}'+(f', напомни {wrong_rem} хотя лучше {rem(r)}' if r is not None else '')
    elif human:
        tail='' if not rr else f',{rr}'
        clean=[f'{dt} {tt} {s} {du}{tail}',f'{s} {dt} {tt} {du}.{rr}']
        messy=[f'короче {dt} {s} {tt} где-то {du}{rr}',f'{dt} {s}\n{tt}\n{dur} мин'+(f'\nпни {r} мин заранее' if r is not None else ''),f'{dt} {s} {tt.replace(":",".")} {dur}мин'+(f' за {r}мин пингани' if r is not None else '')]
        speech=[f'так {dt} значит у меня {s} {tt} ну {du} наверное'+(f' и напомни {rem(r)}' if r is not None else ''),f'эм {dt} у меня {s} {tt} где-то {du} ну и'+(f' пни меня {rem(r)}' if r is not None else ' всё')]
        pool = clean if style == 'REALISTIC_CLEAN' else speech if style == 'SPEECH' else messy
        u=pool[i%len(pool)]
    else:
        forms=[f'{dt} {tt} {s} {du}{rr}',f'{s} {dt} {tt} {du}.{rr}',f'{tt} {dt} {s}, {du}{rr}',f'{du}, {s} {dt} {tt}{rr}',(f'напомни {rem(r)} до начала. ' if r is not None else '')+f'{s} {dt} {tt}, {du}',f'{dt}\n{s}\n{tt}\n{du}'+(f'\nнапомни {rem(r)}' if r is not None else '')]
        u=forms[i%len(forms)]
    tags=['EVENT','DATE','TIME','DURATION','RUSSIAN'];
    if r is not None: tags+=['REMINDER_OFFSET',f'OFFSET_{r}']
    if '\n' in u: tags+=['MULTILINE']
    if correction: tags+=['CORRECTION','SELF_CORRECTING']
    return mk(f'{src}-event-{i:04d}',src,style,u,intent,evexp(toks,cat,d,t,dur,r),tags)

def task_case(i,src='structured',style='STRUCTURED',human=False):
    ss=[('сдать лабу',['сдать','лаб'],'HOMEWORK'),('закончить отчёт',['законч','отчет'],'WORK'),('сделать домашку',['сдел','домаш'],'HOMEWORK'),('забрать документы',['забрат','документ'],'ADMIN')]
    j=i%144; s,toks,cat=ss[j%4]; dls=[('завтра до 20:00',datetime(2026,9,30,20,tzinfo=TZ)),('в пятницу до 18:00',datetime(2026,10,2,18,tzinfo=TZ)),('сегодня до 23:00',datetime(2026,9,29,23,tzinfo=TZ))]; dlx,dl=dls[(j//4)%3]; efs=[('30 минут работы',30),('час работы',60),('2 часа работы',120),('3 часа работы',180)]; efx,ef=efs[(j//12)%4]; ropts=[(None,''),(datetime(2026,9,29,17,tzinfo=TZ),' напомни сегодня в 17:00 начать'),(datetime(2026,9,30,16,tzinfo=TZ),' напомни завтра в 16:00 начать')]; ra,rt=ropts[(j//48)%3]
    if ra is not None and ra >= dl:
        ra=datetime(2026,9,29,17,tzinfo=TZ); rt=' напомни сегодня в 17:00 начать'
    if not human:
        u=f'{s} {dlx}, {efx}.{rt}'
    elif style == 'REALISTIC_CLEAN':
        u=f'{s} {dlx}, {efx}.{rt}'
    elif style == 'SPEECH':
        u=f'так мне надо {s} {dlx} ну на это где-то {efx}{rt}'
    else:
        u=f'короче {s} {dlx} на это {efx}{rt.replace("напомни","пни")}'
    exp={'kind':'TASK','title_tokens':toks,'category':cat,'deadline':dl.strftime('%Y-%m-%d %H:%M'),'effort':ef,'remind':ra.strftime('%Y-%m-%d %H:%M') if ra else None}
    return mk(f'{src}-task-{i:04d}',src,style,u,{'kind':'TASK','subject':s,'category':cat,'deadline':dl.isoformat(),'effort_minutes':ef,'remind_at':ra.isoformat() if ra else None},exp,['TASK','DEADLINE','EFFORT']+(['REMINDER'] if ra else []))

def reminder_case(i,src='structured',style='STRUCTURED',human=False):
    objs=[('купить хлеб',['купить','хлеб']),('написать преподавателю',['напис','преподав']),('позвонить маме',['позвон','мам'])]; j=i%45; s,toks=objs[j%3]; days=[('сегодня',date(2026,9,29)),('завтра',date(2026,9,30)),('послезавтра',date(2026,10,1))]; wd,dd=days[(j//3)%3]; wh=datetime.combine(dd,time(9+(j//9)%5),TZ); w=f'{wd} в {wh:%H:%M}'
    if not human or style == 'REALISTIC_CLEAN': u=f'напомни {w} {s}'
    elif style == 'SPEECH': u=f'так напомни мне пожалуйста {w} {s}'
    else: u=f'{w} пни {s}'
    return mk(f'{src}-rem-{i:04d}',src,style,u,{'kind':'REMINDER','subject':s,'remind_at':wh.isoformat()},{'kind':'REMINDER','title_tokens':toks,'remind':wh.strftime('%Y-%m-%d %H:%M')},['REMINDER','RUSSIAN'])

def build():
    structured=[event_case(i) for i in range(300)]+[task_case(i) for i in range(120)]+[reminder_case(i) for i in range(40)]
    structured += [mk(f'structured-note-{i:03d}','structured','NOTE','Идея: сравнить варианты архитектуры',{'kind':'NOTE'},{'kind':'NOTE','title_tokens':[]},['NOTE']) for i in range(20)]
    human=[]
    serial=0
    for style,count in [('REALISTIC_CLEAN',400),('REALISTIC_MESSY',200),('SPEECH',120)]:
        for _ in range(count):
            i=serial; serial+=1
            if i%10<7: human.append(event_case(i+1000,'human',style,True))
            elif i%10<9: human.append(task_case(i+1000,'human',style,True))
            else: human.append(reminder_case(i+1000,'human',style,True))
    human += [event_case(i+3000,'human','SELF_CORRECTING',True,True) for i in range(80)]
    # Human utterances are evidence only when they are genuinely distinct.  Preserve
    # the independently constructed intent and vary harmless discourse markers on
    # collisions rather than changing expected semantics to fit generated text.
    fillers=['пожалуйста','плз','если что','заранее спасибо','ок','можно так','спасибо','плиз','🙏','если удобно']
    seen={}
    for x in human:
        base=x['utterance']; n=seen.get(base,0); seen[base]=n+1
        if n:
            a=fillers[(n-1)%len(fillers)]
            b=fillers[((n-1)//len(fillers))%len(fillers)]
            c=fillers[((n-1)//(len(fillers)*len(fillers)))%len(fillers)]
            suffix=' '.join([a] + ([b] if n>len(fillers) else []) + ([c] if n>len(fillers)*len(fillers) else []))
            x['utterance']=f"{base} {suffix}"
    meta=[]
    for i,b in enumerate(structured[:40]):
        for name,fn in [('LOWER',str.lower),('POLITE',lambda s:'пожалуйста '+s),('PUNCT',lambda s:s.replace(',',' —').replace('.','!'))]:
            x=dict(b); x['id']=f'meta-{i}-{name}'; x['source']='metamorphic'; x['style']=name; x['utterance']=fn(b['utterance']); x['tags']=b['tags']+['METAMORPHIC',name]; meta.append(x)
    seed=event_case(7777,'seed','REALISTIC_CLEAN',True); seed['id']='seed-known-production-like'; seed['utterance']='созвон с ариадной в 18:00 завтра на пол часа.\nНапомни за 50 минут до начала'; seed['intent']={'kind':'EVENT','subject':'созвон с Ариадной','category':'MEETING','date':'2026-09-30','time':'18:00','duration_minutes':30,'remind_before_minutes':50,'deadline':None}; seed['expected']=evexp(['созвон','ариадн'],'MEETING',date(2026,9,30),time(18),30,50); seed['tags']+=['PERMANENT_REGRESSION_SEED']
    ambiguous=[]; amb=['созвон завтра вечером','позвонить маме в 7','созвон часов в 6','до завтра сделать лабу','напомни перед встречей','врач завтра днем','пни заранее перед созвоном','на следующей неделе наверное курсовая']
    for i in range(80): ambiguous.append({'id':f'amb-{i:03d}','utterance':(['','слушай ','эм ','пожалуйста '][(i//8)%4]+amb[i%8]),'ambiguity':'AMBIGUOUS','must_preserve':{'surface_fact':amb[i%8]},'must_not_invent':['exact_minutes','unsupported_exact_date_or_time'],'acceptable_behaviors':['unresolved_field','clarification','conservative_interpretation']})
    return seed,structured,human,meta,ambiguous

def js(root,cases):
    p=subprocess.run(['node',str(root/'tests/adversarial_capture/run_js_batch.mjs')],input=json.dumps({'now':NOW.isoformat(),'cases':[{'id':c['id'],'utterance':c['utterance']} for c in cases]},ensure_ascii=False),text=True,capture_output=True,env={**os.environ,'TZ':Z},timeout=180)
    if p.returncode: raise RuntimeError(p.stderr)
    return json.loads(p.stdout)

def proj(p):
    k=p.get('kind') or 'TASKLIKE'; c=p.get('actual_cutoff') or {}
    if k=='EVENT': return (k,norm(p.get('title','')),p.get('category'),iso(p.get('starts_at')),iso(p.get('ends_at')),p.get('duration_minutes'),p.get('remind_before_minutes'))
    if k=='REMINDER': return (k,norm(p.get('title','')),iso(p.get('remind_at')))
    return (k,norm(p.get('title','')),p.get('category'),p.get('estimated_total_effort_minutes'),c.get('state'),iso(c.get('at')),iso(p.get('remind_at')),iso(p.get('actionable_from')),iso(p.get('target_at')))
def cmp(c,py,j):
    e=c['expected']; a=j.get('parsed') or {}; k=j.get('effective_kind') or a.get('kind') or 'TASK'; f={'kind':k==e['kind']}; toks=e.get('title_tokens',[])
    if e['kind']!='NOTE': f['title']=all(norm(t) in norm(a.get('title','')) for t in toks)
    if e['kind']=='EVENT': f|={'category':k=='EVENT' and a.get('category')==e['category'],'date_time':k=='EVENT' and iso(a.get('starts_at'))==e['starts'],'duration':k=='EVENT' and a.get('duration_minutes')==e['duration'],'reminder':(k=='EVENT' and a.get('remind_before_minutes')==e['lead']) if e['lead'] is not None else a.get('remind_before_minutes') in (None,''),'deadline':(a.get('actual_cutoff') or {}).get('state')!='KNOWN'}
    elif e['kind']=='TASK':
        co=a.get('actual_cutoff') or {}; f|={'deadline':co.get('state')=='KNOWN' and iso(co.get('at'))==e['deadline'],'effort':a.get('estimated_total_effort_minutes')==e['effort'],'reminder':iso(a.get('remind_at'))==e['remind'] if e['remind'] else not a.get('remind_at'),'category':a.get('category')==e['category']}
    elif e['kind']=='REMINDER': f['reminder']=k=='REMINDER' and iso(a.get('remind_at'))==e['remind']
    passed=all(f.values()); cl=None
    if not passed:
        if 'CORRECTION' in c['tags'] and any(not f.get(x,True) for x in ['date_time','duration','reminder']): cl='SELF_CORRECTION_IGNORED'
        elif e['kind']=='EVENT' and k=='TASK': cl='EVENT_WITH_REMINDER_BECOMES_TASK' if e.get('lead') is not None else 'EVENT_BECOMES_TASK'
        elif e['kind']=='EVENT' and k=='REMINDER': cl='EVENT_BECOMES_REMINDER'
        elif e['kind']=='EVENT' and k=='EVENT' and not f.get('date_time',True): cl='EVENT_START_WRONG_OR_LOST'
        elif e['kind']=='EVENT' and k=='EVENT' and e.get('lead') is not None and not f.get('reminder',True): cl='REMINDER_OFFSET_LOST'
        elif e['kind']=='EVENT' and not f.get('duration',True): cl='DURATION_LOST_OR_WRONG'
        elif e['kind']=='TASK' and (not f.get('deadline',True) or not f.get('reminder',True)): cl='DEADLINE_REMINDER_CONFUSION'
        elif e['kind']=='REMINDER': cl='REMINDER_TIME_OR_KIND_WRONG'
        elif e['kind']=='NOTE': cl='NOTE_KIND_WRONG'
        else: cl='OTHER_SEMANTIC_MISMATCH'
    sev='P1' if cl in {'SELF_CORRECTION_IGNORED','EVENT_WITH_REMINDER_BECOMES_TASK','EVENT_BECOMES_TASK','EVENT_BECOMES_REMINDER','EVENT_START_WRONG_OR_LOST','REMINDER_OFFSET_LOST','DEADLINE_REMINDER_CONFUSION','REMINDER_TIME_OR_KIND_WRONG'} else ('P2' if cl else None)
    return {'id':c['id'],'pass':passed,'fields':f,'cluster':cl,'severity':sev,'expected':e,'actual_python':py,'actual_js':a,'actual_effective_kind':k,'js_python_agree':proj(py)==proj(a)}
def run(root,cases):
    pys={c['id']:parse_task(c['utterance'],now=NOW,timezone_name=Z) for c in cases}; jss={x['id']:x for x in js(root,cases)}; return [cmp(c,pys[c['id']],jss[c['id']]) for c in cases]
def adaptive(cases,results):
    by={c['id']:c for c in cases}; groups=defaultdict(list)
    for r in results:
        if r['cluster'] and r['severity']=='P1': groups[r['cluster']].append(r)
    funcs=[
        lambda s:s.lower(),
        lambda s:'слушай '+s,
        lambda s:'пожалуйста '+s,
        lambda s:s.replace(',',''),
        lambda s:s.replace('. ','\n'),
        lambda s:s.replace(' минут',' мин'),
        lambda s:'короче '+s,
        lambda s:s+' пожалуйста',
        lambda s:re.sub(r'(\\d{1,2}):(\\d{2})',r'\\1.\\2',s),
        lambda s:s.replace('ё','е'),
    ]
    out=[]
    for cl,rs in groups.items():
        bases=[]
        base_seen=set()
        for r in sorted(rs,key=lambda x:len(by[x['id']]['utterance'])):
            b=by[r['id']]
            if b['utterance'] not in base_seen:
                base_seen.add(b['utterance']); bases.append(b)
        seen=set(); n=0; i=0
        while n<50 and bases:
            b=bases[i%len(bases)]
            u=funcs[(i//len(bases))%len(funcs)](b['utterance'])
            if u not in seen and u != b['utterance']:
                seen.add(u); x=dict(b); x['id']=f'adaptive-{cl.lower()}-{n:03d}'; x['source']='adaptive'; x['style']='ADAPTIVE_HUMAN'; x['utterance']=u; x['tags']=b['tags']+['ADAPTIVE',cl]; out.append(x); n+=1
            i+=1
            if i > len(bases)*len(funcs)*3: break
    return out

def main():
    root=Path(__file__).resolve().parents[1]; out=root/'artifacts/capture-adversarial'; out.mkdir(parents=True,exist_ok=True)
    seed,structured,human,meta,amb=build()
    distinct_structured=len({json.dumps(x['intent'],ensure_ascii=False,sort_keys=True) for x in structured})
    unique_human=len({x['utterance'] for x in human})
    if distinct_structured < 400: raise RuntimeError(f'structured corpus has only {distinct_structured} distinct semantic intents')
    if unique_human < 800: raise RuntimeError(f'human corpus has only {unique_human} unique utterances')
    wide=[seed,*structured,*human,*meta]; res=run(root,wide); ad=adaptive(wide,res); ar=run(root,ad) if ad else []; cases=wide+ad; res+=ar; by={c['id']:c for c in cases}; fail=[{**r,'utterance':by[r['id']]['utterance'],'source':by[r['id']]['source'],'style':by[r['id']]['style'],'tags':by[r['id']]['tags']} for r in res if not r['pass']]
    for name,rows in [('seed_regressions',[seed]),('structured_phrases',structured),('human_phrases',human),('ambiguous_phrases',amb),('metamorphic_cases',meta),('adaptive_cases',ad),('failures',fail),('intents',[{'id':c['id'],'intent':c['intent'],'expected':c['expected'],'source':c['source']} for c in cases])]: wr(out/f'{name}.jsonl',rows)
    grp=Counter(r['cluster'] for r in res if r['cluster']); sev={cl:next(r['severity'] for r in res if r['cluster']==cl) for cl in grp}; sc=Counter(sev.values()); agr=sum(r['js_python_agree'] for r in res); both=sum(r['js_python_agree'] and not r['pass'] for r in res); fields=defaultdict(list)
    for r in res:
        for k,v in r['fields'].items(): fields[k].append(v)
    probe=json.loads(subprocess.run(['node',str(root/'tests/adversarial_capture/ui_payload_probe.mjs')],text=True,capture_output=True,env={**os.environ,'TZ':Z},check=True).stdout); probe['backend_accepts_50']=extras.parse_lead(50)==50
    summ={'baseline_sha':BASE,'baseline_tree':TREE,'release':'v0.6.2','timezone':Z,'total_strict_cases':len(cases),'wide_strict_cases':len(wide),'structured_cases':len(structured),'distinct_structured_intents':distinct_structured,'human_cases':len(human),'unique_human_utterances':unique_human,'human_style_counts':dict(Counter(x['style'] for x in human)),'adaptive_human_cases':len(ad),'ambiguous_cases':len(amb),'metamorphic_cases':len(meta),'failure_cases':len(fail),'semantic_accuracy':round(1-len(fail)/len(res),4),'human_accuracy':round(sum(r['pass'] for r in res if by[r['id']]['source'] in {'human','adaptive'})/sum(1 for r in res if by[r['id']]['source'] in {'human','adaptive'}),4),'field_accuracy':{k:round(sum(v)/len(v),4) for k,v in fields.items()},'clusters':dict(grp),'cluster_severity_counts':dict(sc),'js_python_agreement':agr,'js_python_disagreement':len(res)-agr,'js_python_agreement_but_both_wrong':both,'ui_api_probe':probe,'remaining_gaps':['second independent semantic verifier / manual realism adjudication; workflow emits mutation-testing.json, property-testing.json and ui-api-e2e.json separately']}
    (out/'capture-adversarial-summary.json').write_text(json.dumps(summ,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    tops=[]; top_seen=set()
    for x in sorted((z for z in fail if z['source'] != 'adaptive'),key=lambda x:(int((x['severity'] or 'P9')[1]),len(x['utterance']))):
        if x['utterance'] in top_seen: continue
        top_seen.add(x['utterance']); tops.append(x)
        if len(tops) >= 30: break
    lines=['# Capture adversarial report','','## Executive summary',f'- Baseline: `{BASE}`',f'- Strict cases: **{len(cases)}**',f'- Human-like: **{len(human)+len(ad)}**',f'- Failures: **{len(fail)}**',f'- Clusters: **{len(grp)}**',f'- P0/P1/P2/P3 clusters: **{dict(sc)}**',f'- JS==Python but both wrong: **{both}**','','## Confirmed structural findings','- Non-range `_event` rejects any `remind_spans`, so EVENT+reminder is structurally disqualified before event construction.','- Event UI lead presets omit 50, while event payload/backend accept 50.','','## Clusters']
    for cl,n in grp.most_common(): lines+=['',f'### {sev[cl]} — {cl} ({n})',f'- Minimal observed: `{min((x["utterance"] for x in fail if x["cluster"]==cl),key=len).replace(chr(10)," / ")}`']
    lines+=['','## Top failing phrases']+[f'- **{x["severity"]} {x["cluster"]}** — `{x["utterance"].replace(chr(10)," / ")}`' for x in tops]+['','## Recommended v0.6.3 scope','1. Preserve EVENT identity when reminder language is present; assign event start/duration/reminder as separate roles.','2. Carry arbitrary event reminder offsets end-to-end; do not coerce 50 to presets.','3. Make the seed a permanent independent-oracle regression.','4. Define correction policy and repair deadline/reminder role confusion shown by clusters.','','## Remaining first-phase gaps','- Targeted mutation runner not yet executed.','- Full rendered Playwright preview -> Create -> persisted entity E2E not yet added.','- Human corpus is deterministic and diverse but still needs manual/second-verifier realism spot-check.','']; (out/'capture-adversarial-report.md').write_text('\n'.join(lines),encoding='utf8')
    print(json.dumps({'strict_cases':len(cases),'human_like':len(human)+len(ad),'ambiguous':len(amb),'failures':len(fail),'clusters':len(grp),'severity':dict(sc),'js_python_agreement_but_both_wrong':both,'report':str(out/'capture-adversarial-report.md')},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
