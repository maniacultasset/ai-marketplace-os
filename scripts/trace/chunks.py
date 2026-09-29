#!/usr/bin/env python3
"""Split the spec snapshot into trace chunks on page (`# `) and section (`## `) boundaries.
Prints JSON: [{id, start, end, nonblank, pages}]. Line numbers are 1-based and inclusive."""
import json, sys
SRC = sys.argv[1] if len(sys.argv) > 1 else 'docs/source/system-arch-llms-full.txt'
TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 350          # non-blank lines per chunk
L = open(SRC, encoding='utf-8').read().split('\n')
if L and L[-1] == '': L = L[:-1]
cut_ok = {i + 1 for i, l in enumerate(L) if l.startswith(('# ', '## ', '### '))}
chunks, start, nb = [], 1, 0
for ln in range(1, len(L) + 1):
    if ln in cut_ok and ln > start and nb >= TARGET:
        chunks.append((start, ln - 1)); start, nb = ln, 0
    if L[ln - 1].strip(): nb += 1
    if nb >= TARGET * 1.4 and not L[ln - 1].strip():          # no heading for too long: cut on a blank line
        chunks.append((start, ln)); start, nb = ln + 1, 0
chunks.append((start, len(L)))
out = []
for k, (s, e) in enumerate(chunks, 1):
    pages = [L[i - 1][2:].strip() for i in range(s, e + 1) if L[i - 1].startswith('# ')]
    owner = next((L[i - 1][2:].strip() for i in range(s, 0, -1) if L[i - 1].startswith('# ')), '')
    out.append({'id': f'c{k:02d}', 'start': s, 'end': e, 'starts_inside': owner,
                'nonblank': sum(1 for i in range(s, e + 1) if L[i - 1].strip()), 'pages': pages})
print(json.dumps(out))
