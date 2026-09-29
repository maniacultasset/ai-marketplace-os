#!/usr/bin/env python3
"""Prove every non-blank spec line is traced. Usage: check_coverage.py <chunk.json>... [--src FILE]
Exit 0 only if every chunk is valid and covers 100% of its non-blank lines."""
import json, re, sys
args = sys.argv[1:]; src = 'docs/source/system-arch-llms-full.txt'
if '--src' in args: i = args.index('--src'); src = args[i + 1]; del args[i:i + 2]
L = open(src, encoding='utf-8').read().split('\n')
NORMATIVE = {'requirement', 'constraint', 'data', 'interface', 'flow', 'decision', 'scope', 'quality', 'risk'}
WHY = {'heading', 'navigation', 'context-prose', 'table-scaffold', 'restatement'}
ok = True
for path in args:
    errs = []
    try: c = json.load(open(path, encoding='utf-8'))
    except Exception as e: print(f'{path}: INVALID JSON: {e}'); ok = False; continue
    s, e, items = c['start'], c['end'], c.get('items', [])
    covered, ids = set(), set()
    for it in items:
        iid, (a, b) = it.get('id', '?'), it.get('lines', [0, 0])
        if not (s <= a <= b <= e): errs.append(f'{iid}: lines {a}-{b} outside chunk {s}-{e}')
        if not re.fullmatch(r'R\d{5}[a-z]?', iid) or int(iid[1:6]) != a: errs.append(f'{iid}: id must be R + zero-padded start line (+a/b/..)')
        if iid in ids: errs.append(f'{iid}: duplicate id')
        ids.add(iid)
        k = it.get('kind')
        if k in NORMATIVE:
            if not it.get('text'): errs.append(f'{iid}: normative item without text')
        elif k == 'info':
            if it.get('why_not') not in WHY: errs.append(f'{iid}: info needs why_not in {sorted(WHY)}')
        else: errs.append(f'{iid}: bad kind {k!r}')
        covered.update(range(a, b + 1))
    need = [n for n in range(s, e + 1) if L[n - 1].strip()]
    miss = [n for n in need if n not in covered]
    runs, r = [], None
    for n in miss:
        if r and n == r[1] + 1: r[1] = n
        else: r = [n, n]; runs.append(r)
    pct = 100.0 * (len(need) - len(miss)) / max(1, len(need))
    kinds = {}
    for it in items: kinds[it.get('kind')] = kinds.get(it.get('kind'), 0) + 1
    norm = sum(v for k, v in kinds.items() if k in NORMATIVE)
    print(f"{c.get('chunk', path)}: lines {s}-{e}, coverage {pct:.1f}% ({len(need) - len(miss)}/{len(need)}), items {len(items)} ({norm} normative), errors {len(errs)}")
    if runs: print('  uncovered: ' + ', '.join(f'{a}-{b}' if a != b else f'{a}' for a, b in runs[:40]))
    for x in errs[:30]: print('  ' + x)
    if miss or errs: ok = False
sys.exit(0 if ok else 1)
