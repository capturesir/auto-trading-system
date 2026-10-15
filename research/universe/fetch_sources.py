"""每月刷新快取（第一個營業日、澳門 06:00 前，先跑本檔再跑 build_pool.py）：
1) 成分來源（ta_pool.SOURCES 的 URL）→ <cache>/<YYYYMM>/raw/
2) yfinance 日線（未還原 close、volume；約 500 個日曆日，足夠 260 session 和 adv60）→ <cache>/<YYYYMM>/px/<market>.parquet（與 research/opportunity_ta/data/px 同一長表格式）
用法：python fetch_sources.py [--cache DIR]；然後 python build_pool.py --raw <cache>/<YYYYMM>/raw --px <cache>/<YYYYMM>/px
注意：2026-10 這一期沒有跑本檔，直接用 research/opportunity_ta 的 2026-10-06 快取（見 README）。"""
import os, sys, time, argparse, datetime as dt, urllib.request
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ta_pool as T
UA = {'User-Agent': 'Mozilla/5.0 (research; ta_pool builder)', 'Accept': '*/*'}
def get(url, path):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r: data = r.read()
    T.atomic_write(path, data); return len(data)
def fetch_prices(tickers, start, end):
    import yfinance as yf
    out = []
    for i in range(0, len(tickers), 40):
        tks = tickers[i:i + 40]
        df = yf.download(tks, start=start, end=end, auto_adjust=False, actions=False, group_by='ticker', threads=True, progress=False)
        for t in tks:
            try: d = (df[t] if len(tks) > 1 else df).dropna(how='all')
            except KeyError: continue
            if len(d) == 0: continue
            d = d.rename(columns=str.lower).rename(columns={'adj close': 'adj'}); d['ticker'] = t; d.index.name = 'date'
            out.append(d.reset_index()[['date', 'ticker', 'open', 'high', 'low', 'close', 'adj', 'volume']])
        time.sleep(3)
    return pd.concat(out)
def main(argv=None):
    a = argparse.ArgumentParser(); a.add_argument('--cache', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache'))
    o = a.parse_args(argv); today = dt.datetime.now(T.TZ).date(); d = os.path.join(o.cache, today.strftime('%Y%m'))
    raw, px = os.path.join(d, 'raw'), os.path.join(d, 'px'); os.makedirs(raw, exist_ok=True); os.makedirs(px, exist_ok=True)
    for f, (_, url) in T.SOURCES.items(): print(f, get(url, os.path.join(raw, f)))
    u = T.load_constituents(raw); start = (today - dt.timedelta(days=500)).isoformat(); end = today.isoformat()
    fetch_prices(['HKD=X', 'JPY=X', 'TWD=X', 'KRW=X'], start, end).to_parquet(os.path.join(px, 'FX.parquet'), index=False)
    for m in T.MKTS: fetch_prices(u[u.market == m].ticker.tolist(), start, end).to_parquet(os.path.join(px, f'{m}.parquet'), index=False); print(m, 'ok')
if __name__ == '__main__': main()
