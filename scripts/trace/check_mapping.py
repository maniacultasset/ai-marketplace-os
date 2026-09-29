#!/usr/bin/env python3
"""Prove every normative spec statement has a verdict. Usage: check_mapping.py <cNN.json>...
For each register cNN.json it reads cNN.map.json next to it. Exit 0 only if all are complete."""
import json, os, re, sys
STATUS = {'covered', 'covered-changed', 'gap', 'conflict', 'needs-client', 'other-team', 'later-phase'}
OWNER = {'backend-db', 'ai-team', 'fe-team', 'shared', 'client', 'ops'}
ok = True
for reg in sys.argv[1:]:
    c = json.load(open(reg, encoding='utf-8'))
    norm = [it['id'] for it in c['items'] if it['kind'] != 'info']
    mp = reg[:-5] + '.map.json'
    if not os.path.exists(mp): print(f"{c['chunk']}: no {os.path.basename(mp)}"); ok = False; continue
    try: m = json.load(open(mp, encoding='utf-8'))
    except Exception as e: print(f'{mp}: INVALID JSON: {e}'); ok = False; continue
    got, errs, stat = {}, [], {}
    for e in m.get('items', []):
        i = e.get('id')
        if i in got: errs.append(f'{i}: duplicate verdict')
        got[i] = e
        s = e.get('status')
        if s not in STATUS: errs.append(f'{i}: bad status {s!r}')
        if e.get('owner') not in OWNER: errs.append(f'{i}: bad owner {e.get("owner")!r}')
        if s in ('covered', 'covered-changed', 'conflict', 'needs-client') and not (e.get('ref') or '').strip():
            errs.append(f'{i}: {s} needs a ref')
        if s in ('conflict', 'needs-client') and not re.search(r'\bT\d+\b|new', e.get('ref', '')):
            errs.append(f'{i}: {s} ref must name a TEAM-QUESTIONS row (T<n>) or "new: ..."')
        stat[s] = stat.get(s, 0) + 1
    missing = [i for i in norm if i not in got]
    extra = [i for i in got if i not in set(norm)]
    print(f"{c['chunk']}: {len(norm)} normative, {len(got)} verdicts, missing {len(missing)}, extra {len(extra)}, errors {len(errs)}  {dict(sorted(stat.items()))}")
    if missing: print('  missing: ' + ', '.join(missing[:30]))
    if extra: print('  not normative / unknown: ' + ', '.join(extra[:15]))
    for x in errs[:25]: print('  ' + x)
    if missing or errs: ok = False
sys.exit(0 if ok else 1)
