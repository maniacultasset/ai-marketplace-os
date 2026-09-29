export const meta = {
  name: 'plan-step',
  description: 'Plan one build step with the mkos team: planner (CONTEXT + PLAN) -> code planner (CODE-PLAN) -> code + security critique -> routed revisions, for user approval',
  whenToUse: 'Before implementing any build step (S1-S14) or feature. Run only from a session started after CLAUDE.md became @AGENTS.md. The launching session owns docs/STATE.md (Active marker + log). Args: {step: "S1", id?: "s1", goal?: "extra instructions"}',
  phases: [
    { title: 'Plan', detail: 'mkos-planner writes CONTEXT.md + PLAN.md' },
    { title: 'Code plan', detail: 'mkos-code-planner writes CODE-PLAN.md (line 1: Status: draft)' },
    { title: 'Critique', detail: 'code + security reviewers critique both plans' },
    { title: 'Revise', detail: 'findings routed to the owning planner; low ones only logged' },
  ],
}

if (!args || !args.step) throw new Error('Pass args like {step: "S1"} (optional: id, goal)')
const step = String(args.step)
const id = args.id ? String(args.id) : step.toLowerCase()
const dir = `docs/plans/${id}`
const goal = args.goal ? `\nExtra instructions from the user: ${args.goal}` : ''
const stale = r => typeof r === 'string' && r.includes('STALE_SESSION')

phase('Plan')
const plan = await agent(`Plan build step ${step}. Write ${dir}/CONTEXT.md first, then ${dir}/PLAN.md.${goal}`,
  { agentType: 'mkos-planner', label: 'planner', phase: 'Plan' })
if (!plan) throw new Error('mkos-planner returned nothing')
if (stale(plan)) throw new Error('Stale session: restart Claude Code so agents load the current AGENTS.md, then re-run plan-step')

phase('Code plan')
const code = await agent(`Read ${dir}/CONTEXT.md and ${dir}/PLAN.md, then write ${dir}/CODE-PLAN.md (line 1 exactly "Status: draft").${goal}`,
  { agentType: 'mkos-code-planner', label: 'code-planner', phase: 'Code plan' })
if (!code) throw new Error('mkos-code-planner returned nothing')

const CRIT = {
  type: 'object',
  properties: {
    findings: { type: 'array', items: { type: 'object', properties: {
      file: { type: 'string', description: 'PLAN.md, CODE-PLAN.md or CONTEXT.md' }, section: { type: 'string' },
      severity: { type: 'string', enum: ['high', 'medium', 'low'] },
      problem: { type: 'string' }, fix: { type: 'string' },
    }, required: ['file', 'section', 'severity', 'problem', 'fix'] } },
  },
  required: ['findings'],
}

phase('Critique')
const crits = await parallel([
  () => agent(`PLAN-CRITIQUE MODE for ${dir} (no code exists yet). Read CONTEXT.md, PLAN.md and CODE-PLAN.md. Apply the "Plan critique" code list in docs/plans/README.md, and also check CONTEXT.md for wrong or stale rows.`,
    { agentType: 'mkos-code-reviewer', label: 'critique:code', phase: 'Critique', schema: CRIT }),
  () => agent(`PLAN-CRITIQUE MODE for ${dir} (no code exists yet). Read CONTEXT.md, PLAN.md and CODE-PLAN.md. Apply the "Plan critique" security list in docs/plans/README.md.`,
    { agentType: 'mkos-security-reviewer', label: 'critique:security', phase: 'Critique', schema: CRIT }),
])
const critique_failed = ['code', 'security'].filter((_, i) => !crits[i])
if (critique_failed.length) log(`Critique missing from: ${critique_failed.join(', ')}`)
const findings = crits.filter(Boolean).flatMap(c => c.findings)
const lows = findings.filter(f => f.severity === 'low')
const actionable = findings.filter(f => f.severity !== 'low')
const toPlanner = actionable.filter(f => /(^|\/)(PLAN|CONTEXT)\.md$/.test(f.file))
const toCoder = actionable.filter(f => !toPlanner.includes(f))
log(`${findings.length} critique findings: ${toPlanner.length} for the planner, ${toCoder.length} for the code planner, ${lows.length} low (logged only)`)

phase('Revise')
let revise_failed = false
if (toPlanner.length) {
  const r = await agent(`REVISE MODE for ${dir}. Using Edit, apply the findings you agree with to PLAN.md / CONTEXT.md. Log each rejection with its reason in PLAN.md's Critique log.\n\nFINDINGS:\n${JSON.stringify(toPlanner, null, 1)}`,
    { agentType: 'mkos-planner', label: 'revise:plan', phase: 'Revise' })
  if (!r) revise_failed = true
}
if (toCoder.length || toPlanner.length) {
  const r = await agent(`REVISE MODE for ${dir}. Using Edit:
- Apply the findings you agree with to CODE-PLAN.md.
- Re-sync it with any PLAN.md changes.
- Log each rejection with its reason in CODE-PLAN.md's Critique log.
- Append the LOW findings to the Critique log only.
- Line 1 stays "Status: draft".

FINDINGS:
${JSON.stringify(toCoder, null, 1)}

LOW:
${JSON.stringify(lows, null, 1)}`,
    { agentType: 'mkos-code-planner', label: 'revise:code', phase: 'Revise' })
  if (!r) revise_failed = true
}

const redo = critique_failed.length || revise_failed
return {
  id, dir, critique_count: findings.length, low_findings: lows, critique_failed, revise_failed,
  next: `${redo ? 'Re-run the critique first. ' : ''}The user reviews ${dir}/PLAN.md and CODE-PLAN.md, answers the blocking questions and fills in "Approvals required", then approves line 1 of CODE-PLAN.md. Then run build-step with {id: "${id}"}. Launching session: update docs/STATE.md.`,
}
