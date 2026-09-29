export const meta = {
  name: 'build-step',
  description: 'Build an approved code plan with the mkos team: implement test-first -> gates green -> e2e test + code/security review -> verify findings per file -> fix (max 3 rounds) -> final gates -> report',
  whenToUse: 'Only after line 1 of docs/plans/<id>/CODE-PLAN.md reads "Status: approved (...)". Run from a session started after CLAUDE.md became @AGENTS.md. The launching session owns docs/STATE.md. Args: {id: "s1", branch?: "feat/s1-monorepo"}',
  phases: [
    { title: 'Implement', detail: 'mkos-implementer preflight + test-first build' },
    { title: 'Test', detail: 'mkos-e2e-tester verifies criteria and attacks the change' },
    { title: 'Review', detail: 'code + security reviewers in parallel' },
    { title: 'Verify', detail: 'one mkos-verifier per file refutes high/medium findings' },
    { title: 'Fix', detail: 'gate failures first, then confirmed findings (max 3 rounds total)' },
    { title: 'Report', detail: 'final gate run + docs/plans/<id>/REPORT.md' },
  ],
}

// Loop rules are specified in docs/plans/README.md ("Loop rules"); this script mirrors them.
if (!args || !args.id) throw new Error('Pass args like {id: "s1"} (optional: branch)')
const id = String(args.id)
const dir = `docs/plans/${id}`
const branchNote = args.branch ? ` Expected branch: ${args.branch}.` : ''
const MAX_FIX_ROUNDS = 3   // invariant 9: bounded, then blocked -> the user decides

const GATES = { type: 'array', items: { type: 'object', properties: {
  name: { type: 'string' }, result: { type: 'string', enum: ['pass', 'fail', 'skipped'] }, evidence: { type: 'string' },
}, required: ['name', 'result', 'evidence'] } }
const IMPL = {
  type: 'object',
  properties: {
    preflight: { type: 'string', enum: ['ok', 'not-approved', 'stale-session', 'wrong-branch'] },
    tasks_done: { type: 'array', items: { type: 'string' } },
    tasks_blocked: { type: 'array', items: { type: 'string' } },
    files_changed: { type: 'array', items: { type: 'string' } },
    gates: GATES,
    deviations: { type: 'array', items: { type: 'string' } },
  },
  required: ['preflight', 'tasks_done', 'tasks_blocked', 'files_changed', 'gates', 'deviations'],
}
const ISSUE = { type: 'object', properties: {
  source: { type: 'string' }, severity: { type: 'string', enum: ['high', 'medium', 'low'] },
  location: { type: 'string' }, problem: { type: 'string' }, repro_or_evidence: { type: 'string' }, fix: { type: 'string' },
}, required: ['source', 'severity', 'location', 'problem', 'repro_or_evidence', 'fix'] }
const ISSUES = { type: 'object', properties: { issues: { type: 'array', items: ISSUE }, notes: { type: 'string' } }, required: ['issues', 'notes'] }
const TEST = { type: 'object', properties: {
  issues: { type: 'array', items: ISSUE }, notes: { type: 'string' },
  tests_added: { type: 'array', items: { type: 'string' } }, untested: { type: 'array', items: { type: 'string' } },
}, required: ['issues', 'notes', 'tests_added', 'untested'] }
const VERDICTS = { type: 'object', properties: { verdicts: { type: 'array', items: { type: 'object', properties: {
  index: { type: 'integer' }, verdict: { type: 'string', enum: ['confirmed', 'refuted', 'uncertain'] },
  evidence: { type: 'string' }, better_fix: { type: 'string' },
}, required: ['index', 'verdict', 'evidence', 'better_fix'] } } }, required: ['verdicts'] }

const uniq = xs => Array.from(new Set(xs))
const trunc = (s, n) => (s && s.length > n ? s.slice(0, n) + '…' : s || '')
const fileOf = loc => String(loc || '').split(':')[0]
const key = c => fileOf(c.location) + '|' + String(c.problem).toLowerCase().replace(/\W+/g, ' ').slice(0, 60)
// An empty gate list counts as red: "no gates run" is never "green".
const failingGates = r => !r ? [] : (!r.gates || !r.gates.length)
  ? [{ name: 'gates', result: 'fail', evidence: 'the agent reported no gate runs' }]
  : r.gates.filter(g => g.result === 'fail')
const gateItems = gs => gs.map(g => ({ source: 'gate', severity: 'high', location: g.name, problem: 'gate failing', repro_or_evidence: trunc(g.evidence, 600), fix: '' }))
const slim = o => ({ source: o.source, severity: o.severity, location: o.location, problem: o.problem,
  fix: o.better_fix || o.fix, evidence: trunc(o.repro_or_evidence, 600), verdict: o.verdict })

phase('Implement')
const impl = await agent(`Preflight (see your role), then execute ${dir}/CODE-PLAN.md.${branchNote}`,
  { agentType: 'mkos-implementer', label: 'implement', phase: 'Implement', schema: IMPL })
if (!impl) return { id, status: 'implementer-failed', note: 'Partial changes may exist; inspect the working tree before re-running.' }
if (impl.preflight !== 'ok') return { id, status: impl.preflight, impl }

let last = impl
const allFiles = impl.files_changed.slice()
let scope = impl.files_changed.slice()
let gatesRed = failingGates(impl)
const blocked = impl.tasks_blocked.slice()
const incomplete = []
const untested = []
const suggestions = []
const seenLow = new Set()
const history = []
let open = []
let fixes = 0
let round = 0

while (true) {
  if (gatesRed.length) {
    // Gate failures are fixed before any tester/reviewer time is spent.
    if (fixes >= MAX_FIX_ROUNDS) { open = open.map(o => ({ ...o, problem: o.problem + ' (fix attempted; not re-reviewed because gates went red)' })).concat(gateItems(gatesRed)); break }
    phase('Fix')
    fixes++
    const gfix = await agent(`Fix round ${fixes} of ${MAX_FIX_ROUNDS} for ${dir}. First append a "gate-fix" block to ${dir}/LOOP.md (docs/plans/README.md format). Then make these failing gates pass without weakening any test or guard, and re-run all gates. List anything you could not fix in tasks_blocked, with the reason.\n${JSON.stringify(gatesRed.map(g => ({ name: g.name, evidence: trunc(g.evidence, 600) })), null, 1)}`,
      { agentType: 'mkos-implementer', label: `fix-gates:${fixes}`, phase: 'Fix', schema: IMPL })
    if (!gfix) { incomplete.push('a gate-fix agent returned nothing'); open = open.concat(gateItems(gatesRed)); break }
    last = gfix
    scope = uniq([...scope, ...gfix.files_changed])
    allFiles.push(...gfix.files_changed)
    blocked.push(...gfix.tasks_blocked)
    gatesRed = failingGates(gfix)
    continue
  }

  round++
  const lite = round > 1 ? { effort: 'medium' } : {}
  const scopeNote = round === 1
    ? `Changed files: ${JSON.stringify(uniq(allFiles))}.`
    : `RE-CHECK ROUND. Look only at these files and the locations of the previous findings: ${JSON.stringify(uniq(scope))}. Previous findings: ${JSON.stringify(open.map(o => ({ location: o.location, problem: o.problem })))}.`

  phase('Test')
  const test = await agent(`Round ${round}. Test ${dir} against ${dir}/PLAN.md's acceptance criteria and attack it. ${scopeNote} Report issues with source "tester". List the test files you added in tests_added, and any criteria you could not exercise in untested.`,
    { agentType: 'mkos-e2e-tester', label: `test:r${round}`, phase: 'Test', schema: TEST, ...lite })
  phase('Review')
  const reviews = await parallel([
    () => agent(`Round ${round}. Review the change set for ${dir} against PLAN.md and CODE-PLAN.md. ${scopeNote} Report issues with source "code-review".`,
      { agentType: 'mkos-code-reviewer', label: `review:code:r${round}`, phase: 'Review', schema: ISSUES, ...lite }),
    () => agent(`Round ${round}. Security-review the change set for ${dir}. ${scopeNote} Report issues with source "security-review".`,
      { agentType: 'mkos-security-reviewer', label: `review:security:r${round}`, phase: 'Review', schema: ISSUES, ...lite }),
  ])
  const ran = [['tester', test], ['code-review', reviews[0]], ['security-review', reviews[1]]]
  const missing = ran.filter(([, r]) => !r).map(([n]) => n)
  if (missing.length) {
    // A missing result is never "clean": stop, and report as blocked.
    incomplete.push(`round ${round}: no result from ${missing.join(', ')}`)
    log(`Round ${round}: no result from ${missing.join(', ')}; stopping as blocked`)
    break
  }
  allFiles.push(...test.tests_added)
  untested.push(...test.untested)

  // Dedupe across the three sources, and merge the source labels.
  const byKey = new Map()
  for (const c of ran.flatMap(([, r]) => r.issues)) {
    const k = key(c)
    if (byKey.has(k)) byKey.get(k).source += '+' + c.source
    else byKey.set(k, { ...c })
  }
  const candidates = [...byKey.values()]
  for (const c of candidates.filter(c => c.severity === 'low')) {
    if (!seenLow.has(key(c))) { seenLow.add(key(c)); suggestions.push(slim(c)) }
  }
  const toVerify = candidates.filter(c => c.severity !== 'low')

  // One verifier per file, covering all of that file's high/medium findings. Retry once on null.
  phase('Verify')
  const groups = {}
  toVerify.forEach(f => { (groups[fileOf(f.location)] = groups[fileOf(f.location)] || []).push(f) })
  const verified = (await parallel(Object.entries(groups).map(([file, fs], gi) => async () => {
    const prompt = `Try to refute each finding below about ${dir} (all in ${file}). Return one verdict per finding, keyed by its index.\n${JSON.stringify(fs.map((f, i) => ({ index: i, ...slim(f) })), null, 1)}`
    const opts = { agentType: 'mkos-verifier', label: `verify:r${round}:${gi}`, phase: 'Verify', schema: VERDICTS, effort: 'medium' }
    const v = (await agent(prompt, opts)) || (await agent(prompt, { ...opts, label: `${opts.label}:retry` }))
    return fs.map((f, i) => {
      const hit = v && v.verdicts.find(x => x.index === i)
      return hit ? { ...f, ...hit } : { ...f, verdict: 'uncertain', evidence: 'verifier returned nothing', better_fix: '' }
    })
  }))).filter(Boolean).flat()

  const confirmed = verified.filter(v => v.verdict === 'confirmed')
  const uncertainHigh = verified.filter(v => v.verdict === 'uncertain' && v.severity === 'high')
  suggestions.push(...verified.filter(v => v.verdict === 'uncertain' && v.severity === 'medium')
    .map(v => ({ ...slim(v), problem: '[unverified medium] ' + v.problem })))
  open = confirmed.concat(uncertainHigh)
  history.push({ round, found: candidates.length, verified: toVerify.length, confirmed: confirmed.length,
    uncertain_high: uncertainHigh.length, refuted: verified.filter(v => v.verdict === 'refuted').length })
  log(`Round ${round}: ${candidates.length} found (deduped), ${toVerify.length} verified in ${Object.keys(groups).length} file groups, ${confirmed.length} confirmed, ${uncertainHigh.length} uncertain-high`)
  if (!open.length || fixes >= MAX_FIX_ROUNDS) break

  phase('Fix')
  fixes++
  const fix = await agent(`Fix round ${fixes} of ${MAX_FIX_ROUNDS} for ${dir}. First append this round's "fix" block to ${dir}/LOOP.md (docs/plans/README.md format), using the round data below. Then fix ONLY these findings. Give each one a regression test that fails without the fix; if ${dir}/repro/ has its repro, move it into tests/. Re-run the gates. List anything you did not fix in tasks_blocked, with the reason.\n\nROUND: ${JSON.stringify(history[history.length - 1])}\nFINDINGS:\n${JSON.stringify(open.map(slim), null, 1)}`,
    { agentType: 'mkos-implementer', label: `fix:${fixes}`, phase: 'Fix', schema: IMPL })
  if (!fix) { incomplete.push('a fix agent returned nothing'); break }
  last = fix
  scope = uniq([...fix.files_changed, ...open.map(o => fileOf(o.location))])
  allFiles.push(...fix.files_changed)
  blocked.push(...fix.tasks_blocked)
  gatesRed = failingGates(fix)
}

phase('Report')
// If nothing is open, run the gates once more on the final tree: the tester may have added tests after the last gate run.
let finalGates = null
if (!open.length && !incomplete.length) {
  const fg = await agent(`Run every required check that exists for ${dir} (AGENTS.md §6). Change nothing. Report each check as pass/fail/skipped, with the last output lines as evidence.`,
    { label: 'final-gates', phase: 'Report', effort: 'low', schema: { type: 'object', properties: { gates: GATES }, required: ['gates'] } })
  if (!fg) incomplete.push('the final gate run returned nothing')
  else {
    finalGates = fg.gates
    if (fg.gates.some(g => g.result === 'fail') || !fg.gates.some(g => g.result === 'pass')) incomplete.push('the final gate run is not green')
  }
}
const gatesNow = finalGates || last.gates || []
const skipped = gatesNow.filter(g => g.result === 'skipped').map(g => g.name)
const status = (open.length || blocked.length || incomplete.length) ? 'blocked'
  : (skipped.length || untested.length) ? 'ready-with-skips' : 'ready-for-merge-review'

const rep = await agent(`Write ${dir}/REPORT.md (under 120 lines) and append a "final" block to ${dir}/LOOP.md. Edit no other file.

REPORT.md sections:
- Status: ${status}
- What was built
- Gates (with evidence)
- Review rounds (a table)
- Open findings / blocked tasks / incomplete checks (if blocked: exactly what the user must decide)
- Skipped gates and untested criteria
- Low and unverified-medium suggestions
- Deviations (read ${dir}/DEVIATIONS.md if it exists)
- Files changed

End with the user's next step: merge review, or unblock.

DATA:
${JSON.stringify({ status, gates: gatesNow, tasks_done: impl.tasks_done, tasks_blocked: blocked, incomplete, skipped, untested, deviations: impl.deviations, files: uniq(allFiles), history, open: open.map(slim), suggestions }, null, 1)}`,
  { label: 'report', phase: 'Report', model: 'haiku', effort: 'low' })

return {
  id, status, review_rounds: round, fix_rounds: fixes, open_count: open.length, blocked, incomplete,
  report: rep ? `${dir}/REPORT.md` : null, open: rep ? undefined : open.map(slim),
  next: 'Launching session: update docs/STATE.md (step status, session log, Active: none); then the user reviews REPORT.md for merge.',
}
