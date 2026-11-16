"""Closed-session S-28 monitoring, with durable notification receipts."""
import argparse
import json
import os
import base64
import hashlib
import hmac
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
from engine import generate, iso
from market_data import CACHE, COLS

ROOT = Path(__file__).resolve().parent


def request(url, **kwargs):
    response = requests.get(url, timeout=40, **kwargs)
    response.raise_for_status()
    return response.json()


def save(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)


def run(send=False, only=None):
    now=datetime.now(timezone.utc)
    universe=json.loads((ROOT/'universe.json').read_text(encoding='utf-8'))['assets']
    report={'asof':now.isoformat(),'provider':'OKX','signals':[],'health':[],'errors':[],'unsupported':[]}
    state_file=ROOT/'state'/'notifications.json'
    state=json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {}
    try:
        instruments=okx('/api/v5/public/instruments',{'instType':'SWAP'})
        available={x['instId'] for x in instruments if x['state']=='live'}
    except Exception as exc:
        available=set()
        report['errors'].append({'symbol':'instruments','error_type':type(exc).__name__})
    for asset in universe:
        symbol=asset['symbol']
        if only and symbol not in only:
            continue
        instrument=('CL' if symbol=='CL=F' else symbol)+'-USDT-SWAP'
        if instrument not in available:
            report['unsupported'].append(symbol)
            continue
        try:
            daily=candles(instrument,'1Dutc',86400,now)
            four=candles(instrument,'4H',14400,now)
            if len(daily)<75 or len(four)<75:
                raise ValueError('Insufficient history')
            if four.end[-1]<int(now.timestamp())//14400*14400:
                raise ValueError('Latest closed 4H missing')
            if daily.end[-1]<int(now.timestamp())//86400*86400:
                raise ValueError('Latest closed daily missing')
            if any(four.t[1:]!=four.end[:-1]) or any(daily.t[1:]!=daily.end[:-1]):
                raise ValueError('Candle history contains gaps')
            report['health'].append({'symbol':symbol,'instrument':instrument,'daily_bars':len(daily),'four_hour_bars':len(four),'end':iso(four.end[-1])})
            for signal in generate(instrument,daily,four,overlap=False,confirm_seconds=14400):
                if signal.ready<now.timestamp()-48*3600:
                    continue
                sl,tp1,tp2=signal.levels
                key='okx-v1/'+signal.id+'/'+str(signal.ready)
                row={'key':key,'instrument':instrument,'ready':iso(signal.ready),'side':signal.side,
                     'low':signal.zone[0],'high':signal.zone[1],'mid':signal.mid,'sl':sl,'tp1':tp1,'tp2':tp2}
                report['signals'].append(row)
                if send and key not in state:
                    notify(f"S-28 OKX {instrument} {'多' if signal.side>0 else '空'}\n确认 {iso(signal.ready)} UTC\n区间 {signal.zone[0]:.4f}–{signal.zone[1]:.4f}\nMID {signal.mid:.4f}\nSL {sl:.4f} · TP1 {tp1:.4f} · TP2 {tp2:.4f}\n{key}")
                    state[key]={'sent_at':now.isoformat()}
                    save(state_file,state)
        except Exception as exc:
            report['errors'].append({'symbol':symbol,'error_type':type(exc).__name__,'reason':str(exc) if isinstance(exc,ValueError) else 'market or notification request failed'})
    save(ROOT/'scan_output.json',report)
    if send:
        status='本轮没有信号触发' if not report['signals'] and not report['errors'] else '扫描存在异常' if report['errors'] else f"本轮信号 {len(report['signals'])} 条（已通知信号去重）"
        notify(f"S-28 欧意扫描完成\n{status}\n正常标的 {len(report['health'])} · 异常 {len(report['errors'])}\n未上架 {', '.join(report['unsupported']) or '无'}\n时间 {now.astimezone(timezone(timedelta(hours=8))).isoformat()}"+('\n异常 '+json.dumps(report['errors'],ensure_ascii=False) if report['errors'] else ''))
    print(json.dumps({'healthy':len(report['health']),'signals':len(report['signals']),'errors':report['errors'],'unsupported':report['unsupported']},ensure_ascii=False))
    return int(bool(report['errors']))


def okx(path,params):
    result=request('https://www.okx.com'+path,params=params)
    if result.get('code')!='0':
        raise ValueError('OKX API error '+str(result.get('code')))
    return result['data']


def candles(instrument,bar,seconds,now):
    from engine import Bars
    rows={}
    after=None
    for page in range(4):
        params={'instId':instrument,'bar':bar,'limit':100}
        if after:
            params['after']=after
        data=okx('/api/v5/market/history-candles',params)
        if not data:
            break
        for row in data:
            t=int(row[0])//1000
            if row[8]=='1' and t+seconds<=now.timestamp():
                rows[t]=(t,*map(float,row[1:6]))
        cursor=min(int(x[0]) for x in data)
        if after is not None and cursor>=int(after):
            raise ValueError('Pagination did not advance')
        after=str(cursor)
    return Bars.from_frame(pd.DataFrame([rows[t] for t in sorted(rows)],columns=['t',*COLS]),seconds)


def notify(text):
    url=os.environ['FEISHU_WEBHOOK']
    if not url.startswith('https://open.feishu.cn/open-apis/bot/v2/hook/'):
        raise ValueError('Invalid webhook host')
    payload={'msg_type':'text','content':{'text':text}}
    secret=os.environ.get('FEISHU_SIGN_SECRET')
    if secret:
        stamp=str(int(time.time()))
        payload.update(timestamp=stamp,sign=base64.b64encode(hmac.new((stamp+'\n'+secret).encode(),b'',hashlib.sha256).digest()).decode())
    r=requests.post(url,json=payload,timeout=20)
    r.raise_for_status()
    result=r.json()
    if result.get('code',result.get('StatusCode'))!=0:
        raise ValueError('Feishu rejected notification')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--send',action='store_true')
    parser.add_argument('--symbols',nargs='+')
    args=parser.parse_args()
    raise SystemExit(run(args.send,args.symbols))
