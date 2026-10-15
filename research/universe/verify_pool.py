"""重算 pool_hash 並核對（執行規格 v0.8 §3.1 第 5 點：存檔 sha256 = pool_hash）。
用法：python verify_pool.py [archive.csv] [--meta ta_pool.meta.json] [--expect sha256:...] [--strategy FILE] [--root DIR] [--current FILE]
退出碼 0 = 全部一致；1 = 有不符。--root 指向暫存目錄時，比對該目錄裡的現行 CSV，不比正式檔。"""
import os, sys, json, hashlib, argparse
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
def pool_hash(path):
    with open(path, 'rb') as f: return 'sha256:' + hashlib.sha256(f.read()).hexdigest()
def _first_existing(paths):
    for path in paths:
        if path and os.path.exists(path): return path
    return paths[0]
def _meta_path(base, explicit, rooted):
    if explicit: return explicit
    if rooted:
        return _first_existing([
            os.path.join(base, 'ta_pool.meta.json'),
            os.path.join(base, 'data', 'universe', 'ta_pool.meta.json'),
            os.path.join(REPO, 'data', 'universe', 'ta_pool.meta.json'),
        ])
    return os.path.join(REPO, 'data', 'universe', 'ta_pool.meta.json')
def _resolve(base, rel):
    rel = rel.replace('\\', '/')
    name = os.path.basename(rel)
    if '/archive/' in ('/' + rel) or rel.startswith('archive/'):
        options = [
            os.path.join(base, rel),
            os.path.join(base, 'archive', name),
            os.path.join(base, 'data', 'universe', 'archive', name),
        ]
    else:
        options = [
            os.path.join(base, rel),
            os.path.join(base, name),
            os.path.join(base, 'data', 'universe', name),
        ]
    return _first_existing(options)
def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument('archive', nargs='?')
    a.add_argument('--meta', default=None)
    a.add_argument('--expect')
    a.add_argument('--strategy', default=None)
    a.add_argument('--root', default=None)
    a.add_argument('--current', default=None)
    o = a.parse_args(argv); ok = True
    base = os.path.abspath(o.root) if o.root else REPO
    meta_path = _meta_path(base, o.meta, o.root is not None)
    strategy = o.strategy or os.path.join(REPO, 'strategies/approved/ta_breakout52.v1.json')
    meta = json.load(open(meta_path, encoding='utf-8')) if os.path.exists(meta_path) else None
    if o.archive:
        arc = o.archive
    elif meta:
        arc = _resolve(base, meta['csv']['archive'])
    else:
        print('RESULT FAIL'); return 1
    h = pool_hash(arc); print('archive     ', arc); print('pool_hash   ', h)
    def chk(name, want):
        nonlocal ok; good = want == h; ok &= good; print(f'{name:12s}', want, 'OK' if good else 'MISMATCH')
    if o.expect: chk('expected', o.expect)
    if meta:
        chk('meta', meta['pool_hash'])
        current_csv = o.current or _resolve(base, meta['csv']['file'])
        if os.path.exists(current_csv): chk('ta_pool.csv', pool_hash(current_csv))
        if os.path.exists(strategy):
            f = json.load(open(strategy, encoding='utf-8'))['universe']['filters']
            fh = 'sha256:' + hashlib.sha256(json.dumps(f, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
            good = fh == meta['filters_hash']; ok &= good; print('filters_hash', fh, 'OK' if good else 'MISMATCH')
    print('RESULT', 'OK' if ok else 'FAIL'); return 0 if ok else 1
if __name__ == '__main__': sys.exit(main())
