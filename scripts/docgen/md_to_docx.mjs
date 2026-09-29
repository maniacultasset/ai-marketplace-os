// md_to_docx.mjs: turn a project .md (with ```mermaid``` blocks) into a team-shareable .docx.
// Diagrams are rendered to PNG with mermaid-cli (using the local Chrome) and embedded as images.
//
// Usage:
//   DOCGEN_DEPS=<dir with node_modules/docx + marked> \
//   MMDC=<path to mmdc> PPTR_CONFIG=<puppeteer json> \
//   node scripts/docgen/md_to_docx.mjs <input.md> <output.docx> \
//     [--title T] [--subtitle S] [--version V] [--extra more.md ...]
// The .md is the source of truth (AGENTS.md §8). Regenerate the .docx; never edit it by hand.
import { createRequire } from 'node:module'
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { join, dirname, basename } from 'node:path'
import { createHash } from 'node:crypto'

const deps = process.env.DOCGEN_DEPS
if (!deps) throw new Error('Set DOCGEN_DEPS to a folder whose node_modules has docx and marked')
const require = createRequire(join(deps, 'package.json'))
const D = require('docx')
const { marked } = require('marked')
const MMDC = process.env.MMDC
const PPTR = process.env.PPTR_CONFIG

// ---- args ----
const argv = process.argv.slice(2)
const inputs = [argv[0]]; const out = argv[1]
const opt = { title: 'Document', subtitle: '', version: '', drop: [], flags: [], legend: '' }
for (let i = 2; i < argv.length; i++) {
  if (argv[i] === '--title') opt.title = argv[++i]
  else if (argv[i] === '--subtitle') opt.subtitle = argv[++i]
  else if (argv[i] === '--version') opt.version = argv[++i]
  else if (argv[i] === '--extra') inputs.push(argv[++i])
  else if (argv[i] === '--drop') opt.drop.push(argv[++i].toLowerCase())   // leave out a section (heading text contains this)
  else if (argv[i] === '--flag') opt.flags.push(argv[++i])                 // regex: highlight matches as "decision needed"
  else if (argv[i] === '--legend') opt.legend = argv[++i]                  // one line on the cover explaining the highlight
}
if (!inputs[0] || !out) throw new Error('usage: md_to_docx.mjs <input.md> <output.docx> [--title ..]')

// ---- look ----
const FONT = 'Calibri', MONO = 'Menlo'
const BLUE = '1D4ED8', DARK = '0F172A', MUTED = '475569', HEAD_FILL = 'DBEAFE', CODE_FILL = 'F1F5F9', NOTE_FILL = 'FEF9C3'
const PAGE_W = 11906, PAGE_H = 16838, MARGIN = 1080            // A4, 0.75in margins (DXA)
const CONTENT_W = PAGE_W - 2 * MARGIN                           // DXA
const MAX_IMG_W = Math.floor(CONTENT_W / 1440 * 96)             // px at 96 dpi
const MAX_IMG_H = Math.floor((PAGE_H - 2 * MARGIN) / 1440 * 96) - 90
const LAND_IMG_W = Math.floor((PAGE_H - 2 * MARGIN) / 1440 * 96)
const LAND_IMG_H = Math.floor((PAGE_W - 2 * MARGIN) / 1440 * 96) - 90

// ---- mermaid -> png ----
const imgDir = process.env.DOCGEN_CACHE || join(dirname(out), '.docgen-cache')   // build.sh sets scripts/docgen/.cache/img
mkdirSync(imgDir, { recursive: true })
function pngSize(buf) { return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) } }
function renderMermaid(code) {
  const img = renderOnce(code)
  // A long left-to-right chain shrinks to unreadable text at page width; stack it top-down instead.
  const lr = /^(\s*(?:flowchart|graph)\s+)(LR|RL)\b/m
  if (img.w / img.h > 6 && lr.test(code)) return renderOnce(code.replace(lr, '$1TB'))
  return img
}
function renderOnce(code) {
  const h = createHash('sha1').update(code).digest('hex').slice(0, 12)
  const mmd = join(imgDir, `${h}.mmd`), png = join(imgDir, `${h}.png`)
  if (!existsSync(png)) {
    writeFileSync(mmd, code)
    const args = ['-i', mmd, '-o', png, '-b', 'white', '-s', '3', '-w', '1400']
    if (PPTR) args.push('-p', PPTR)
    execFileSync(MMDC, args, { stdio: 'pipe' })
  }
  const buf = readFileSync(png)
  return { buf, ...pngSize(buf) }
}

// ---- inline tokens -> runs ----
const FLAG_RE = opt.flags.length ? new RegExp(opt.flags.map(f => `(?:${f})`).join('|'), 'g') : null
const FLAG_STYLE = { bold: true, color: '92400E', shading: { type: D.ShadingType.CLEAR, fill: 'FDE68A', color: 'auto' } }
function textRuns(text, style) {                                  // plain text, with open decisions highlighted amber
  if (!FLAG_RE) return [new D.TextRun({ text, ...style })]
  const res = []; let last = 0
  for (const m of text.matchAll(FLAG_RE)) {
    if (m.index > last) res.push(new D.TextRun({ text: text.slice(last, m.index), ...style }))
    res.push(new D.TextRun({ text: m[0], ...style, ...FLAG_STYLE }))
    last = m.index + m[0].length
  }
  if (last < text.length) res.push(new D.TextRun({ text: text.slice(last), ...style }))
  return res
}
function runs(tokens, style = {}) {
  const outRuns = []
  for (const t of tokens || []) {
    switch (t.type) {
      case 'strong': outRuns.push(...runs(t.tokens, { ...style, bold: true })); break
      case 'em': outRuns.push(...runs(t.tokens, { ...style, italics: true })); break
      case 'del': outRuns.push(...runs(t.tokens, { ...style, strike: true })); break
      case 'codespan': {
        const code = decode(t.text)
        const flagged = FLAG_RE && code.match(FLAG_RE)?.[0] === code
        outRuns.push(new D.TextRun({ text: code, font: MONO, size: 18, shading: { type: D.ShadingType.CLEAR, fill: CODE_FILL, color: 'auto' }, ...style, ...(flagged ? FLAG_STYLE : {}) }))
        break
      }
      case 'link': outRuns.push(new D.ExternalHyperlink({ link: t.href, children: runs(t.tokens, { ...style, color: BLUE, underline: {} }) })); break
      case 'br': outRuns.push(new D.TextRun({ break: 1 })); break
      case 'html': break // comments / placeholders are not shown
      case 'text': case 'escape':
        if (t.tokens && t.tokens.length) outRuns.push(...runs(t.tokens, style))
        else outRuns.push(...textRuns(decode(t.text), style))
        break
      default: if (t.text) outRuns.push(...textRuns(decode(t.text), style))
    }
  }
  return outRuns
}
function decode(s) { return String(s).replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'") }
function plain(tokens) { return (tokens || []).map(t => t.tokens ? plain(t.tokens) : decode(t.text || '')).join('') }

// ---- blocks ----
let figureNo = 0, lastHeading = '', headingNo = 0
const toc = []                                                  // {id, text, depth} for the static contents list
const HEADINGS = [null, D.HeadingLevel.TITLE, D.HeadingLevel.HEADING_1, D.HeadingLevel.HEADING_2, D.HeadingLevel.HEADING_3, D.HeadingLevel.HEADING_4, D.HeadingLevel.HEADING_5]
let skipDepth = null                                            // inside a --drop section until a heading at this depth or higher
function blocks(tokens, level = 0) {
  const outB = []
  for (const t of tokens) {
    if (t.type === 'heading') {
      if (skipDepth !== null && t.depth <= skipDepth) skipDepth = null
      if (skipDepth === null && opt.drop.some(d => plain(t.tokens).toLowerCase().includes(d))) skipDepth = t.depth
    }
    if (skipDepth !== null) continue
    switch (t.type) {
      case 'heading': {
        if (t.depth === 1) break // the document title is on the cover
        lastHeading = plain(t.tokens)
        const id = `h${++headingNo}`
        if (t.depth <= 3) toc.push({ id, text: lastHeading, depth: t.depth })
        outB.push(new D.Paragraph({ heading: HEADINGS[t.depth], keepNext: true, children: [new D.Bookmark({ id, children: runs(t.tokens) })] }))
        break
      }
      case 'paragraph': outB.push(new D.Paragraph({ children: runs(t.tokens), spacing: { after: 120 } })); break
      case 'space': break
      case 'hr': outB.push(new D.Paragraph({ border: { bottom: { style: D.BorderStyle.SINGLE, size: 6, color: 'CBD5E1', space: 1 } }, spacing: { after: 120 } })); break
      case 'blockquote':
        for (const b of t.tokens) {
          const inner = b.type === 'paragraph' ? runs(b.tokens) : [new D.TextRun(plain(b.tokens || []) || b.text || '')]
          outB.push(new D.Paragraph({ children: inner, shading: { type: D.ShadingType.CLEAR, fill: NOTE_FILL, color: 'auto' }, border: { left: { style: D.BorderStyle.SINGLE, size: 18, color: 'A16207', space: 8 } }, spacing: { after: 120 }, indent: { left: 240 } }))
        }
        break
      case 'list': outB.push(...list(t, level)); break
      case 'table': outB.push(table(t), new D.Paragraph({ spacing: { after: 120 } })); break
      case 'code':
        if ((t.lang || '').trim() === 'mermaid') outB.push(...figure(t.text))
        else for (const line of t.text.split('\n')) outB.push(new D.Paragraph({ children: [new D.TextRun({ text: line || ' ', font: MONO, size: 17 })], shading: { type: D.ShadingType.CLEAR, fill: CODE_FILL, color: 'auto' }, spacing: { after: 0 } }))
        if ((t.lang || '').trim() !== 'mermaid') outB.push(new D.Paragraph({ spacing: { after: 120 } }))
        break
      case 'html': break
      default: if (t.tokens) outB.push(new D.Paragraph({ children: runs(t.tokens) }))
    }
  }
  return outB
}
function list(t, level) {
  const res = []
  for (const item of t.items) {
    let first = true
    for (const sub of item.tokens) {
      if (sub.type === 'list') { res.push(...list(sub, level + 1)); continue }
      const kids = sub.tokens ? runs(sub.tokens) : [new D.TextRun(decode(sub.text || ''))]
      if (first) res.push(new D.Paragraph({ children: kids, numbering: { reference: t.ordered ? 'num' : 'bullets', level: Math.min(level, 4) }, spacing: { after: 60 } }))
      else res.push(new D.Paragraph({ children: kids, indent: { left: 720 * (level + 1) }, spacing: { after: 60 } }))
      first = false
    }
  }
  return res
}
function table(t) {
  const cols = t.header.length
  const weights = t.header.map((h, i) => Math.max(plain(h.tokens).length, ...t.rows.map(r => Math.min(plain(r[i]?.tokens).length, 60)), 4))
  const sum = weights.reduce((a, b) => a + b, 0)
  let widths = weights.map(w => Math.max(700, Math.floor(CONTENT_W * w / sum)))
  const scale = CONTENT_W / widths.reduce((a, b) => a + b, 0)
  widths = widths.map(w => Math.floor(w * scale)); widths[cols - 1] += CONTENT_W - widths.reduce((a, b) => a + b, 0)
  const cell = (tokens, i, head) => new D.TableCell({
    width: { size: widths[i], type: D.WidthType.DXA },
    shading: head ? { type: D.ShadingType.CLEAR, fill: HEAD_FILL, color: 'auto' } : undefined,
    margins: { top: 60, bottom: 60, left: 90, right: 90 },
    children: [new D.Paragraph({ children: runs(tokens, head ? { bold: true, size: 18 } : { size: 18 }) })],
  })
  return new D.Table({
    width: { size: CONTENT_W, type: D.WidthType.DXA }, columnWidths: widths,
    rows: [new D.TableRow({ tableHeader: true, children: t.header.map((h, i) => cell(h.tokens, i, true)) }),
      ...t.rows.map(r => new D.TableRow({ cantSplit: true, children: r.map((c, i) => cell(c.tokens, i, false)) }))],
  })
}
function figure(code) {
  let img
  try { img = renderMermaid(code) } catch (e) {
    return [new D.Paragraph({ children: [new D.TextRun({ text: `[Diagram failed to render: ${String(e.message).slice(0, 200)}]`, color: 'B91C1C' })] })]
  }
  const cw = img.w / 3, ch = img.h / 3                               // -s 3 => divide back to CSS px
  const kP = Math.min(MAX_IMG_W / cw, MAX_IMG_H / ch, 1.6)
  const kL = Math.min(LAND_IMG_W / cw, LAND_IMG_H / ch, 1.6)
  // A wide diagram whose text would shrink hard on a portrait page gets its own landscape page.
  const landscape = kP < 0.75 && kL > kP * 1.2
  const k = landscape ? kL : kP
  figureNo++
  const paras = [
    new D.Paragraph({ alignment: D.AlignmentType.CENTER, keepNext: true, spacing: { before: 120, after: 60 }, children: [new D.ImageRun({ type: 'png', data: img.buf, transformation: { width: Math.round(cw * k), height: Math.round(ch * k) } })] }),
    new D.Paragraph({ alignment: D.AlignmentType.CENTER, spacing: { after: 200 }, children: [new D.TextRun({ text: `Figure ${figureNo} — ${lastHeading}`, italics: true, color: MUTED, size: 18 })] }),
  ]
  return landscape ? [{ landscape: paras }] : paras
}

// ---- assemble ----
const body = []
for (const f of inputs) body.push(...blocks(marked.lexer(readFileSync(f, 'utf8'))))
const cover = [
  new D.Paragraph({ spacing: { before: 3200 }, children: [new D.TextRun({ text: opt.title, bold: true, size: 56, color: DARK })] }),
  new D.Paragraph({ spacing: { before: 240 }, children: [new D.TextRun({ text: opt.subtitle, size: 30, color: BLUE })] }),
  new D.Paragraph({ spacing: { before: 480 }, children: [new D.TextRun({ text: opt.version, size: 22, color: MUTED })] }),
  new D.Paragraph({ spacing: { before: 240 }, children: [new D.TextRun({ text: 'Generated from the project .md sources; the .md files are the source of truth.', size: 18, italics: true, color: MUTED })] }),
  ...(opt.legend ? [new D.Paragraph({ spacing: { before: 480 }, children: [new D.TextRun({ text: ' ⚑ Decision needed ', ...FLAG_STYLE, size: 20 }), new D.TextRun({ text: '  ' + opt.legend, size: 20, color: DARK })] })] : []),
  new D.Paragraph({ children: [new D.PageBreak()] }),
  // Static, clickable contents: renders the same in Word, Google Docs, Pages and Quick Look (a TOC field would not).
  new D.Paragraph({ children: [new D.TextRun({ text: 'Contents', bold: true, size: 34, color: BLUE })], spacing: { after: 160 } }),
  ...toc.map(e => new D.Paragraph({
    indent: { left: e.depth === 3 ? 400 : 0 }, spacing: { before: e.depth === 2 ? 100 : 0, after: 20 },
    children: [new D.InternalHyperlink({ anchor: e.id, children: [new D.TextRun({ text: e.text, bold: e.depth === 2, size: e.depth === 2 ? 21 : 19, color: e.depth === 2 ? DARK : MUTED })] })],
  })),
]
const PAGE = { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN }
const header = () => new D.Header({ children: [new D.Paragraph({ alignment: D.AlignmentType.RIGHT, children: [new D.TextRun({ text: opt.title, size: 16, color: MUTED })] })] })
const footer = () => new D.Footer({ children: [new D.Paragraph({ alignment: D.AlignmentType.CENTER, children: [new D.TextRun({ children: ['Page ', D.PageNumber.CURRENT, ' of ', D.PageNumber.TOTAL_PAGES], size: 16, color: MUTED })] })] })
const section = (children, landscape) => ({
  properties: { page: { size: { width: PAGE_W, height: PAGE_H, ...(landscape ? { orientation: D.PageOrientation.LANDSCAPE } : {}) }, margin: PAGE } },
  headers: { default: header() }, footers: { default: footer() }, children,
})
const sections = [section(cover, false)]
let run = []
for (const b of body) {
  if (b && b.landscape) { if (run.length) sections.push(section(run, false)); sections.push(section(b.landscape, true)); run = [] }
  else run.push(b)
}
if (run.length) sections.push(section(run, false))
const doc = new D.Document({
  creator: 'AI Marketplace OS backend team', title: opt.title, description: opt.subtitle,
  styles: {
    default: { document: { run: { font: FONT, size: 21, color: DARK } } },
    paragraphStyles: [
      { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { size: 34, bold: true, color: BLUE }, paragraph: { spacing: { before: 360, after: 160 }, outlineLevel: 0 } },
      { id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { size: 27, bold: true, color: DARK }, paragraph: { spacing: { before: 280, after: 120 }, outlineLevel: 1 } },
      { id: 'Heading3', name: 'Heading 3', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { size: 23, bold: true, color: MUTED }, paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 2 } },
    ],
  },
  numbering: { config: [
    { reference: 'bullets', levels: [0, 1, 2, 3, 4].map(l => ({ level: l, format: D.LevelFormat.BULLET, text: ['•', '◦', '▪', '•', '◦'][l], alignment: D.AlignmentType.LEFT, style: { paragraph: { indent: { left: 360 * (l + 1), hanging: 260 } } } })) },
    { reference: 'num', levels: [0, 1, 2, 3, 4].map(l => ({ level: l, format: l % 2 ? D.LevelFormat.LOWER_LETTER : D.LevelFormat.DECIMAL, text: `%${l + 1}.`, alignment: D.AlignmentType.LEFT, style: { paragraph: { indent: { left: 360 * (l + 1), hanging: 300 } } } })) },
  ] },
  sections,
})
const buf = await D.Packer.toBuffer(doc)
writeFileSync(out, buf)
console.log(`wrote ${out} (${(buf.length / 1024).toFixed(0)} KB, ${figureNo} diagrams, from ${inputs.map(f => basename(f)).join(' + ')})`)
