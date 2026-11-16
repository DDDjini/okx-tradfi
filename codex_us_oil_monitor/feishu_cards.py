"""Feishu interactive cards; prices are strategy references, not execution receipts."""
from datetime import datetime,timezone,timedelta

def md(text):return {'tag':'div','text':{'tag':'lark_md','content':text}}
def fields(items):
    return {'tag':'div','fields':[{'is_short':True,'text':{'tag':'lark_md','content':f'**{label}**\n{value}'}} for label,value in items]}
def card(title,color,elements):
    return {'config':{'wide_screen_mode':True},'header':{'template':color,'title':{'tag':'plain_text','content':title}},'elements':elements}
def cn_time(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
def signal_card(row,asset):
    symbol=row['instrument'].removesuffix('-USDT-SWAP');side='做多 ↑' if row['side']>0 else '做空 ↓'
    price=lambda v:f'{v:,.4f}'.rstrip('0').rstrip('.')
    evidence=asset.get('okx_backtest',{})
    stat=f"{evidence['rate']:.1%} · {evidence['tp1']}/{evidence['decided']} 次" if evidence.get('rate') is not None else '暂无欧意统计'
    outside=row['side']*(row.get('reference',row['mid'])-row['tp1'])>=0
    body=[md(f"**{side}　｜　4H 确认 · UTC 日线结构**"),
          fields([('MID 参考价',price(row['mid'])),('参考入场区间',f"{price(row['low'])} — {price(row['high'])}")]),
          {'tag':'hr'},fields([('🛑 止损 SL',price(row['sl'])),('🎯 第一目标 TP1',price(row['tp1'])),
                             ('第二目标 TP2',price(row['tp2'])),('单信号名义仓位上限','净值的 15%')]),
          {'tag':'hr'},fields([('信号确认 · 北京时间',cn_time(row['ready'])),('历史 TP1 先触率',stat)])]
    if outside:body.append(md('⚠️ **确认价已越过 TP1：该信号仅供观察，不应按已到达的目标追单。**'))
    body.append({'tag':'action','actions':[{'tag':'button','text':{'tag':'plain_text','content':'查看欧意合约 ↗'},'type':'primary',
                                        'url':'https://www.okx.com/trade-swap/'+row['instrument'].lower()}]})
    body.append({'tag':'note','elements':[{'tag':'plain_text','content':'价格单位 USDT · MID 为策略锚点，未验证挂单成交；历史先触率不等于实盘收益率。'}]})
    return card(f'S28 交易信号 · {symbol}', 'green' if row['side']>0 else 'red',body)

def summary_card(report):
    errors=report['errors'];n=len(report['new_signals'])
    title='扫描异常 · 请检查' if errors else f'新增 {n} 条交易信号' if n else '本轮没有新信号触发'
    elements=[md('**欧意美股与原油监控　｜　每 4 小时**'),
      fields([('正常合约',str(len(report['health']))),('新增信号',str(n)),('扫描异常',str(len(errors))),('北京时间',cn_time(report['asof']))])]
    if report['unsupported']:elements.append(md('未上架 / 暂不可用：'+', '.join(report['unsupported'])))
    if errors:elements.append(md('\n'.join(f"• {e['symbol']}：{e.get('reason',e['error_type'])}" for e in errors)))
    elements.append({'tag':'action','actions':[{'tag':'button','text':{'tag':'plain_text','content':'查看扫描运行记录 ↗'},'type':'default',
      'url':'https://github.com/DDDjini/okx-tradfi/actions/workflows/s28-us-oil.yml'}]})
    return card('S28 · '+title,'orange' if errors else 'blue',elements)
