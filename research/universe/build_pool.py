"""建立 ta_pool（策略規格 v0.9 §16.2）。
用法：python build_pool.py [--raw DIR] [--px DIR] [--out DIR] [--strategy FILE] [--risk FILE] [--nlv HKD] [--asof YYYY-MM-DD]
預設沿用 research/opportunity_ta 的來源快取和價格快取；每月第一個營業日由 fetch_sources.py 先更新快取再跑。"""
import os, sys, json, argparse, datetime as dt
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ta_pool as T
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument('--raw', default=os.path.join(ROOT, 'research/opportunity_ta/data/raw'))
    a.add_argument('--px', default=os.path.join(ROOT, 'research/opportunity_ta/data/px'))
    a.add_argument('--out', default=os.path.join(ROOT, 'data/universe'))
    a.add_argument('--strategy', default=os.path.join(ROOT, 'strategies/approved/ta_breakout52.v1.json'))
    a.add_argument('--risk', default=os.path.join(ROOT, 'config/risk_limits_expected.json'))
    a.add_argument('--nlv', type=float, default=7.8e6, help='NLV（HKD），用於 min(R1×NLV, R2)')
    a.add_argument('--asof', default=None, help='預設 = 各市場價格數據的最後日期中最晚者')
    a.add_argument('--pool-month', default=None, help='YYYYMM；預設 = 執行時的澳門月份（每月第一個營業日重建，所以是「刷新月份」，不是 asof 月份）')
    o = a.parse_args(argv)
    strat = json.load(open(o.strategy, encoding='utf-8')); filters = strat['universe']['filters']
    assert strat['universe']['source'] == 'ta_pool' and filters['max_lot_cost'] == 'order_cap'
    risk = json.load(open(o.risk)); lim = risk.get('limits', risk)
    cap = T.order_cap(o.nlv, lim['R1_order_size_pct_nlv'], lim['R2_max_order_notional_base'])
    u = T.load_constituents(o.raw)
    fx = T.fx_to_hkd(o.px)
    px = {m: pd.read_parquet(os.path.join(o.px, f'{m}.parquet')) for m in T.MKTS}
    asof = pd.Timestamp(o.asof) if o.asof else max(df.date.max() for df in px.values())
    metrics, last = {}, {}
    for m in T.MKTS: metrics[m], last[m] = T.market_metrics(px[m], fx[T.CCY[m]], asof)
    asof_s = asof.strftime('%Y-%m-%d'); pool_id = 'ta_pool_' + (o.pool_month or dt.datetime.now(T.TZ).strftime('%Y%m'))
    assert len(pool_id) == 14 and pool_id[8:].isdigit()
    df = T.apply_filters(u, metrics, filters, cap, asof_s)
    b = T.to_csv_bytes(df); ph = T.sha256_bytes(b)
    cnt = lambda s: {m: int(s[s.market == m].shape[0]) for m in T.MKTS} | {'total': int(len(s))}
    mt = lambda p: dt.datetime.fromtimestamp(os.path.getmtime(p), T.TZ).isoformat(timespec='seconds')
    meta = dict(schema_version='ta_pool_meta/1.0', pool_id=pool_id, asof=asof_s,
                generated_at=dt.datetime.now(T.TZ).isoformat(timespec='seconds'),
                strategy=dict(file=os.path.relpath(o.strategy, ROOT), id=strat.get('id', strat.get('strategy_id')), version=strat.get('version'), spec_hash=T.spec_hash(strat)),
                filters=filters, filters_hash=T.canonical_hash(filters),
                order_cap=dict(nlv_base=o.nlv, r1_order_size_pct_nlv=lim['R1_order_size_pct_nlv'], r2_max_order_notional_base=lim['R2_max_order_notional_base'], cap_base=cap,
                               lot_price_buffer_pct=filters['lot_price_buffer_pct'], base_currency='HKD'),
                sources=[dict(name=n, url=url, file=f, fetched_at=mt(os.path.join(o.raw, f)), sha256=T.sha256_file(os.path.join(o.raw, f))) for f, (n, url) in T.SOURCES.items()],
                price_data=dict(source='Yahoo Finance via yfinance (unadjusted close, volume)', last_session={m: last[m].strftime('%Y-%m-%d') for m in T.MKTS},
                                files={m: dict(file=os.path.relpath(os.path.join(o.px, f'{m}.parquet'), ROOT), sha256=T.sha256_file(os.path.join(o.px, f'{m}.parquet'))) for m in T.MKTS + ['FX']}),
                csv=dict(file='data/universe/ta_pool.csv', archive=f'data/universe/archive/{pool_id}.csv', columns=T.COLUMNS,
                         format='utf-8 no BOM; LF; comma; header; rows sorted by market (US,JP,HK,TW,KR) then ticker ASCII; close_local %.4f; lot_cost_base/adv60_base %.2f HKD; bool true/false; empty = missing'),
                pool_hash=ph, pool_hash_algorithm='sha256 of the CSV file bytes; "sha256:" + lowercase hex',
                counts=dict(base=cnt(df), in_pool=cnt(df[df.in_pool])),
                excluded_reason_counts={r: int(df.excluded_reason.str.split(';').apply(lambda x: r in x).sum()) for r in T.REASONS},
                generator='research/universe/build_pool.py')
    arc = T.write_outputs(o.out, pool_id, b, meta)
    print(json.dumps(dict(pool_id=pool_id, asof=asof_s, archive=arc, pool_hash=ph, counts=meta['counts'], excluded=meta['excluded_reason_counts'], filters_hash=meta['filters_hash']), ensure_ascii=False, indent=1))
if __name__ == '__main__': main()
