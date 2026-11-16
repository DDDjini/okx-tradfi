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
import exchange_calendars as xcals
import extended_data as data
from engine import generate, iso
from market_data import CACHE, COLS

ROOT = Path(__file__).resolve().parent


def request(url, **kwargs):
    response = requests.get(url, timeout=40, **kwargs)
    response.raise_for_status()
    return response.json()


def yahoo(symbol, interval):
    result = request(f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}',
                     headers={'User-Agent':'Mozilla/5.0'}, params={'interval':interval,
                     'range':'730d' if interval=='60m' else '5y', 'includePrePost':'false','events':'splits'})
    return result['chart']['result'][0]


def sip(symbol, now):
    headers = {'APCA-API-KEY-ID':os.environ['APCA_API_KEY_ID'],
               'APCA-API-SECRET-KEY':os.environ['APCA_API_SECRET_KEY']}
    start = now-timedelta(days=730)
    params = {'timeframe':'30Min','start':start.isoformat(),'end':now.isoformat(),
              'feed':'sip','adjustment':'raw','limit':10000,'sort':'asc'}
    rows = []
    while True:
        result = request(f'https://data.alpaca.markets/v2/stocks/{symbol}/bars',headers=headers,params=params)
        rows.extend((int(pd.Timestamp(b['t']).timestamp()),b['o'],b['h'],b['l'],b['c'],b['v']) for b in result.get('bars',[]))
        token = result.get('next_page_token')
        if not token:
            break
        params['page_token'] = token
    frame = pd.DataFrame(rows,columns=['t',*COLS])
    if frame.empty or frame.t.duplicated().any():
        raise ValueError('Missing or duplicate SIP bars')
    for year in range(2022,now.year+1):
        subset=frame[pd.to_datetime(frame.t,unit='s',utc=True).dt.year==year]
        subset.to_csv(CACHE/f'alpaca-sip-{symbol}-{year}-30m.csv',index=False)


def save(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)


def run(send=False, only=None):
    now=datetime.now(timezone.utc)
    data.CUTOFF=int(now.timestamp())
    data.yahoo_data=yahoo
    calendar=xcals.get_calendar('XNYS',start=(now-timedelta(days=1900)).date(),end=now.date()).schedule
    completed=calendar[calendar['close']<=pd.Timestamp(now)]
    expected=int(completed.iloc[-1]['close'].timestamp())
    state_file=ROOT/'state'/'notifications.json'
    state=json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {}
    report={'asof':now.isoformat(),'signals':[],'health':[],'errors':[]}
    assets=json.loads((ROOT/'universe.json').read_text(encoding='utf-8'))['assets']
    for asset in assets:
        symbol=asset['symbol']
        if only and symbol not in only:
            continue
        try:
            if symbol=='CL=F':
                fine,setup,confirm,gaps,health=data.futures_market(symbol)
                options={'confirm_seconds':14400,'overlap':False}
                if fine.end[-1]<now.timestamp()-4*86400:
                    raise ValueError('Stale oil sessions')
            else:
                provider=asset['venue']
                if provider=='Alpaca SIP':
                    sip(symbol,now)
                fine,setup,confirm,gaps,health=data.regular_market(symbol,calendar,provider)
                options={'rth':True,'long_only':False,'overlap':False}
                if fine.end[-1]<expected:
                    raise ValueError('Latest completed exchange session missing')
            report['health'].append(health)
            signals=generate(symbol,setup,confirm,**options)
            for s in signals:
                if s.ready<now.timestamp()-48*3600 or any(s.setup<=t<s.ready for t in gaps):
                    continue
                sl,tp1,tp2=s.levels
                key=s.id+'/'+str(s.ready)
                row={'key':key,'symbol':symbol,'side':s.side,'confirmed':iso(s.ready),
                     'low':s.zone[0],'high':s.zone[1],'mid':s.mid,'stop':sl,'tp1':tp1,'tp2':tp2,
                     'historical_tp':asset['tp1'],'historical_decided':asset['closed'],'provider':asset['venue']}
                report['signals'].append(row)
                if send and key not in state:
                    url=os.environ['FEISHU_WEBHOOK']
                    if not url.startswith('https://open.feishu.cn/open-apis/bot/v2/hook/'):
                        raise ValueError('Invalid Feishu webhook host')
                    text=f"S-28 {symbol} {'多' if s.side>0 else '空'}\n确认 {iso(s.ready)} UTC\n区间 {s.zone[0]:.4f}–{s.zone[1]:.4f}\nMID {s.mid:.4f}\nSL {sl:.4f} · TP1 {tp1:.4f} · TP2 {tp2:.4f}\n历史先触 {asset['tp1']}/{asset['closed']}，价格路径统计\n信号 ID {key}"
                    if symbol=='CL=F':
                        text+='\n连续期货观察价，交易前需核对实际到期合约。'
                    payload={'msg_type':'text','content':{'text':text}}
                    secret=os.environ.get('FEISHU_SIGN_SECRET')
                    if secret:
                        timestamp=str(int(time.time()))
                        signature=base64.b64encode(hmac.new((timestamp+'\n'+secret).encode(),b'',hashlib.sha256).digest()).decode()
                        payload.update(timestamp=timestamp,sign=signature)
                    response=requests.post(url,json=payload,timeout=20)
                    response.raise_for_status()
                    result=response.json()
                    if result.get('code',result.get('StatusCode'))!=0:
                        raise ValueError('Feishu rejected notification')
                    state[key]={'sent_at':now.isoformat(),'symbol':symbol}
                    save(state_file,state)
        except Exception as exc:
            # Avoid request URLs in logs because notification URLs contain credentials.
            report['errors'].append({'symbol':symbol,'error_type':type(exc).__name__})
    save(ROOT/'scan_output.json',report)
    print(json.dumps({'signals':len(report['signals']),'healthy':len(report['health']),'errors':report['errors']},ensure_ascii=False))
    return int(bool(report['errors']))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--send',action='store_true')
    parser.add_argument('--symbols',nargs='+')
    args=parser.parse_args()
    raise SystemExit(run(args.send,args.symbols))
