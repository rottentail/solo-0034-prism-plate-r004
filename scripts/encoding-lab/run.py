#!/usr/bin/env python3
"""Encoding-upgrade evaluation harness for prism-plate (ADR-0001 decision support).

Builds two real service binaries from the untouched repo source:
  binA  stock build (current PNG compressor)
  binB  the same source with a build-time -overlay that swaps only the PNG
        compression level (pixels untouched), simulating the planned upgrade
then drives both over real HTTP through six phases:
  1 pre-upgrade baseline (revisions, proof pin, ETag/304, restart restore)
  2 upgrade on the same data root (old-reference reads, pixel-equal re-renders)
  3 local real changes (hidden color under low alpha, 1px cutout, alpha=0,
    sub-sample move) to find the exact fold boundary
  4 offline re-render via bundle export/import
  5 transcode check (decode+re-encode reproduces binB bytes) and timings
  6 512x512 canvas bound: render vs transcode cost
Writes facts.json plus captured PNGs into the work directory. Nothing in the
repo or in any existing data root is modified.

Usage: python3 scripts/encoding-lab/run.py [--work DIR] [--go GO_BINARY]
"""
import argparse, copy, hashlib, json, os, pathlib, shutil, signal, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', default=None, help='work directory (default: a new temp dir, kept for inspection)')
    ap.add_argument('--go', default=os.environ.get('GO', 'go'))
    args = ap.parse_args()
    work = pathlib.Path(args.work) if args.work else pathlib.Path(tempfile.mkdtemp(prefix='prism-encoding-lab-'))
    work.mkdir(parents=True, exist_ok=True)
    out = work / 'out'; out.mkdir(exist_ok=True)

    # --- build binA (stock), pngtool, and binB (encoder-swapped via overlay) ---
    subprocess.run([args.go, 'build', '-o', str(work / 'binA'), './cmd/prismplate'], cwd=REPO, check=True)
    subprocess.run([args.go, 'build', '-o', str(work / 'pngtool'), './scripts/encoding-lab'], cwd=REPO, check=True)
    src = (REPO / 'internal/raster/raster.go').read_text()
    anchor = 'var out bytes.Buffer\n\terr := png.Encode(&out, img)\n\treturn out.Bytes(), err'
    swapped = ('var out bytes.Buffer\n\tenc := png.Encoder{CompressionLevel: png.BestCompression}\n'
               '\terr := enc.Encode(&out, img)\n\treturn out.Bytes(), err')
    if anchor not in src:
        raise SystemExit('raster.go encode anchor not found; regenerate the overlay manually')
    ovdir = work / 'overlay'; ovdir.mkdir(exist_ok=True)
    (ovdir / 'raster.go').write_text(src.replace(anchor, swapped))
    (work / 'overlay.json').write_text(json.dumps({'Replace': {str(REPO / 'internal/raster/raster.go'): str(ovdir / 'raster.go')}}))
    subprocess.run([args.go, 'build', '-overlay', str(work / 'overlay.json'), '-o', str(work / 'binB'), './cmd/prismplate'],
                   cwd=REPO, check=True)

    BINA, BINB, TOOL = str(work / 'binA'), str(work / 'binB'), str(work / 'pngtool')
    facts = {'go': subprocess.run([args.go, 'version'], capture_output=True, text=True).stdout.strip(),
             'platform': subprocess.run(['uname', '-sm'], capture_output=True, text=True).stdout.strip(),
             'phases': {}}

    def free_port():
        s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p

    class Svc:
        def __init__(self, binary, root):
            self.binary, self.root, self.proc = binary, pathlib.Path(root), None
            self.port = free_port(); self.url = f'http://127.0.0.1:{self.port}'
            self.log = open(self.root.parent / (self.root.name + '.log'), 'w')
        def start(self):
            self.proc = subprocess.Popen([self.binary, '-listen', f'127.0.0.1:{self.port}', '-data', str(self.root)],
                                         stdout=self.log, stderr=self.log)
            for _ in range(100):
                if self.proc.poll() is not None: raise RuntimeError('service exited early')
                try:
                    urllib.request.urlopen(self.url + '/healthz', timeout=1).close(); return
                except OSError: time.sleep(.05)
            raise RuntimeError('startup timed out')
        def stop(self):
            if self.proc and self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM); self.proc.wait(timeout=45)
        def call(self, method, path, data=None, status=200, raw=False, headers=None):
            body = data if isinstance(data, bytes) else (json.dumps(data).encode() if data is not None else None)
            req = urllib.request.Request(self.url + path, data=body, method=method,
                                         headers=headers or {'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=35) as r:
                    code, payload, hs = r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
            except urllib.error.HTTPError as e:
                code, payload, hs = e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}
            if code != status: raise RuntimeError(f'{method} {path}: expected {status}, got {code}: {payload[:200]!r}')
            return (payload, hs) if raw else (json.loads(payload) if payload else None)

    def tool(*a):
        r = subprocess.run([TOOL, *a], capture_output=True, text=True)
        if r.returncode != 0: raise RuntimeError(f'pngtool {a}: {r.stderr}')
        return json.loads(r.stdout)

    def save(name, data):
        p = out / name; p.write_bytes(data); return str(p)

    def artifact_key(plate, rev, scene_digest, fmt, digest):
        return hashlib.sha256(f'{plate}:{rev}:{scene_digest}:{fmt}:{digest}'.encode()).hexdigest()

    seed = json.loads((REPO / 'examples/amber-proof.json').read_text())

    # ---------- phase 1: pre-upgrade baseline on stock encoder ----------
    for r in ('root1', 'root2', 'root3', 'root-big'):
        shutil.rmtree(work / r, ignore_errors=True)
    root1 = work / 'root1'; root1.mkdir()
    s = Svc(BINA, root1); s.start()
    p1 = {'render_ms': {}, 'artifacts': {}}
    created = s.call('POST', '/v1/plates', seed, status=201)
    doc2 = copy.deepcopy(seed['document'])
    for pt in doc2['layers'][1]['shapes'][0]['path'][6]['points'] + doc2['layers'][1]['shapes'][0]['path'][7]['points']:
        pt['x'] = 123  # cutout inner edge 124 -> 123 (1px real change)
    s.call('PUT', '/v1/plates/amber-plate', {'expected_revision': 1, 'document': doc2})
    doc3 = copy.deepcopy(seed['document'])
    doc3['layers'][1]['shapes'][0]['brush']['color']['a'] = 0.80  # global alpha change
    s.call('PUT', '/v1/plates/amber-plate', {'expected_revision': 2, 'document': doc3})
    scene_digests = {r['number']: r['digest'] for r in s.call('GET', '/v1/plates/amber-plate')['revisions']}
    for rev in (1, 2, 3):
        for _ in range(3):
            t0 = time.perf_counter()
            a = s.call('POST', f'/v1/plates/amber-plate/render?revision={rev}&format=png', status=201)
            p1['render_ms'].setdefault(rev, []).append(round((time.perf_counter() - t0) * 1000, 2))
        raw, hs = s.call('GET', '/v1/artifacts/' + a['id'], raw=True)
        assert hashlib.sha256(raw).hexdigest() == a['digest']
        assert a['id'] == artifact_key('amber-plate', rev, scene_digests[rev], 'png', a['digest']), 'key formula mismatch'
        p1['artifacts'][rev] = {'id': a['id'], 'digest': a['digest'], 'etag': hs['etag'], 'bytes': len(raw),
                                'file': save(f'A-rev{rev}.png', raw), 'key_formula_verified': True}
    a1 = p1['artifacts'][1]
    s.call('POST', '/v1/proofs', {'id': 'proof-amber', 'artifact': a1['id']}, status=201)
    s.call('PUT', '/v1/proofs/proof-amber', {'state': 'accepted', 'note': 'phase1 baseline accept'})
    s.call('DELETE', '/v1/artifacts/' + a1['id'], status=409)
    s.call('GET', '/v1/artifacts/' + a1['id'], status=304, raw=True, headers={'If-None-Match': a1['etag']})
    p1['state_json_bytes'] = os.path.getsize(root1 / 'state.json')
    p1['artifact_count'] = len(json.loads((root1 / 'state.json').read_text())['artifacts'])
    s.stop(); s.start()
    raw2, hs2 = s.call('GET', '/v1/artifacts/' + a1['id'], raw=True)
    p1['restore_identical'] = hashlib.sha256(raw2).hexdigest() == a1['digest'] and hs2['etag'] == a1['etag']
    s.stop()
    facts['phases']['phase1_pre_upgrade'] = p1

    # ---------- phase 2: compressor upgrade, same data root ----------
    s = Svc(BINB, root1); s.start()
    p2 = {'old_reads': {}, 're_renders': {}}
    for rev in (1, 2, 3):
        old = p1['artifacts'][rev]
        raw, hs = s.call('GET', '/v1/artifacts/' + old['id'], raw=True)
        p2['old_reads'][rev] = {'bytes_identical': hashlib.sha256(raw).hexdigest() == old['digest'],
                                'etag': hs['etag'], 'etag_matches_phase1': hs['etag'] == old['etag']}
        s.call('GET', '/v1/artifacts/' + old['id'], status=304, raw=True, headers={'If-None-Match': old['etag']})
    s.call('DELETE', '/v1/artifacts/' + a1['id'], status=409)  # proof pin survives the upgrade
    p2['proof_pin_holds'] = True
    for rev in (1, 2, 3):
        b = s.call('POST', f'/v1/plates/amber-plate/render?revision={rev}&format=png', status=201)
        raw, hs = s.call('GET', '/v1/artifacts/' + b['id'], raw=True)
        f = save(f'B-rev{rev}.png', raw)
        d = tool('diff', p1['artifacts'][rev]['file'], f)
        assert d['pixel_equal'], f'rev{rev} re-render not pixel-equal'
        p2['re_renders'][rev] = {'new_id': b['id'], 'new_digest': b['digest'],
                                 'id_changed': b['id'] != p1['artifacts'][rev]['id'],
                                 'digest_changed': b['digest'] != p1['artifacts'][rev]['digest'],
                                 'bytes': len(raw), 'pixel_equal_to_phase1': d['pixel_equal'], 'file': f}
    p2['state_json_bytes'] = os.path.getsize(root1 / 'state.json')
    p2['artifact_count'] = len(json.loads((root1 / 'state.json').read_text())['artifacts'])
    p2['determinism_checks_would_fail'] = sum(1 for r in p2['re_renders'].values() if r['digest_changed'])
    s.stop()
    facts['phases']['phase2_upgrade_same_root'] = p2

    # ---------- phase 3: local real changes (lab plate, stock encoder) ----------
    def lab_doc(veil, hole_x1=56, veil_x1=32):
        return {'width': 64, 'height': 64, 'background': {'r': 0, 'g': 0, 'b': 0, 'a': 0},
                'layers': [{'id': 'lab-layer', 'opacity': 1, 'blend': 'normal', 'shapes': [
                    {'id': 'veil', 'fill_rule': 'nonzero', 'transform': [1, 0, 0, 1, 0, 0],
                     'path': [{'kind': 'M', 'points': [{'x': 0, 'y': 0}]},
                              {'kind': 'L', 'points': [{'x': veil_x1, 'y': 0}]},
                              {'kind': 'L', 'points': [{'x': veil_x1, 'y': 64}]},
                              {'kind': 'L', 'points': [{'x': 0, 'y': 64}]}, {'kind': 'Z'}],
                     'brush': {'kind': 'solid', 'color': veil}},
                    {'id': 'card', 'fill_rule': 'evenodd', 'transform': [1, 0, 0, 1, 0, 0],
                     'path': [{'kind': 'M', 'points': [{'x': 32, 'y': 0}]},
                              {'kind': 'L', 'points': [{'x': 64, 'y': 0}]},
                              {'kind': 'L', 'points': [{'x': 64, 'y': 64}]},
                              {'kind': 'L', 'points': [{'x': 32, 'y': 64}]}, {'kind': 'Z'},
                              {'kind': 'M', 'points': [{'x': 48, 'y': 24}]},
                              {'kind': 'L', 'points': [{'x': hole_x1, 'y': 24}]},
                              {'kind': 'L', 'points': [{'x': hole_x1, 'y': 40}]},
                              {'kind': 'L', 'points': [{'x': 48, 'y': 40}]}, {'kind': 'Z'}],
                     'brush': {'kind': 'solid', 'color': {'r': 0.2, 'g': 0.4, 'b': 0.9, 'a': 1}}}]}]}
    red_veil = {'r': 1, 'g': 0, 'b': 0, 'a': 0.004}
    blue_veil = {'r': 0, 'g': 0, 'b': 1, 'a': 0.004}
    variants = {'D0_base': lab_doc(red_veil), 'D1_hidden_color': lab_doc(blue_veil),
                'D2_cutout_1px': lab_doc(red_veil, hole_x1=55),
                'D3_transparent_red': lab_doc({'r': 1, 'g': 0, 'b': 0, 'a': 0}),
                'D4_transparent_blue': lab_doc({'r': 0, 'g': 0, 'b': 1, 'a': 0}),
                'D5_subsample_move': lab_doc(red_veil, veil_x1=32.01)}
    root2 = work / 'root2'; root2.mkdir()
    s = Svc(BINA, root2); s.start()
    s.call('POST', '/v1/plates', {'id': 'lab-plate', 'title': '微变实验版', 'document': variants['D0_base']}, status=201)
    p3 = {'renders': {}, 'pairs': {}}
    for i, name in enumerate(list(variants)[1:], start=1):
        s.call('PUT', '/v1/plates/lab-plate', {'expected_revision': i, 'document': variants[name]})
    plate = s.call('GET', '/v1/plates/lab-plate')
    for rev, name in enumerate(variants, start=1):
        a = s.call('POST', f'/v1/plates/lab-plate/render?revision={rev}&format=png', status=201)
        raw, _ = s.call('GET', '/v1/artifacts/' + a['id'], raw=True)
        p3['renders'][name] = {'revision': rev, 'id': a['id'], 'digest': a['digest'], 'bytes': len(raw),
                               'scene_digest': plate['revisions'][rev - 1]['digest'],
                               'file': save(f'lab-{name}.png', raw)}
    def pair(x, y):
        d0 = tool('diff', p3['renders'][x]['file'], p3['renders'][y]['file'], '0')
        d2 = tool('diff', p3['renders'][x]['file'], p3['renders'][y]['file'], '2')
        return {'exact_diff_pixels': d0['exact_diff_pixels'], 'max_channel_delta': d0['max_channel_delta'],
                'visual_diff_pixels_tol0': d0['visual_diff_pixels'], 'visual_diff_pixels_tol2': d2['visual_diff_pixels'],
                'pixel_hash_equal': d0['pixel_hash_a'] == d0['pixel_hash_b'],
                'scene_digest_equal': p3['renders'][x]['scene_digest'] == p3['renders'][y]['scene_digest']}
    p3['pairs']['D0_vs_D1_hidden_color'] = pair('D0_base', 'D1_hidden_color')
    p3['pairs']['D0_vs_D2_cutout_1px'] = pair('D0_base', 'D2_cutout_1px')
    p3['pairs']['D3_vs_D4_transparent'] = pair('D3_transparent_red', 'D4_transparent_blue')
    p3['pairs']['D0_vs_D5_subsample'] = pair('D0_base', 'D5_subsample_move')
    s.stop()
    facts['phases']['phase3_local_real_changes'] = p3

    # ---------- phase 4: offline re-render through bundle export/import ----------
    s = Svc(BINB, root1); s.start()
    sealed, _ = s.call('GET', '/v1/plates/amber-plate/bundle', raw=True)
    s.stop()
    root3 = work / 'root3'; root3.mkdir()
    s = Svc(BINB, root3); s.start()
    s.call('POST', '/v1/bundles', sealed, status=201)
    b = s.call('POST', '/v1/plates/amber-plate/render?revision=1&format=png', status=201)
    raw, _ = s.call('GET', '/v1/artifacts/' + b['id'], raw=True)
    f = save('B-imported-rev1.png', raw)
    d = tool('diff', p1['artifacts'][1]['file'], f)
    facts['phases']['phase4_offline_rerender'] = {
        'imported_render_id': b['id'], 'id_matches_origin': b['id'] == p1['artifacts'][1]['id'],
        'digest_matches_origin': b['digest'] == p1['artifacts'][1]['digest'],
        'pixel_equal_to_origin': d['pixel_equal'], 'bytes': len(raw)}
    s.stop()

    # ---------- phase 5: transcode check, timings, storage model ----------
    p5 = {'transcode': {}, 'timings': {}}
    for rev in (1, 2, 3):
        out_b = str(out / f'transcoded-rev{rev}.png')
        r = tool('reencode', p1['artifacts'][rev]['file'], out_b)
        service_b = pathlib.Path(p2['re_renders'][rev]['file']).read_bytes()
        p5['transcode'][rev] = {**r, 'byte_identical_to_service_binB': pathlib.Path(out_b).read_bytes() == service_b}
        p5['timings'].setdefault('decode_ns', []).append(r['decode_ns'])
        p5['timings'].setdefault('reencode_ns', []).append(r['decode_ns'] + r['encode_ns'])
    render_ms = [m for rev in p1['render_ms'].values() for m in rev]
    p5['timings']['render_ms_samples'] = render_ms
    p5['timings']['render_ms_median'] = sorted(render_ms)[len(render_ms) // 2]
    p5['timings']['startup_index_projection_ms_at_256'] = round(
        (sum(p5['timings']['decode_ns']) / len(p5['timings']['decode_ns'])) * 256 / 1e6, 1)
    avg_a = sum(p1['artifacts'][r]['bytes'] for r in (1, 2, 3)) / 3
    avg_b = sum(p2['re_renders'][r]['bytes'] for r in (1, 2, 3)) / 3
    p5['png_sizes'] = {'avg_encoder_A': round(avg_a, 1), 'avg_encoder_B': round(avg_b, 1),
                       'compressor_delta_pct': round((avg_b - avg_a) / avg_a * 100, 2)}
    p5['capacity_projection'] = {
        'artifact_object_cap': 256,
        'revisions_rerendered_to_exhaust_cap': {'A_per_encoding_originals': 128,
                                                'C_commit_time_pixel_dedup': 'unbounded (no new objects)',
                                                'D_if_all_variants_materialized': 128}}
    facts['phases']['phase5_transcode_and_model'] = p5

    # ---------- phase 6: 512x512 canvas bound, render vs transcode ----------
    def rect(x0, y0, x1, y1):
        return [{'kind': 'M', 'points': [{'x': x0, 'y': y0}]}, {'kind': 'L', 'points': [{'x': x1, 'y': y0}]},
                {'kind': 'L', 'points': [{'x': x1, 'y': y1}]}, {'kind': 'L', 'points': [{'x': x0, 'y': y1}]}, {'kind': 'Z'}]
    big = {'width': 512, 'height': 512, 'background': {'r': 0.05, 'g': 0.06, 'b': 0.1, 'a': 1},
           'layers': [{'id': 'base', 'opacity': 1, 'blend': 'normal', 'shapes': [
               {'id': 'r1', 'fill_rule': 'nonzero', 'transform': [1, 0, 0, 1, 0, 0], 'path': rect(20, 20, 480, 300),
                'brush': {'kind': 'linear', 'from': {'x': 0, 'y': 0}, 'to': {'x': 512, 'y': 512},
                          'color': {'r': 0, 'g': 0, 'b': 0, 'a': 0},
                          'stops': [{'at': 0, 'color': {'r': 1, 'g': 0.6, 'b': 0.2, 'a': 1}},
                                    {'at': 1, 'color': {'r': 0.8, 'g': 0.1, 'b': 0.3, 'a': 1}}]}},
               {'id': 'r2', 'fill_rule': 'evenodd', 'transform': [1, 0.1, -0.05, 1, 8, -4], 'path': rect(60, 200, 460, 480),
                'brush': {'kind': 'solid', 'color': {'r': 0.1, 'g': 0.6, 'b': 0.85, 'a': 0.8}}}]}]}
    root4 = work / 'root-big'; root4.mkdir()
    p6 = {}
    for name, binary in (('A', BINA), ('B', BINB)):
        s = Svc(binary, root4); s.start()
        if name == 'A':
            s.call('POST', '/v1/plates', {'id': 'big-plate', 'title': '满幅实验版', 'document': big}, status=201)
        samples, art = [], None
        for _ in range(3):
            t0 = time.perf_counter()
            art = s.call('POST', '/v1/plates/big-plate/render?revision=1&format=png', status=201)
            samples.append(round((time.perf_counter() - t0) * 1000, 1))
        raw, _ = s.call('GET', '/v1/artifacts/' + art['id'], raw=True)
        save(f'big-{name}.png', raw)
        p6[name] = {'render_ms': samples, 'bytes': len(raw), 'digest': art['digest'], 'id': art['id']}
        s.stop()
    d = tool('diff', str(out / 'big-A.png'), str(out / 'big-B.png'))
    t = tool('reencode', str(out / 'big-A.png'), str(out / 'big-transcoded.png'))
    p6['cross'] = {'pixel_equal': d['pixel_equal'],
                   'transcode_byte_identical': (out / 'big-transcoded.png').read_bytes() == (out / 'big-B.png').read_bytes(),
                   'decode_ns': t['decode_ns'], 'encode_ns': t['encode_ns'],
                   'transcode_ms': round((t['decode_ns'] + t['encode_ns']) / 1e6, 1)}
    facts['phases']['phase6_big_canvas'] = p6

    (out / 'facts.json').write_text(json.dumps(facts, indent=1, ensure_ascii=False))
    print(json.dumps({'result': 'ok', 'work': str(work),
                      'phase2_id_changes': sum(1 for r in p2['re_renders'].values() if r['id_changed']),
                      'phase2_pixel_equal': all(r['pixel_equal_to_phase1'] for r in p2['re_renders'].values()),
                      'phase3_exact_diffs': {k: v['exact_diff_pixels'] for k, v in p3['pairs'].items()},
                      'phase5_transcode_exact': all(v['byte_identical_to_service_binB'] for v in p5['transcode'].values()),
                      'phase6_transcode_exact': p6['cross']['transcode_byte_identical']}, ensure_ascii=False))

if __name__ == '__main__':
    main()
