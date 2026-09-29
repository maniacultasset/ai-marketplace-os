#!/usr/bin/env python3
"""Build the Word edition of BACKEND-ARCHITECTURE.md: chapter 7 is generated from DATABASE.md.

Usage: db_chapter.py <BACKEND-ARCHITECTURE.md> <DATABASE.md> <out.md>

The chapter keeps what a reader needs to see how the data fits together: the database choice, the
big picture, tenant isolation, each domain's plain-English summary and diagrams, the lifecycles, the
key flows, data lifecycle and scaling. Column-level tables stay in the Database Design reference
(DATABASE.md). The .md files are the source of truth; this output is a build artifact.
"""
import re
import sys

arch_path, db_path, out_path = sys.argv[1:4]
arch = open(arch_path, encoding='utf-8').read()
db = open(db_path, encoding='utf-8').read().split('\n')
START, END = '<!-- DB-CHAPTER-STUB-START', '<!-- DB-CHAPTER-STUB-END -->'
if START not in arch or END not in arch:
    sys.exit('BACKEND-ARCHITECTURE.md lost its DB-CHAPTER-STUB markers')


def sections(lines, level):
    """Split lines into (heading, body_lines) at headings of exactly `level` #'s."""
    out, cur = [], None
    for ln in lines:
        m = re.match(r'^(#+) ', ln)
        if m and len(m.group(1)) == level:
            cur = [ln, []]
            out.append(cur)
        elif cur is not None:
            cur[1].append(ln)
    return out


def strip_number(heading):
    return re.sub(r'^#+\s+(\d+(\.\d+)*\.?\s+)?', '', heading).strip()


def fences(body):
    """Yield (start, end) index pairs of fenced code blocks."""
    i = 0
    while i < len(body):
        if body[i].startswith('```'):
            j = i + 1
            while j < len(body) and not body[j].startswith('```'):
                j += 1
            yield i, j
            i = j + 1
        else:
            i += 1


def lead_and_diagrams(body):
    """First paragraph (or the 'Plain English.' paragraph) plus every mermaid block, each diagram
    introduced by the #### heading it sits under."""
    out = []
    fenced = {k for s, e in fences(body) for k in range(s, e + 1)}
    para = next((k for k, l in enumerate(body) if l.startswith('**Plain English.**')), None)
    if para is None:  # else the first prose paragraph that is not inside a code block
        para = next((k for k, l in enumerate(body)
                     if k not in fenced and l.strip() and not l.startswith(('#', '|', '<!--'))), None)
    if para is not None:
        k = para
        while k < len(body) and body[k].strip():
            out.append(body[k]); k += 1
        out.append('')
    tables = []
    for l in body:
        name = re.search(r'`([^`]+)`', l) if l.startswith('#### ') else None
        if name: tables.append(name.group(1))
    for s, e in fences(body):
        if not body[s].strip().startswith('```mermaid'):
            continue
        head = next((body[k] for k in range(s, -1, -1) if body[k].startswith('#### ')), None)
        if head:  # a diagram that belongs to one table: label it with that table
            out.append('**' + re.sub(r'^#+\s+', '', head).split(' — ')[0].strip() + '**')
            out.append('')
        out.extend(body[s:e + 1]); out.append('')
    if tables:
        out.append('*Tables in this domain:* ' + ', '.join(f'`{t}`' for t in dict.fromkeys(tables)) + '.')
        out.append('')
    return out


top = sections(db, 2)
by_title = {strip_number(h).lower(): (h, b) for h, b in top}


def find(prefix):
    for t, v in by_title.items():
        if t.startswith(prefix.lower()):
            return v
    sys.exit(f'DATABASE.md has no "## {prefix}" section')


ch = [
    '*This chapter is generated from `docs/architecture/DATABASE.md` for the Word edition. It shows the',
    'shape of the data and how it connects. Every column, constraint and index is in the companion',
    'document "Database Design (reference)". Section references (§n) inside this chapter point there.*',
    '',
]
n = 0
for prefix in ('Which database', 'The big picture', 'Tenant isolation'):
    h, b = find(prefix); n += 1
    ch += [f'### 7.{n} {strip_number(h)}', ''] + b
h, b = find('Domains'); n += 1
ch += [f'### 7.{n} {strip_number(h)}', '']
intro = []
for l in b:
    if l.startswith('### '): break
    intro.append(l)
ch += intro
for k, (sh, sb) in enumerate(sections(b, 3), 1):
    ch += [f'#### 7.{n}.{k} {strip_number(sh)}', ''] + lead_and_diagrams(sb)
for prefix in ('Lifecycles', 'Key flows'):
    h, b = find(prefix); n += 1
    ch += [f'### 7.{n} {strip_number(h)}', '']
    for k, (sh, sb) in enumerate(sections(b, 3), 1):
        ch += [f'#### 7.{n}.{k} {strip_number(sh)}', ''] + lead_and_diagrams(sb)
for prefix in ('Data lifecycle', 'Scaling path'):
    h, b = find(prefix); n += 1
    ch += [f'### 7.{n} {strip_number(h)}', ''] + b

a, rest = arch.split(START, 1)
_, z = rest.split(END, 1)
open(out_path, 'w', encoding='utf-8').write(a + '\n'.join(ch) + '\n' + z)
print(f'wrote {out_path}: chapter 7 = {n} sections, {sum(1 for l in ch if l.startswith("```mermaid"))} diagrams')
