import json
from pathlib import Path
from feishu_cards import card,md,fields
from datetime import datetime,timezone,timedelta
import requests

def resolve_minute(instrument,start,end,side,target,stop):
    rows={};after=end*1000
    for _ in range(4):
        response=requests.get('https://www.okx.com/api/v5/market/history-candles',params={
            'instId':instrument,'bar':'1m','after':str(after),'limit':100},timeout=40)
        response.raise_for_status();data=response.json()
        if data.get('code')!='0':raise ValueError('Minute history unavailable')
        batch=data['data']
        if not batch:break
        for row in batch:
            t=int(row[0])//1000
            if row[8]=='1' and start<=t<end:rows[t]=list(map(float,row[1:5]))
        cursor=min(int(x[0]) for x in batch)
        if cursor>=after:raise ValueError('Minute pagination did not advance')
        after=cursor
        if cursor//1000<=start:break
    if sorted(rows)!=list(range(start,end,60)):return 'AMBIGUOUS'
    for t,(o,h,l,c) in sorted(rows.items()):
        if side*(o-target)>=0:return 'TP'
        if side*(o-stop)<=0:return 'SL'
        win=h>=target if side>0 else l<=target
        loss=l<=stop if side>0 else h>=stop
        if win or loss:return 'AMBIGUOUS' if win and loss else 'TP' if win else 'SL'
    return 'AMBIGUOUS'

def update(root,instrument,signals,bars,receipts,notify,save,send):
    path=root/'state'/'outcomes.json'
    ledger=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    records=ledger.setdefault(instrument,{})
    for s in signals:
        key='okx-v1/'+s.id+'/'+str(s.ready)
        records.setdefault(key,{'ready':s.ready,'side':s.side,'sl':s.levels[0],
            'tp1':s.levels[1],'tp2':s.levels[2],'results':{},'announced':[]})
    for key,r in records.items():
        for target in ('tp1','tp2'):
            if target not in r['results']:
                expected=r['ready']
                for i,t in enumerate(bars.t):
                    if t<r['ready']:continue
                    if t!=expected:break
                    expected=int(bars.end[i]);side=r['side']
                    win=bars.h[i]>=r[target] if side>0 else bars.l[i]<=r[target]
                    loss=bars.l[i]<=r['sl'] if side>0 else bars.h[i]>=r['sl']
                    if not(win or loss):continue
                    if side*(bars.o[i]-r[target])>=0:result='TP'
                    elif side*(bars.o[i]-r['sl'])<=0:result='SL'
                    else:result='AMBIGUOUS' if win and loss else 'TP' if win else 'SL'
                    if result=='AMBIGUOUS':result=resolve_minute(instrument,int(t),int(bars.end[i]),side,r[target],r['sl'])
                    r['results'][target]={'result':result,'bar_start':int(t),'bar_end':int(bars.end[i])};break
            if send and key in receipts and target in r['results'] and target not in r['announced']:
                result=r['results'][target]['result']
                title=f"价格先触 {target.upper()}" if result=='TP' else f"{target.upper()} 路径先触止损" if result=='SL' else f"{target.upper()} 同柱顺序待确认"
                hit=r['results'][target]
                clock=lambda t:datetime.fromtimestamp(t,timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
                notify(card(instrument+' · '+title,'green' if result=='TP' else 'orange',[
                    md('**已通知信号的价格路径更新**'),fields([('目标价',str(r[target])),('原始止损',str(r['sl']))]),
                    md('触及所在4H区间（北京）：'+clock(hit['bar_start'])+' — '+clock(hit['bar_end'])),
                    md('独立比较该目标与原始止损；不代表账户已成交或已实现收益。'),md(key)]))
                r['announced'].append(target)
                save(path,ledger)
    stats={}
    for target in ('tp1','tp2'):
        decided=[r['results'][target] for r in records.values() if target in r['results']]
        wins=sum(x['result']=='TP' for x in decided)
        stats[target]={'tp':wins,'n':len(decided),'rate':wins/len(decided) if decided else None,
                       'ambiguous':sum(x['result']=='AMBIGUOUS' for x in decided),'open':len(records)-len(decided)}
    if send:save(path,ledger)
    return stats
