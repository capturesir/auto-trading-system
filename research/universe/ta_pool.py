"""ta_pool 股票池產生器（策略規格 v0.9 §16.2；執行規格 v0.8 §3.1 第 5 點的 pool_hash 核對）。

流程：成分（原始來源檔）→ 價格（yfinance 長表 parquet）→ 每檔指標 → 篩選 → 規範化 CSV 位元組 → pool_hash。
pool_hash = "sha256:" + sha256(CSV 檔位元組).hexdigest()；ta_pool.csv 與 archive/ta_pool_<YYYYMM>.csv 逐位元組相同。
CSV 規範化（決定位元組，改動即改雜湊，見 README「CSV 規範」）：
  UTF-8（無 BOM）、LF 行尾、逗號分隔、首行為表頭（欄位順序照 §16.2）、最後一行後有 LF、不加引號（欄位內不含逗號／引號／換行，寫出時檢查）；
  列順序：market 按 US, JP, HK, TW, KR，再按 ticker 的 ASCII 升序；
  數字：close_local 4 位小數、lot_cost_base 與 adv60_base 2 位小數（HKD）、lot_size 與 history_sessions 為整數；缺值為空字串；
  布林：true／false；index_membership 以 '+' 連接（ASCII 升序）；excluded_reason 以 ';' 連接（固定次序），入池為空字串。
"""
import io, os, re, json, hashlib, datetime as dt
import numpy as np, pandas as pd

MKTS = ['US', 'JP', 'HK', 'TW', 'KR']
CCY = {'US': 'USD', 'JP': 'JPY', 'HK': 'HKD', 'TW': 'TWD', 'KR': 'KRW'}
FIXED_LOT = {'US': 1, 'JP': 100, 'TW': 1000, 'KR': 1}          # HK 由 HKEX 每手股數；TW 只買整張（零股開關 false，規格 §2.8）
COLUMNS = ['asof', 'ticker', 'market', 'index_membership', 'currency', 'lot_size', 'close_local', 'lot_cost_base', 'adv60_base',
           'history_sessions', 'pass_adv', 'pass_px', 'pass_lot', 'in_pool', 'excluded_reason']
REASONS = ['no_price_data', 'stale_price', 'no_lot_size', 'history', 'adv', 'price', 'lot']   # excluded_reason 的固定次序
STALE_SESSIONS = 10           # 最後成交距 asof 超過 10 個市場 session → stale_price
TZ = dt.timezone(dt.timedelta(hours=8))
SOURCES = {   # 檔名 → (說明, URL)
    'sp500.html': ('S&P 500', 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'),
    'ndx_nasdaq.json': ('Nasdaq-100', 'https://api.nasdaq.com/api/quote/list-type/nasdaq100'),
    'n225.html': ('Nikkei 225', 'https://en.wikipedia.org/wiki/Nikkei_225'),
    'hsi.html': ('Hang Seng Index', 'https://en.wikipedia.org/wiki/Hang_Seng_Index'),
    'hscei.html': ('Hang Seng China Enterprises Index', 'https://en.wikipedia.org/wiki/Hang_Seng_China_Enterprises_Index'),
    'tw50_zh.html': ('Taiwan 50', 'https://zh.wikipedia.org/wiki/%E8%87%BA%E7%81%A350%E6%8C%87%E6%95%B8'),
    'kospi200.html': ('KOSPI 200', 'https://en.wikipedia.org/wiki/KOSPI_200'),
    'hkex_ListOfSecurities.xlsx': ('HKEX board lots', 'https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx'),
}

# ---------------- 雜湊 ----------------
def sha256_bytes(b): return 'sha256:' + hashlib.sha256(b).hexdigest()
def sha256_file(p):
    with open(p, 'rb') as f: return sha256_bytes(f.read())
def canonical_hash(obj):
    """策略規格 §3.4 的規範化（sort_keys、緊湊分隔符、ensure_ascii=False、allow_nan=False、UTF-8）。"""
    return sha256_bytes(json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8'))
def spec_hash(strategy):
    o = dict(strategy); o.pop('mode', None); return canonical_hash(o)

# ---------------- 成分 ----------------
def load_constituents(raw):
    """原始來源檔 → DataFrame[ticker, market, index_membership, lot_size]（與 research/opportunity_ta/universe.py 同一解析）。"""
    def rh(n): return pd.read_html(io.StringIO(open(os.path.join(raw, n), encoding='utf-8').read()))
    rows = []
    sp = rh('sp500.html')[0]
    rows += [(s.replace('.', '-'), 'US', 'SPX') for s in sp.Symbol]
    nd = json.load(open(os.path.join(raw, 'ndx_nasdaq.json')))['data']['data']['rows']
    rows += [(r['symbol'].replace('.', '-'), 'US', 'NDX') for r in nd]
    codes = re.findall(r'topSearchStr=(\d{3}[0-9A-Z])\b', open(os.path.join(raw, 'n225.html'), encoding='utf-8').read())
    rows += [(f'{c}.T', 'JP', 'N225') for c in dict.fromkeys(codes)]
    for n, idx in (('hsi.html', 'HSI'), ('hscei.html', 'HSCEI')):
        t = [x for x in rh(n) if 'Ticker' in x.columns and len(x) >= 40][0]
        rows += [(f'{int(re.search(r"(\d+)", str(tk)).group(1)):04d}.HK', 'HK', idx) for tk in t.Ticker]
    tw = rh('tw50_zh.html')[0]
    for a in ('股票代號', '股票代號.1'):
        for tk in tw[a]:
            m = re.search(r'(\d{4})', str(tk))
            if m: rows.append((f'{m.group(1)}.TW', 'TW', 'TW50'))
    kp = [x for x in rh('kospi200.html') if 'Symbol' in x.columns and len(x) >= 150][0]
    rows += [(f'{str(s).zfill(6)}.KS', 'KR', 'KOSPI200') for s in kp.Symbol]
    u = pd.DataFrame(rows, columns=['ticker', 'market', 'index'])
    u = u.groupby('ticker', as_index=False).agg(market=('market', 'first'), index_membership=('index', lambda x: '+'.join(sorted(set(x)))))
    u['lot_size'] = [FIXED_LOT.get(m) for m in u.market]
    hk = hk_board_lots(os.path.join(raw, 'hkex_ListOfSecurities.xlsx'))
    u.loc[u.market == 'HK', 'lot_size'] = u.loc[u.market == 'HK', 'ticker'].map(hk)
    return u
def hk_board_lots(path):
    """HKEX ListOfSecurities.xlsx → {'0005.HK': 400, ...}（Equity 與 REIT）。"""
    hk = pd.read_excel(path, header=2)
    hk = hk[hk['Category'].isin(['Equity', 'Real Estate Investment Trusts'])]
    lots = hk['Board Lot'].astype(str).str.replace(',', '').astype(float)
    return {f'{int(c):04d}.HK': int(l) for c, l in zip(hk['Stock Code'], lots)}

# ---------------- 價格與指標（與 research/opportunity_ta/prep.py 相同的清洗） ----------------
def fx_to_hkd(px_dir):
    fx = pd.read_parquet(os.path.join(px_dir, 'FX.parquet')).pivot(index='date', columns='ticker', values='close')
    f = pd.DataFrame({'USD': fx['HKD=X'], 'JPY': fx['HKD=X'] / fx['JPY=X'], 'TWD': fx['HKD=X'] / fx['TWD=X'],
                      'KRW': fx['HKD=X'] / fx['KRW=X'], 'HKD': 1.0}).sort_index().ffill()
    for c in ['JPY', 'TWD', 'KRW', 'USD']:
        med = f[c].rolling(21, center=True, min_periods=5).median(); f.loc[(f[c] / med - 1).abs() > 0.05, c] = np.nan; f[c] = f[c].ffill()
    return f
def market_metrics(df, fxs_all, asof):
    """一個市場的長表 → 每檔 close_local、adv60_base、history_sessions、最後成交距 asof 的 session 數。只用 ≤ asof 的數據。"""
    df = df[(df.date <= asof) & (df.volume > 0) & (df.close > 0) & df[['open', 'high', 'low', 'close']].notna().all(axis=1)]
    C = df.pivot(index='date', columns='ticker', values='close').sort_index(); V = df.pivot(index='date', columns='ticker', values='volume').reindex_like(C)
    n = C.notna().sum(axis=1); sess = n.index[n >= 0.3 * n.rolling(250, min_periods=1).max()]
    C, V = C.loc[sess], V.loc[sess]
    r = C.pct_change(fill_method=None); spike = (r.abs() > 0.6) & (r.shift(-1).abs() > 0.35)
    C, V = C.mask(spike), V.mask(spike)
    fxs = fxs_all.reindex(sess, method='ffill')
    adv60 = (C * V).mul(fxs, axis=0).rolling(60, min_periods=40).mean().iloc[-1]
    last_valid = C.notna()[::-1].idxmax()
    pos = pd.Series(np.arange(len(sess)), index=sess)
    return pd.DataFrame({'close_local': C.ffill().iloc[-1], 'adv60_base': adv60, 'history_sessions': C.notna().sum(),
                         'sessions_since_last': len(sess) - 1 - pos.reindex(last_valid.values).values,
                         'fx_hkd': float(fxs.iloc[-1])}), sess[-1]

# ---------------- 篩選與 CSV ----------------
def order_cap(nlv_base, r1_pct, r2_base): return min(r1_pct * nlv_base, r2_base)
def apply_filters(u, metrics, filters, cap, asof):
    """u：成分；metrics：{market: DataFrame}；filters：策略檔 universe.filters；cap：單筆上限（HKD）。回傳 COLUMNS 的 DataFrame。"""
    buf = 1 + filters['lot_price_buffer_pct'] / 100.0; out = []
    for _, row in u.iterrows():
        m = row.market; t = row.ticker; M = metrics.get(m); reasons = []
        x = M.loc[t] if (M is not None and t in M.index) else None
        lot = row.lot_size if row.lot_size is not None and not pd.isna(row.lot_size) else None
        close = adv = cost = None; hist = 0
        if x is None or pd.isna(x.close_local): reasons.append('no_price_data')
        else:
            close = float(x.close_local); hist = int(x.history_sessions); adv = None if pd.isna(x.adv60_base) else float(x.adv60_base)
            if x.sessions_since_last > STALE_SESSIONS: reasons.append('stale_price')
            if lot is not None: cost = lot * close * float(x.fx_hkd)
        if lot is None: reasons.append('no_lot_size')
        if hist < filters['min_history_sessions']: reasons.append('history')
        p_adv = adv is not None and adv >= filters['min_adv60_base'][m]
        p_px = close is not None and close >= filters['min_close_local'][m]
        p_lot = cost is not None and cost * buf <= cap
        if not p_adv: reasons.append('adv')
        if not p_px: reasons.append('price')
        if not p_lot: reasons.append('lot')
        reasons = [r for r in REASONS if r in reasons]
        out.append(dict(asof=asof, ticker=t, market=m, index_membership=row.index_membership, currency=CCY[m],
                        lot_size=None if lot is None else int(lot), close_local=close, lot_cost_base=cost, adv60_base=adv,
                        history_sessions=hist, pass_adv=p_adv, pass_px=p_px, pass_lot=p_lot, in_pool=not reasons, excluded_reason=';'.join(reasons)))
    return pd.DataFrame(out, columns=COLUMNS)
def _fmt(v, kind):
    if v is None or (isinstance(v, float) and np.isnan(v)): return ''
    if kind == 'bool': return 'true' if v else 'false'
    if kind == 'int': return str(int(v))
    if kind == 'px': return f'{float(v):.4f}'
    if kind == 'amt': return f'{float(v):.2f}'
    s = str(v)
    if any(ch in s for ch in ',"\n\r'): raise ValueError(f'field contains forbidden char: {s!r}')
    return s
KIND = dict(lot_size='int', history_sessions='int', close_local='px', lot_cost_base='amt', adv60_base='amt',
            pass_adv='bool', pass_px='bool', pass_lot='bool', in_pool='bool')
def to_csv_bytes(df):
    """規範化 CSV 位元組（見模組說明）。輸入列順序不影響輸出。"""
    order = {m: i for i, m in enumerate(MKTS)}
    rows = sorted(df.to_dict('records'), key=lambda r: (order[r['market']], r['ticker'].encode('ascii')))
    lines = [','.join(COLUMNS)]
    for r in rows: lines.append(','.join(_fmt(r[c], KIND.get(c, 'str')) for c in COLUMNS))
    return ('\n'.join(lines) + '\n').encode('utf-8')

# ---------------- 寫檔 ----------------
def atomic_write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
def write_outputs(out_dir, pool_id, csv_bytes, meta):
    """存檔不可覆寫：archive/<pool_id>.csv 已存在且內容不同 → 拒絕（內容相同 → 不動）。之後原子寫 ta_pool.csv、ta_pool.meta.json。"""
    arc_dir = os.path.join(out_dir, 'archive'); os.makedirs(arc_dir, exist_ok=True)
    arc = os.path.join(arc_dir, pool_id + '.csv')
    if os.path.exists(arc):
        if open(arc, 'rb').read() != csv_bytes:
            raise FileExistsError(f'{arc} 已存在且內容不同；存檔不可覆寫（規格 §16.2）')
    else:
        fd = os.open(arc, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(fd, 'wb') as f: f.write(csv_bytes)
    atomic_write(os.path.join(out_dir, 'ta_pool.csv'), csv_bytes)
    atomic_write(os.path.join(out_dir, 'ta_pool.meta.json'), (json.dumps(meta, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    return arc
