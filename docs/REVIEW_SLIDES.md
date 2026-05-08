# Local Data Sanitization Pipeline Review

A slide-style version of the canvas review.

Source: `README.md` and `sanitization-pipeline-review.canvas.tsx`

---

# Slide 1: What You Built

You built a deterministic local data sanitization pipeline for exported
enterprise data.

The strongest part is not the regex replacement itself. The strongest part is
the evidence system around it: deterministic traversal, file accounting,
failure isolation, row-level PII findings, quarantine, and validation.

In interview terms:

> This is a batch data governance pipeline, not just a find-and-replace script.

Core outputs:

- Sanitized mirrored output tree.
- `run_summary.json`
- `file_manifest.jsonl`
- `validation_report.json`
- `pii_transformations.csv`
- `pii_quarantine.csv`
- `analytics.html` — interactive single-page dashboard rendered per
  run, with run-stat tiles, an entity ↔ file network graph, and a
  grouped quarantine triage panel.

---

# Slide 2: Core Interpretation

The project walks a folder of messy enterprise-export-style files and produces a
sanitized output tree plus reports that let a reviewer understand what happened.

The design is centered on trust:

- Every input file is accounted for.
- Supported files are sanitized.
- Unsupported files are recorded instead of silently ignored.
- Malformed files fail at file scope instead of crashing the entire run.
- Mapped PII replacements are traceable.
- Unmapped emails and phones are masked and routed to quarantine.
- Validation checks the artifacts after they are written.

That makes the run reviewable without requiring a human to open every sanitized
file manually.

---

# Slide 3: Summary Metrics

The README describes a compact but complete demonstration surface:

- **6 report artifacts**: summary, manifest, validation report, mapped PII CSV,
  quarantine CSV, plus an interactive HTML analytics dashboard rendered
  per run.
- **4 post-run validation checks**: raw emails, raw phones, processed output
  existence, complete input accounting.
- **4 supported text-like formats**: `.txt`, `.md`, `.json`, `.csv`.
- **47 documented tests** covering replacement behavior, pipeline behavior,
  validation, reporting, edge cases, and analytics dashboard
  generation.

These numbers matter because they communicate that the system is more than a
single transformation function. It has runtime behavior, audit contracts, and
test coverage.

---

# Slide 4: End-To-End Flow

The input is a folder tree of mixed enterprise exports.

The pipeline:

1. Walks the input tree in deterministic sorted order.
2. Classifies files by extension.
3. Skips unsupported files without opening their bytes.
4. Processes `.txt`, `.md`, `.json`, and `.csv`.
5. Applies de-identification with configured mappings and quarantine fallbacks.
6. Writes a mirrored sanitized tree under `output/sanitized`.
7. Emits manifest, summary, validation report, and row-level PII CSVs.
8. Re-scans outputs and cross-checks manifest completeness.
9. Renders `analytics.html` — a single-page dashboard over the same data
   so a reviewer can open one file and see run stats, the
   entity ↔ file network graph, and the quarantine triage panel.

The important architectural point is that validation audits written artifacts,
not just in-memory state from the happy path.

---

# Slide 5: What This Demonstrates Well

## Auditability

The manifest, row-level findings, hashes, and validation report create an
evidence bundle. Reviewers can inspect what happened file by file and
replacement by replacement.

## Determinism

Stable traversal, stable placeholders, stable CSV columns, stable hashes,
deterministic JSON formatting, and stable output writers make regression testing
and incident replay practical.

## Operational Honesty

Unknown values are not disguised as solved. They are replaced safely, routed to
quarantine, and given enough context for operator triage.

## Visual Evidence

The same audit data also lands in `analytics.html`, a single-page interactive
dashboard rendered per run. A reviewer who doesn't want to parse JSON or query
CSVs can open one file and see the run stats, the entity ↔ file network graph,
and the quarantine triage panel — with back-links into the underlying
machine-readable artifacts. The dashboard is the visual layer over the same
evidence; it never replaces the structured reports.

---

# Slide 6: Major Decision Tradeoffs

| Decision | Why It Is Good | Tradeoff |
|---|---|---|
| Stdlib-only runtime | Auditable, portable, easy to run, low support burden. | Gives up richer parsers, DLP libraries, `libphonenumber`, streaming JSON, and mature detection engines. |
| Regex + explicit config | Deterministic and reviewable. Every known entity maps to a visible token. | Limited recall. Misses pronouns, implied entities, unknown names, and messy formats. |
| Person-centric config | Groups aliases, emails, and phones around the human entity they belong to. | Does not model relationships between people and organizations as first-class edges. |
| Deterministic traversal | Reproducible manifests, reports, and sanitized outputs. | Future parallelism must preserve stable report ordering. |
| Per-file failure isolation | One bad file does not abort the whole run. | Downstream systems must respect warnings and exit code `2`. |

---

# Slide 7: More Decision Tradeoffs

| Decision | Why It Is Good | Tradeoff |
|---|---|---|
| Manifest as source of truth | Every input file gets a row, including unsupported, failed, and empty files. | The manifest schema becomes a product contract. |
| Separate validation pass | The pipeline proves key claims by re-scanning outputs and checking the manifest. | Current checks are narrow and do not prove full semantic correctness. |
| Two PII CSVs with one schema | Mapped and unmapped rows can be consumed by the same tooling. | Raw values appear in both files and need stricter access controls. |
| Unmapped placeholders | `<UNMAPPED_EMAIL>` and `<UNMAPPED_PHONE>` make uncertainty visible. | Unknown values collapse to the same output token in sanitized text. |
| Three-phase de-id | Original locations stay honest and snippets are safe. | More complex than a single replace loop, especially for future streaming. |
| Self-contained `analytics.html` | One HTML file per run gives reviewers a visual entry point without reading raw artifacts; no Python deps beyond stdlib. | Relies on a CDN script tag for the graph library, so offline rendering would need the library inlined. Display-layer only — not a substitute for the structured reports. |

---

# Slide 8: Nuance - Audit Pipeline, Not Just Sanitizer

The README says the transformation is intentionally small and easy to audit.
That is the right scope.

The more important system is the pipeline around the transformation:

- Traversal.
- Per-file status.
- Hashing.
- Manifesting.
- Validation.
- Row-level findings.
- Operator triage.

Enterprise data workflows often fail because they lack proof, repeatability, and
operational visibility. This project puts design weight into those areas.

Better next step:

- Add explicit artifact contracts with schema versions and machine-readable
  schemas.

---

# Slide 9: Nuance - Determinism As A Product Requirement

Determinism is foundational here, not a nice-to-have.

The pipeline uses:

- Sorted recursive traversal.
- Deterministic placeholders.
- Content-derived `value_hash`.
- Stable CSV field order.
- Consistent line endings.
- Deterministic JSON formatting.

Why it matters:

- Reviewers can re-run and compare outputs.
- CI can detect real regression instead of ordering noise.
- Incident response can replay old runs from the same input, config, and code.

Caveat:

- `run_id` and timestamps intentionally differ between runs. A world-class
  version would separate stable content summaries from run metadata.

---

# Slide 10: Nuance - Quarantine Is Honest About Uncertainty

Unknown emails and phones are not auto-pseudonymized into tokens that look
reviewed.

Instead:

- Sanitized output receives `<UNMAPPED_EMAIL>` or `<UNMAPPED_PHONE>`.
- The raw value, hash, location, and safe snippet go to `pii_quarantine.csv`.
- Operators review the quarantine rows.
- Approved values are added to `config/entities.json`.
- The pipeline is re-run.
- The row migrates from quarantine to transformations.

This is strong workflow design because uncertainty becomes a visible operator
backlog instead of hidden ambiguity.

Tradeoff:

- The sanitized output loses unknown-entity distinctness.

---

# Slide 11: Nuance - Three-Phase Apply

The three-phase de-identification design is the most sophisticated implementation
detail.

It does:

1. **Scan original text**
   - Capture findings and offsets against the source text.
2. **Replace**
   - Apply cascading replacements in the correct order.
3. **Render snippets**
   - Generate snippets from fully sanitized text.

This solves two subtle bugs:

- Locations remain tied to the input file the operator opens.
- Snippets do not leak neighboring raw PII.

This invariant should be protected aggressively with tests because future
refactors will be tempted to simplify it.

---

# Slide 12: Nuance - Precision Over Recall

The system favors deterministic, explainable behavior over broad detection.

That means a reviewer can understand why a value changed:

- It matched an email regex.
- It matched a phone regex.
- It matched a configured person alias.
- It matched a configured organization alias.

But it will not discover:

- Unknown people.
- Unknown organizations.
- Pronouns.
- Nicknames outside config.
- Role references like "the CTO".
- Implied references like "the vendor".

This is a deliberate and reasonable demo tradeoff. A production system would add
NER, entity resolution, and source-specific parsing as reviewed layers around
the deterministic core.

---

# Slide 13: Central Compromise

This is a strong demonstration pipeline, not a complete production anonymization
platform.

It is intentionally:

- Local.
- Deterministic.
- Readable.
- Dependency-light.
- Audit-heavy.

Those strengths create predictable gaps:

- Limited file-type coverage.
- Limited detection recall.
- No NER or coreference.
- No token vault.
- No HMAC-backed value hashes.
- No production access control model.
- No streaming or concurrency yet.

The important point is that these are understandable scope decisions, not random
omissions.

---

# Slide 14: Compromises To Explain

| Tension | What You Gained | What You Accepted |
|---|---|---|
| Security vs operability | Raw values let operators resolve quarantine quickly. | PII reports become sensitive assets needing tighter controls. |
| Simplicity vs breadth | Regex and config are readable and testable. | Names, pronouns, PDFs, images, spreadsheets, and embedded docs are incomplete or unsupported. |
| Batch robustness vs strict correctness | A malformed file does not stop the run. | Partial success can be misused if warnings are ignored. |
| Portable runtime vs rich ecosystem | No runtime dependencies lowers setup and supply-chain risk. | You avoid mature libraries for parsing, schema validation, OCR, and entity detection. |
| Determinism vs secret-grade pseudonyms | Plain SHA prefixes are stable and easy to group. | They are guessable. HMAC is the production answer. |

---

# Slide 15: What Could Go Wrong

## False Negatives

- Unknown person names remain raw if they are not configured.
- Pronouns, nicknames, roles, and inferred references are not resolved.
- Unsupported PDFs, images, spreadsheets, archives, and embedded documents may
  contain PII.
- Conservative phone patterns can miss unusual international formats.

## Operational Misuse

- Downstream users may treat `completed_with_warnings` as clean success.
- PII CSVs may be copied around like normal reports unless access controls are
  explicit.
- Plain `value_hash` values support grouping but also membership checks.
- Pretty JSON output is not byte-preserving.

---

# Slide 16: What To Say About Limitations

The right answer is not to pretend the gaps are accidental.

The design deliberately prioritizes:

- Deterministic behavior.
- Inspectability.
- Local execution.
- Reviewable artifacts.
- Clear operator workflow.

A production version would keep the audit contracts and deterministic core, then
add stronger detection, security boundaries, processor coverage, policy controls,
and operational tooling.

Good interview sentence:

> I chose a small, deterministic detection mechanism so I could make the
> pipeline behavior trustworthy. In production, I would expand detection and file
> support without weakening the audit trail.

---

# Slide 17: Near-Term Enhancements

High-value improvements that fit the current architecture:

- Add explicit `schema_version` fields for artifacts.
- Add config and output schema validation.
- Add `--strict`.
- Add `--fail-on-unsupported`.
- Add `--fail-on-quarantine`.
- Replace plain SHA `value_hash` with HMAC in hardened mode.
- Add config hash to `run_summary.json`.
- Add code version or git commit to `run_summary.json`.
- Add output hash validation.
- Add golden-output tests.
- Add a README threat model.
- Classify report sensitivity.

---

# Slide 18: Medium-Term Enhancements

Enhancements that require more design:

- Processor plugin architecture.
- PDF text extraction.
- XLSX support.
- DOCX support.
- MIME sniffing.
- Streaming text processing.
- Streaming or size-gated JSON.
- Bounded worker pool for many files.
- Structured logging.
- Throughput and redaction-density metrics.
- Per-source schema validators.
- Policy-as-code for run acceptance.

Key constraint:

> Any concurrency or scale upgrade should preserve deterministic final artifact
> ordering.

---

# Slide 19: Production Enhancements

Platform-level upgrades:

- Separately permissioned storage for raw-value PII reports.
- Encryption and audit logs for sensitive artifacts.
- Token vault or keyed pseudonym service.
- HMAC key management and rotation.
- NER and coreference.
- Entity-resolution evaluation with labeled data.
- Human review queues.
- Lineage across input snapshots, config versions, code commits, validation
  results, and downstream datasets.
- Workflow states: generated, validated, reviewed, approved, exported, expired.

At this level, sanitization becomes a governed data workflow rather than a local
script.

---

# Slide 20: What I Would Have Done Differently

## Start With A Threat Model

Make clear who can read each artifact, what happens if reports leak, whether
pseudonyms must resist dictionary attacks, and whether reversibility is required.

## Version Artifact Contracts Immediately

Reports are effectively APIs. Once dashboards, CI, or downstream jobs consume
them, schema changes need compatibility discipline.

## Separate Demo Defaults From Production Modes

Local demo mode can be dependency-free and easy to run. Hardened mode should
require HMAC keys, separate PII report routing, and stricter run policy.

---

# Slide 21: More Things I Would Do Differently

## Formalize Processor Extensibility

Define a processor interface:

- Can read.
- Parse.
- Traverse strings.
- Annotate location.
- Write.
- Validate source-specific contracts.

That makes PDFs, spreadsheets, Slack exports, Jira exports, and email exports
feel planned rather than bolted on.

## Protect The Three-Phase Invariant

Make it explicit that:

- Locations always refer to original input.
- Snippets always come from fully sanitized output.
- Replacement counts come from findings, not replacement side effects.

---

# Slide 22: What World-Class Means

World-class is not just more file types or a smarter model.

It means the system is:

- Accurate.
- Secure.
- Explainable.
- Measurable.
- Governable.
- Operable at enterprise scale.

It should preserve the current strengths:

- Determinism.
- Auditability.
- Failure isolation.
- Row-level evidence.
- Quarantine workflow.
- Validation.

while adding production security, richer detection, broader modality support,
lineage, and operator UX.

---

# Slide 23: World-Class Dimensions

| Dimension | World-Class Version | Why It Matters |
|---|---|---|
| Detection quality | Hybrid deterministic rules, NER, coreference, source-aware parsers, human-reviewed evaluation sets. | Measured precision and recall by PII type, source, language, and format. |
| Security posture | HMAC or token vault pseudonyms, encryption, scoped IAM, audit logs, retention, key rotation. | A leaked sanitized dataset should not reveal raw PII or make membership testing easy. |
| Governance | Approval workflows, lineage, dataset versioning, policy-as-code, evidence bundles. | Every output can be traced back to input snapshot, config version, code commit, and validation report. |
| Operations | Metrics, structured logs, alerting, retry strategy, failure taxonomy, SLOs. | Teams know when a run is degraded, why, and who needs to act. |

---

# Slide 24: More World-Class Dimensions

| Dimension | World-Class Version | Why It Matters |
|---|---|---|
| Scale | Streaming readers, bounded parallelism, object-store writes, resumability, distributed execution. | Same contracts work for 10 files on a laptop and millions in a data lake. |
| Reviewer UX | Triage UI, grouping by value hash, diff views, approval states, comments, bulk config updates. The current `analytics.html` is the first step in this direction: it groups quarantine by hash and gives a visual overview, but a production version would add review state, approvals, and bulk actions. | Reviewers spend time deciding, not spelunking through CSVs. |
| Extensibility | Processor SDK, schema contracts, source-specific validators, custom detectors. | Adding a new enterprise export does not threaten core correctness. |

World-class keeps the trustworthy core and expands capability around it.

---

# Slide 25: Best Interview Framing

## Emphasize

- You scoped the transformation intentionally small so the pipeline mechanics
  could be excellent.
- You designed for repeatability, not just one successful run.
- You treated audit artifacts as first-class outputs.
- You handled uncertainty through quarantine instead of hiding it.
- You validated after writing artifacts, making the evidence independent of the
  happy path.

## Acknowledge

- This is not production DLP without HMAC or token-vault security.
- Regex and config do not solve entity recognition or coreference.
- Unsupported modalities are a serious production gap.
- Raw-value reports need stricter permissions than sanitized outputs.
- Scale upgrades must preserve deterministic contracts.

---

# Slide 26: Bottom Line

You built a credible local sanitization pipeline whose strongest quality is
trustworthiness.

Its strongest features:

- Deterministic outputs.
- Complete input accounting.
- Per-file failure isolation.
- Row-level PII auditability.
- Quarantine for uncertainty.
- Safe snippets.
- Validation after artifact writing.
- Visual evidence layer (`analytics.html`) over the same data, so a
  reviewer can land on one file and form a complete picture before
  drilling into the structured reports.

The world-class path is to preserve those contracts while adding:

- Stronger security.
- Richer detection.
- More file and source coverage.
- Better operator UX.
- Production-grade governance.
- Scalable runtime.

Final framing:

> This is not a finished production DLP platform. It is a well-scoped,
> deterministic, auditable local sanitization pipeline that demonstrates the
> foundations of one.
