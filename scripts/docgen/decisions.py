#!/usr/bin/env python3
"""Generate the "Decisions needed" page that opens each Word document.

Usage: decisions.py <repo root> <out.md>
Sources (single source of truth, never copied by hand): docs/adr/README.md (proposed ADRs),
docs/review/TEAM-QUESTIONS.md (open T-rows) and docs/architecture/DATABASE.md §11.1 (engineering
decisions). Also prints a regex of the proposed ADR ids, which build.sh passes to the highlighter.
"""
import re
import sys

root, out_path = sys.argv[1:3]


def cells(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


def short(text, n=160):
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= n else text[:n].rsplit(' ', 1)[0] + ' …'


adrs = []
for line in open(f'{root}/docs/adr/README.md', encoding='utf-8'):
    c = cells(line) if line.startswith('| 0') else None
    if c and 'proposed' in c[2].lower():
        adrs.append((c[0], c[1], c[2]))

groups, section, header = {}, None, None
for line in open(f'{root}/docs/review/TEAM-QUESTIONS.md', encoding='utf-8'):
    if line.startswith('## '):
        section, header = line[3:].strip(), None
    elif line.startswith('| ID'):
        header = [h.lower() for h in cells(line)]
    elif re.match(r'^\| T\d+ ', line) and header:
        c = dict(zip(header, cells(line)))
        status = next((v for k, v in c.items() if k.startswith('status')), '')
        if status.lower() not in ('open', ''):
            continue
        default = next((v for k, v in c.items() if 'default' in k), '')
        groups.setdefault(section, []).append((c['id'], c.get('topic', ''), default))

db_open = 0
in_11 = False
for line in open(f'{root}/docs/architecture/DATABASE.md', encoding='utf-8'):
    if line.startswith('### 11.1'): in_11 = True; continue
    if in_11 and line.startswith('### '): break
    if in_11 and re.match(r'^\| *\d+ *\|', line): db_open += 1

total_t = sum(len(v) for v in groups.values())
md = ['# Decisions needed', '', '## Decisions needed (read this first)', '',
      '> ⚑ **Amber highlight marks every open decision in this document**: a `[Proposed]` item, a proposed ADR, '
      'a team question (T-number) or anything provisional. Until someone decides, we build the default shown. '
      'Answers go into `docs/review/TEAM-QUESTIONS.md`, and the Tech Lead records them.', '',
      f'**At a glance:** {len(adrs)} proposed ADRs, {total_t} open team questions, and {db_open} engineering '
      'decisions in the Database Design §11.1.', '']
if adrs:
    md += ['### Proposed ADRs: the team confirms or overrules', '',
           '| ADR | Decision | Status |', '|---|---|---|']
    md += [f'| ADR-{n} | {t} | {short(s, 120)} |' for n, t, s in adrs]
    md += ['']
for sec, rows in groups.items():
    md += [f'### {sec}', '', '| ID | Question | Default we build until decided |', '|---|---|---|']
    md += [f'| {i} | {short(t, 110)} | {short(d, 170)} |' for i, t, d in rows]
    md += ['']
open(out_path, 'w', encoding='utf-8').write('\n'.join(md) + '\n')
print('|'.join(f'ADR-{n}' for n, _, _ in adrs))
