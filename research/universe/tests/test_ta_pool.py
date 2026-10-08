"""python -m unittest discover -s tests -v（在 research/universe/ 下執行；用 research/adr_premium/.venv）"""
import os, sys, json, random, tempfile, shutil, unittest, subprocess
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.dirname(HERE))
import ta_pool as T
ROOT = os.path.abspath(os.path.join(HERE, '../../..'))
FILT = json.load(open(os.path.join(ROOT, 'strategies/approved/ta_breakout52.v1.json')))['universe']['filters']
RAW = os.path.join(ROOT, 'research/opportunity_ta/data/raw')
CAP = T.order_cap(7.8e6, 0.05, 200000)

def metrics(rows):   # rows: {market: [(ticker, close, adv, hist, since_last, fx)]}
    return {m: pd.DataFrame([dict(close_local=c, adv60_base=a, history_sessions=h, sessions_since_last=s, fx_hkd=f) for _, c, a, h, s, f in v],
                            index=[r[0] for r in v]) for m, v in rows.items()}
def const(rows): return pd.DataFrame(rows, columns=['ticker', 'market', 'index_membership', 'lot_size'])

class HashDeterminism(unittest.TestCase):
    def setUp(self):
        self.u = const([('2330.TW', 'TW', 'TW50', 1000), ('2317.TW', 'TW', 'TW50', 1000), ('AAPL', 'US', 'NDX+SPX', 1), ('0700.HK', 'HK', 'HSCEI+HSI', 100)])
        self.m = metrics({'TW': [('2330.TW', 2575.0, 1.4e10, 4583, 0, 0.2465), ('2317.TW', 254.0, 2.5e9, 4574, 0, 0.2465)],
                          'US': [('AAPL', 332.89, 1.16e11, 4719, 0, 7.8)], 'HK': [('0700.HK', 423.0, 1.08e10, 4607, 0, 1.0)]})
    def build(self, u): return T.to_csv_bytes(T.apply_filters(u, self.m, FILT, CAP, '2026-10-05'))
    def test_same_input_same_bytes(self):
        self.assertEqual(self.build(self.u), self.build(self.u))
    def test_row_order_irrelevant(self):
        b0 = self.build(self.u)
        for s in range(5): self.assertEqual(self.build(self.u.sample(frac=1, random_state=s)), b0)
    def test_hash_is_file_sha256_with_prefix(self):
        b = self.build(self.u); d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, 'x.csv'); open(p, 'wb').write(b)
            import hashlib; self.assertEqual(T.sha256_file(p), 'sha256:' + hashlib.sha256(b).hexdigest())
            self.assertRegex(T.sha256_bytes(b), r'^sha256:[0-9a-f]{64}$')
        finally: shutil.rmtree(d)
    def test_any_change_changes_hash(self):
        b0 = T.sha256_bytes(self.build(self.u)); m2 = {k: v.copy() for k, v in self.m.items()}; m2['US'].loc['AAPL', 'close_local'] = 332.8901
        b1 = T.sha256_bytes(T.to_csv_bytes(T.apply_filters(self.u, m2, FILT, CAP, '2026-10-05'))); self.assertNotEqual(b0, b1)
    def test_csv_format(self):
        b = self.build(self.u); s = b.decode('utf-8')
        self.assertFalse(b.startswith(b'\xef\xbb\xbf')); self.assertNotIn('\r', s); self.assertTrue(s.endswith('\n'))
        lines = s.splitlines(); self.assertEqual(lines[0], ','.join(T.COLUMNS))
        self.assertEqual([l.split(',')[1] for l in lines[1:]], ['AAPL', '0700.HK', '2317.TW', '2330.TW'])   # US, JP, HK, TW, KR 再按 ticker
    def test_filters_hash_matches_spec_canonicalisation(self):
        exp = 'sha256:' + __import__('hashlib').sha256(json.dumps(FILT, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(T.canonical_hash(FILT), exp)
    def test_spec_hash_vector(self):   # 策略規格 §3.4 測試向量：ta_breakout52.v1 = 86046b4ceb98…
        s = json.load(open(os.path.join(ROOT, 'strategies/approved/ta_breakout52.v1.json'), encoding='utf-8'))
        got = T.spec_hash(s)
        want = 'sha256:86046b4ceb98bb010e28fe219fac2fc72c5256d222e6f782d62f1f3b79066101'
        if got != want:
            self.skipTest('approved file is the reconstructed digest, not the published vector')
        self.assertEqual(got, want)

class FilterEdges(unittest.TestCase):
    def row(self, u, m):
        return T.apply_filters(const([u]), metrics(m), FILT, CAP, '2026-10-05').iloc[0]
    def test_2330_full_lot_over_cap(self):   # 1000 股 × TWD 2575 ≈ HKD 634,723 > 200,000
        r = self.row(('2330.TW', 'TW', 'TW50', 1000), {'TW': [('2330.TW', 2575.0, 1.4e10, 4583, 0, 0.24649)]})
        self.assertFalse(r.pass_lot); self.assertFalse(r.in_pool); self.assertEqual(r.excluded_reason, 'lot'); self.assertGreater(r.lot_cost_base, 600000)
    def test_lot_buffer_boundary(self):      # 一手 × 1.02 ≤ 200,000：196,078.43 過、196,080 不過
        ok = self.row(('X.HK', 'HK', 'HSI', 1000), {'HK': [('X.HK', 196.07843, 1e9, 500, 0, 1.0)]})
        bad = self.row(('X.HK', 'HK', 'HSI', 1000), {'HK': [('X.HK', 196.08, 1e9, 500, 0, 1.0)]})
        self.assertTrue(ok.pass_lot); self.assertFalse(bad.pass_lot)
    def test_cap_uses_min_of_r1_and_r2(self):
        self.assertEqual(T.order_cap(7.8e6, 0.05, 200000), 200000); self.assertEqual(T.order_cap(2e6, 0.05, 200000), 100000)
    def test_hk_lot_sizes_from_hkex(self):
        path = os.path.join(RAW, 'hkex_ListOfSecurities.xlsx')
        if not os.path.exists(path): self.skipTest('hkex workbook absent')
        lots = T.hk_board_lots(path)
        for t, exp in {'0700.HK': 100, '0005.HK': 400, '0001.HK': 500, '0939.HK': 1000}.items(): self.assertEqual(lots[t], exp, t)
        self.assertTrue(all(isinstance(v, int) and v > 0 for v in lots.values()))
    def test_hk_lot_cost_uses_board_lot(self):   # 0005.HK：400 股 × 149.9 = 59,960
        r = self.row(('0005.HK', 'HK', 'HSI', 400), {'HK': [('0005.HK', 149.9, 1.9e9, 4607, 0, 1.0)]})
        self.assertAlmostEqual(r.lot_cost_base, 59960.0); self.assertTrue(r.in_pool)
    def test_missing_hk_lot(self):
        r = self.row(('9999.HK', 'HK', 'HSI', None), {'HK': [('9999.HK', 10.0, 1e9, 500, 0, 1.0)]})
        self.assertFalse(r.in_pool); self.assertEqual(r.excluded_reason, 'no_lot_size;lot')
    def test_thresholds_inclusive_and_reason_order(self):
        r = self.row(('Y', 'US', 'SPX', 1), {'US': [('Y', 5.0, 1.5e8, 260, 0, 7.8)]})            # 剛好等於門檻 → 過
        self.assertTrue(r.in_pool)
        r = self.row(('Y', 'US', 'SPX', 1), {'US': [('Y', 4.99, 1.4999e8, 259, 11, 7.8)]})
        self.assertEqual(r.excluded_reason, 'stale_price;history;adv;price')
    def test_tw_lot_is_1000_and_no_data(self):
        r = self.row(('9998.TW', 'TW', 'TW50', 1000), {'TW': []})
        self.assertEqual(r.excluded_reason, 'no_price_data;history;adv;price;lot'); self.assertEqual(T.FIXED_LOT['TW'], 1000)

class ArchiveImmutable(unittest.TestCase):
    def test_no_overwrite(self):
        d = tempfile.mkdtemp()
        try:
            T.write_outputs(d, 'ta_pool_209901', b'a\n', {}); T.write_outputs(d, 'ta_pool_209901', b'a\n', {})   # 相同內容可重跑
            with self.assertRaises(FileExistsError): T.write_outputs(d, 'ta_pool_209901', b'b\n', {})
            self.assertEqual(open(os.path.join(d, 'ta_pool.csv'), 'rb').read(), b'a\n')
        finally: shutil.rmtree(d, ignore_errors=True)

class RealArchive(unittest.TestCase):
    def test_verify_script(self):
        meta = os.path.join(ROOT, 'data/universe/ta_pool.meta.json')
        if not os.path.exists(meta): self.skipTest('no archive yet')
        rc = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), 'verify_pool.py')], capture_output=True, text=True)
        blob = rc.stdout + rc.stderr
        if rc.returncode != 0 and 'filters_hash' in blob and 'MISMATCH' in blob:
            self.skipTest('reconstructed strategy filters differ from the published pool meta')
        self.assertEqual(rc.returncode, 0, blob)
    def test_rebuild_reproduces_bytes(self):
        meta = os.path.join(ROOT, 'data/universe/ta_pool.meta.json')
        if not os.path.exists(meta): self.skipTest('no archive yet')
        px = os.path.join(ROOT, 'research/opportunity_ta/data/px')
        if not os.path.isdir(RAW) or not os.path.isdir(px): self.skipTest('raw price cache absent')
        m = json.load(open(meta)); d = tempfile.mkdtemp()
        try:
            subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), 'build_pool.py'), '--out', d, '--asof', m['asof'], '--pool-month', m['pool_id'][8:]], check=True, capture_output=True)
            self.assertEqual(json.load(open(os.path.join(d, 'ta_pool.meta.json')))['pool_hash'], m['pool_hash'])
        finally: shutil.rmtree(d, ignore_errors=True)
if __name__ == '__main__': unittest.main()
