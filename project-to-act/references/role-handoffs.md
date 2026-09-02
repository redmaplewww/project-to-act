# Three-role handoff contracts

Read this reference when assigning work from Lead to Builder, chartering verification, presenting a Builder checkpoint for verification, returning findings, escalating architecture, or recommending a Gate decision.

Roles are task-scoped seats, not permanent identities:

- `lead`: owns objective, scope, architecture, task contract, major decisions, and Gate decisions.
- `builder`: owns implementation, technical execution, self-checks, and evidence-backed escalation.
- `verifier`: owns independent verification, findings, tuning within the charter, and Gate recommendations.

The six handoff types are:

| Type | Legal task states |
|---|---|
| `lead_to_builder.build_contract` | `ready` |
| `lead_to_verifier.verification_charter` | `ready`, `in_progress`, `review` |
| `builder_to_verifier.verification_candidate` | `review` |
| `verifier_to_builder.change_request` | `review` |
| `builder_to_lead.architecture_escalation` | `in_progress`, `blocked` |
| `verifier_to_lead.gate_recommendation` | `review` |

Use `scripts/task_view.py role-preview` to validate the direction, current state, and required payload fields. Preview returns `writes: false` and `authorizationValidated: false`: it does not create a handoff ID, update task state, append an event, prove actor authorization, or imply that the consumer accepted the work.

Required payload fields:

- `build_contract`: objective, valueOrReason, inScope, outOfScope, acceptanceScenarios, architectureConstraints, interfacesAndDependencies, expectedDeliverables, requiredSelfChecks, autonomyBoundary, escalationTriggers.
- `verification_charter`: claimsToVerify, acceptanceScenarios, riskAreas, forbiddenRegressions, boundaryConditions, requiredIndependence, releaseBlockingConditions, allowedTuningScope.
- `verification_candidate`: implementationSummary, changedComponents, changeRefs, designDecisions, deviationsFromContract, selfTestEvidenceRefs, setupOrMigrationSteps, knownLimitations, suspectedRiskAreas, requestedTestFocus.
- `change_request`: overallVerdict, findings.
- `architecture_escalation`: discoveredConstraint, impactedDecisions, affectedScope, optionsConsidered, recommendedOption, costAndRisk, decisionNeeded, workSafeToContinue.
- `gate_recommendation`: verdict, coverageSummary, acceptanceMatrix, blockingFindings, residualRisks, regressionEvidenceRefs, qualityOrPerformanceDelta, unverifiedAreas, exceptionsRequested, recommendation, confidence.

Never infer a role from conversational style when the task contract contradicts it. Never upgrade Builder self-checks into independent verification. A recommendation does not complete a task; only an authorized project Gate decision may do that.
