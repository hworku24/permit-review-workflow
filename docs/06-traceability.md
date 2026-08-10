# Requirements Traceability Matrix

Every requirement traced to the design artifact that specifies it, the module that
implements it, and the test that proves it. Rows with no test are not done.

Verified against the codebase at the close of the build.

## Functional requirements

| ID | Requirement | Design | Implementation | Test |
|---|---|---|---|---|
| FR-01 | Accept application with required fields | 03-data-model §3 | `api/schemas.py`, `api/routes/applications.py`, `process/engine.py::create_application` | `tests/test_api_applications.py::test_submit_creates_application` |
| FR-02 | Enrich parcel from Property Records | 04-integration-spec I1 | `integrations/property_records.py` | `tests/test_integrations.py::test_parcel_enrichment_attaches_zoning` |
| FR-03 | Verify contractor license | 04-integration-spec I2 | `integrations/licensing.py` | `tests/test_integrations.py::test_license_states` |
| FR-04 | Derive required document checklist and block on missing | 02-process-map §3 | `process/checklist.py` | `tests/test_checklist.py` |
| FR-05 | Return incomplete with itemized reasons | 02-process-map §1 | `process/engine.py::return_incomplete` | `tests/test_engine_intake.py::test_return_requires_reasons` |
| FR-06 | Refuse submission on open stop-work order | 04-integration-spec I1 | `process/engine.py::submit` | `tests/test_engine_intake.py::test_stop_work_order_blocks_submission` |
| FR-07 | Route to discipline queues | 02-process-map §3 | `process/routing.py` | `tests/test_routing.py` |
| FR-08 | Parallel discipline reviews | 02-process-map §2 | `process/engine.py::_evaluate_review_completion` | `tests/test_engine_review.py::test_parallel_tasks` |
| FR-09 | Least-loaded certified assignment | 02-process-map §3 | `process/assignment.py` | `tests/test_assignment.py` |
| FR-10 | Approve, approve with conditions, or record deficiencies | 02-process-map §2 | `process/engine.py` task actions | `tests/test_engine_review.py` |
| FR-11 | Deficiencies pause the clock | 02-process-map §5 | `process/engine.py`, `process/sla.py` | `tests/test_sla.py::test_pause_on_revisions` |
| FR-12 | Resubmission reopens only deficient disciplines | 03-data-model §2 | `process/engine.py::resubmit` | `tests/test_engine_review.py::test_reopen_only_deficient` |
| FR-13 | Supervisor reassignment with recorded reason | 02-process-map §2 | `process/engine.py::reassign` | `tests/test_engine_review.py::test_reassign_records_reason` |
| FR-14 | Business-day SLA clocks | 03-data-model §4 | `process/sla.py`, `sql/002_views.sql` | `tests/test_sla.py` |
| FR-15 | Clock pauses while waiting on applicant | 03-data-model §2 | `process/sla.py` | `tests/test_sla.py::test_pause_excluded` |
| FR-16 | Escalate at 80% and at breach | 02-process-map §5 | `process/escalation.py` | `tests/test_escalation.py` |
| FR-17 | Supervisor escalation queue by time past due | 03-data-model §4 | `sql/002_views.sql::v_open_escalations` | `tests/test_views.py::test_open_escalations_ordering` |
| FR-18 | Issue only when all disciplines terminal | 02-process-map §1 | `process/engine.py::issue` | `tests/test_engine_decision.py::test_issue_requires_all_terminal` |
| FR-19 | Denial requires recorded reason | 02-process-map §1 | `process/engine.py::deny` | `tests/test_engine_decision.py::test_deny_requires_reason` |
| FR-20 | Audit every transition and action | 03-data-model §3 | `permitflow/audit.py` | `tests/test_audit.py::test_transition_writes_audit` |
| FR-21 | Audit records not editable or deletable | 03-data-model §2 | `sql/001_schema.sql` audit rules | `tests/test_audit.py::test_audit_is_append_only` |
| FR-22 | Cycle time by type and month | 03-data-model §4 | `sql/002_views.sql::v_cycle_time` | `tests/test_views.py::test_cycle_time` |
| FR-23 | SLA compliance rate | 03-data-model §4 | `sql/002_views.sql::v_sla_compliance` | `tests/test_views.py::test_sla_compliance` |
| FR-24 | Workload per reviewer and discipline | 03-data-model §4 | `sql/002_views.sql::v_reviewer_workload` | `tests/test_views.py::test_reviewer_workload` |

## AI requirements

| ID | Requirement | Design | Implementation | Test |
|---|---|---|---|---|
| AI-01 | Extract structured fields from narrative | 02-process-map §4 | `ai/triage.py::extract_fields` | `tests/test_triage.py::test_extraction_shape` |
| AI-02 | Per-field confidence, blank below threshold | 02-process-map §4 | `ai/triage.py` | `tests/test_triage.py::test_low_confidence_left_blank` |
| AI-03 | Recommend discipline routing with reasoning | 02-process-map §4 | `ai/triage.py::recommend_routing` | `tests/test_triage.py::test_routing_recommendation` |
| AI-04 | No AI output causes a transition | 02-process-map §4 | `process/engine.py` accepts human actors only | `tests/test_triage.py::test_ai_cannot_transition` |
| AI-05 | Ordinance answers grounded with verbatim citation | 02-process-map §4 | `ai/rag.py` | `tests/test_rag.py::test_quote_is_verbatim` |
| AI-06 | Withhold rather than generate ungrounded answers | 02-process-map §4 | `ai/rag.py` | `tests/test_rag.py::test_unanswerable_withheld` |
| AI-07 | Log model, prompt version, sources, and override | 03-data-model §3 | `ai/recording.py` | `tests/test_triage.py::test_recommendation_recorded` |

## Non-functional requirements

| ID | Requirement | Implementation | Test |
|---|---|---|---|
| NFR-01 | Role based access, reviewer scoped to discipline | `api/security.py` | `tests/test_api_security.py` |
| NFR-02 | Integration failure degrades, does not block | `integrations/base.py` | `tests/test_integrations.py::test_degrades_on_outage` |
| NFR-03 | Timeout, bounded retry, circuit breaker | `integrations/base.py` | `tests/test_integrations.py::test_circuit_opens` |
| NFR-04 | Append-only audit enforced in the database | `sql/001_schema.sql` | `tests/test_audit.py::test_audit_is_append_only` |
| NFR-05 | Business day math correct across weekends, holidays, pauses | `process/sla.py`, `sql/002_views.sql` | `tests/test_sla.py`, `tests/test_sla_parity.py` |
| NFR-06 | Runs locally with no external keys | `ai/provider.py` offline path, `soap_mock/` | `tests/` run with no keys set in CI |

## Coverage of discovery pain points

| Pain point | Addressed by |
|---|---|
| P1 no reliable status | FR-14, US-02, `v_application_summary` |
| P2 SLA not tracked | FR-14, FR-23 |
| P3 work sits in unwatched queues | FR-09, FR-16, FR-17 |
| P4 uneven assignment | FR-09, FR-24 |
| P5 incomplete submissions consume capacity | FR-04, AI-01 |
| P6 no audit trail | FR-20, FR-21 |
| P7 repeated inconsistent zoning answers | AI-05, AI-06 |

## Deliberately not covered in Phase 1

| Item | Reason |
|---|---|
| Inspections, certificate of occupancy | Out of scope per 01-requirements §3 |
| Fee calculation and payment | Out of scope. Removes the APPROVED-then-paid-then-ISSUED split from the lifecycle |
| Public applicant portal | Out of scope. The API supports it, the UI is Phase 2 |
| Appeals hearing management | Out of scope. Phase 1 freezes the case at APPEAL_FILED |
| Java JDBC connector module | See 04-integration-spec I2. The direct connection is the same integration shape, the Java version is optional additional evidence |
