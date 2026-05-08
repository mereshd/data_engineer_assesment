# Local Data Sanitization Pipeline - Deep Dive

This document is a detailed explanation of the project described in `README.md`.
It is written as an interview-prep artifact: what was built, why each decision
matters, what tradeoffs came with those decisions, what the current compromises
are, what could be improved, what I would do differently, and what a truly
world-class version of the system would look like.

## 1. Executive Summary

You built a local, deterministic, dependency-light Python pipeline for
sanitizing exported enterprise data. At the surface level, the project walks an
input folder, processes supported text-like files, replaces known PII with
stable tokens, writes a sanitized output tree, and emits audit reports.

The deeper engineering story is more important than the raw replacement logic.
This is not just a "find and replace PII" script. It is a small data governance
pipeline designed around trust:

- Every input file is accounted for.
- Files are processed in deterministic order.
- One malformed file does not crash the entire run.
- Outputs mirror the input tree.
- Sanitized files are hashable and reproducible.
- Every mapped PII replacement gets a row-level audit record.
- Every unmapped email or phone gets routed to quarantine.
- Validation runs after outputs are written, not just during processing.
- The system produces enough evidence for a reviewer to trust the run without
  manually opening every sanitized file.

That is the core value of the project. The transformation itself is deliberately
simple: regular expressions plus an entity configuration. The system around that
transformation is what demonstrates mature judgment: deterministic traversal,
manifesting, failure isolation, validation, audit artifacts, test coverage, and
operator workflow.

In interview terms, the strongest framing is:

> I built a local sanitization pipeline where the key problem was not merely
> replacing PII, but making the run auditable, deterministic, reproducible, and
> operationally trustworthy.

That distinction matters. A naive version of this project would read files,
replace strings, and write outputs. Your version creates an evidence bundle:
sanitized data plus a manifest, summary, validation report, transformation log,
and quarantine log. That is much closer to how production data platforms need to
behave.

## 2. What You Built

The project is a command-line Python package invoked as:

```bash
python -m sanitizer --input sample_input --output output
```

It takes an input directory containing mixed enterprise-export-style data and
produces:

```text
output/
  sanitized/
    <mirrored input tree, with sanitized supported files>
  reports/
    run_summary.json
    file_manifest.jsonl
    validation_report.json
    pii_transformations.csv
    pii_quarantine.csv
    analytics.html
```

The supported formats are:

- `.txt`
- `.md`
- `.json`
- `.csv`

Unsupported files such as `.pdf`, `.png`, `.xlsx`, and `.zip` are not opened or
parsed. They are recorded in the manifest as `skipped_unsupported`.

Malformed supported files, such as invalid JSON, are recorded as `failed`. The
pipeline continues processing the rest of the input tree.

The project therefore has three main layers:

1. **Input and processing layer**
   - Recursively walks files.
   - Sorts traversal for determinism.
   - Dispatches by extension.
   - Processes supported formats.
   - Isolates per-file failures.

2. **De-identification layer**
   - Detects emails and phone numbers with regex.
   - Replaces configured emails, phones, people, and organizations with stable
     tokens.
   - Replaces unconfigured emails and phones with generic unmapped placeholders.
   - Produces row-level `Finding` records with file locations and safe snippets.

3. **Evidence and validation layer**
   - Writes a manifest row for every input file.
   - Writes a run summary with totals and status.
   - Writes mapped and unmapped PII reports.
   - Re-scans sanitized outputs for raw email and phone patterns.
   - Cross-checks output existence and manifest completeness.
   - Renders an interactive single-page `analytics.html` dashboard over
     the same data so a reviewer can open one file and see run stats,
     the entity ↔ file network, and the quarantine triage panel without
     parsing JSON or CSV.

That layering is the main architecture.

## 3. The Main Design Thesis

The README makes one idea very clear: the transformation itself is intentionally
small. The main thing being demonstrated is the pipeline around it.

That is a wise scope decision. In a limited project, attempting to build a
production-grade de-identification engine with NER, coreference, PDF extraction,
OCR, spreadsheet parsing, policy enforcement, and token vaults would result in a
thin, unreliable surface across too many areas. Instead, this project chooses a
smaller detection mechanism and makes the surrounding operational behavior
strong.

The thesis is:

> A simple transformation can still be valuable if the pipeline around it is
> deterministic, auditable, well-tested, and honest about uncertainty.

That is the right lesson. Enterprise data systems do not fail only because they
lack a regex. They fail because:

- Nobody knows which files were actually processed.
- Failures disappear into logs.
- Outputs differ between runs for unclear reasons.
- Reviewers cannot trace what changed.
- Unknown values are silently treated as known.
- Validation is implicit instead of explicit.
- Artifacts contain raw sensitive values without proper routing.
- Downstream systems consume partial outputs as if they were clean.

Your project addresses many of those concerns directly.

## 4. End-To-End Flow

The end-to-end flow is:

1. The CLI receives `--input`, `--output`, optional `--config`, and optional
   `--verbose`.
2. The pipeline validates top-level inputs and loads the entity config.
3. It recursively walks the input directory in sorted order.
4. For each file:
   - Determine extension.
   - If unsupported, record `skipped_unsupported`.
   - If empty, create a 0-byte mirrored output and record `empty`.
   - If supported, dispatch to the appropriate processor.
   - If the processor succeeds, write sanitized output and record `processed`.
   - If the processor raises, catch the exception and record `failed`.
5. During processing, the de-identifier:
   - Scans original strings for PII matches.
   - Produces mapped and unmapped findings.
   - Performs replacements.
   - Renders safe snippets against sanitized text.
6. After all files are handled:
   - Write `file_manifest.jsonl`.
   - Write `pii_transformations.csv` if mapped findings exist.
   - Write `pii_quarantine.csv` if unmapped findings exist.
   - Run validation against written artifacts.
   - Write `validation_report.json`.
   - Write `run_summary.json`.
   - Render `analytics.html` over the same data — the dashboard is
     written last so a rendering error never blocks the structured
     reports from getting on disk.
7. The CLI prints a compact one-line summary and exits with a meaningful code.

The key property is that the pipeline is not purely in-memory. The validator
audits the artifacts that were actually written. This makes the reports more
credible because the system proves properties about the output tree and manifest,
not just about temporary objects inside one function.

## 5. Supported File Types And Why This Scope Makes Sense

The project supports `.txt`, `.md`, `.json`, and `.csv`.

This is a practical scope for a local take-home or interview project. These file
types cover common exported enterprise text:

- Plain notes.
- Markdown docs.
- JSON exports from Slack, Jira, or similar tools.
- CSV exports from email systems, CRMs, spreadsheets, or ticketing tools.

The processors are format-aware:

- Text and Markdown are treated as whole documents.
- JSON is recursively traversed and only string values are sanitized.
- CSV is handled cell-by-cell with headers preserved.

That matters because "sanitize a file" means different things by format.

For text, a useful location is line and column.

For JSON, a useful location is a JSON path like:

```text
$.issues[1].comments[0].body
```

For CSV, a useful location is row and column:

```text
row 4, column "from"
```

This location-aware processor design is strong. It means the row-level audit
log is not just "something changed in file X." It tells the operator where in
the source structure the finding came from.

### Tradeoff

The scope is intentionally narrow. It excludes:

- PDFs.
- Images.
- Spreadsheets.
- Word documents.
- Archives.
- Audio or video transcripts.
- Embedded attachments.
- Non-text binary formats.

That is acceptable for the current project because unsupported files are not
ignored silently. They are recorded in the manifest. This means the system is
honest about its supported surface area.

The production gap is that enterprise exports often contain exactly these
unsupported modalities. Contracts arrive as PDFs, screenshots contain customer
data, spreadsheets contain names and emails, and ZIP files bundle many hidden
documents. A production version would need processors for those formats or a
policy that blocks runs containing unsupported files.

## 6. Deterministic Traversal

One of the best design decisions is sorted traversal.

`os.walk` does not guarantee deterministic ordering. File ordering can vary by:

- Filesystem.
- Operating system.
- Insertion order.
- Prior deletes and rewrites.
- Case-folding behavior.

If file ordering changes, downstream artifacts can change even when the input
content did not. Manifest rows may appear in a different order. PII CSV rows may
shift. Summary aggregation order may drift. Diffs become noisy.

The project fixes this by sorting directory names in place and sorting file
names at every level.

That supports:

- Reproducible output.
- Stable test expectations.
- Cleaner diffs.
- Easier incident replay.
- Cross-machine consistency.

### Tradeoff

Sorting is a small overhead. For realistic local enterprise exports, this is
worth it. The larger tradeoff appears later if concurrency is added. Parallel
processing can make result ordering nondeterministic unless the parent process
sorts completed per-file results before writing reports.

So the principle should be:

> Process in parallel if needed, but write artifacts in deterministic order.

That lets the project scale without losing one of its strongest properties.

## 7. Dependency-Free Runtime

The README emphasizes that the runtime uses only the Python standard library.
`pytest` is the only dev dependency.

This is a deliberate engineering decision.

### Benefits

1. **Auditability**
   - A reviewer can inspect the whole runtime stack without chasing third-party
     package behavior.
   - The transformation logic is transparent.

2. **Portability**
   - Python 3.9+ is enough.
   - No dependency installation is required for runtime use.
   - The code can run in restricted environments.

3. **Lower supply-chain risk**
   - No runtime package vulnerabilities.
   - No transitive dependency churn.
   - No version conflicts.

4. **Interview clarity**
   - The project demonstrates your own pipeline design rather than leaning on a
     large external framework.

### Costs

The main cost is capability. Standard library tools are solid but limited.

You do not get:

- `libphonenumber` for robust global phone parsing.
- `pydantic` or `jsonschema` for schema validation.
- `ijson` for streaming JSON.
- `pandas` for high-volume tabular ergonomics.
- PDF parsing libraries.
- OCR libraries.
- Mature NER or DLP libraries.
- Better regex engines like the third-party `regex` package.

For a local deterministic demo, stdlib-only is a good choice. For production,
you would selectively add dependencies where the capability gain clearly
outweighs the operational cost.

The key is not "never use dependencies." The key is "make dependencies earn
their place."

## 8. Source Layout

The README describes a `src/sanitizer/` layout:

```text
src/sanitizer/
  __main__.py
  cli.py
  pipeline.py
  deid.py
  processors.py
  validation.py
  utils.py
```

This is a good package layout.

### Why `src/` Layout Helps

The `src/` layout prevents accidentally importing the local working tree in ways
that differ from the installed package. It makes packaging behavior more honest.

It also communicates maturity. Instead of one large script, the system has clear
module boundaries:

- `cli.py`: command-line parsing, exit codes, user-facing summary.
- `pipeline.py`: orchestration across files and reports.
- `deid.py`: entity config and replacement logic.
- `processors.py`: format-specific parsing, writing, and location annotation.
- `validation.py`: independent post-run checks.
- `utils.py`: hashing, traversal, run IDs, JSON/CSV helpers.

That split makes the code reviewable. A reviewer can ask:

- How are files walked?
- How is PII detected?
- How are locations computed?
- How are reports written?
- How is validation done?

And there is a natural place to look for each answer.

### Tradeoff

For a tiny script, this structure is more ceremony than necessary. But this
project is not trying to be a tiny script. It is trying to demonstrate a
pipeline that can grow. The module boundaries are justified because the system
has separate concerns.

## 9. Entity Configuration

The config is person-centric:

```json
{
  "persons": [
    {
      "canonical_id": "PERSON_001",
      "aliases": ["John Miller", "John"],
      "emails": [{"value": "john@acme.com", "token": "EMAIL_001"}],
      "phones": []
    }
  ],
  "organizations": [
    {"canonical_id": "ORG_001", "aliases": ["BetaHealth"]}
  ]
}
```

This is one of the more important modeling decisions.

The config does not store separate flat maps as the primary source of truth.
Instead, human-readable entity records are loaded and transformed into efficient
runtime lookup structures.

### Why This Is Good

The config preserves relationships:

- `Sarah Chen`
- `Sarah`
- `sarah@betahealth.io`
- `+1-212-555-0199`

all belong to the same person.

That matters because sanitization is not just string replacement. It is entity
preservation. Downstream systems may need to know that two mentions refer to the
same person without knowing the raw identity.

A person-centric config gives a future entity-resolution system a natural place
to write results. It can add aliases, emails, phones, confidence scores,
source-specific references, or lifecycle metadata later.

### Tradeoff

The current model is simple. It does not deeply represent:

- Employment relationships.
- Person-to-organization membership.
- Role history.
- Multiple organizations per person.
- Temporal validity.
- Source confidence.
- Conflicting source claims.
- Entity lifecycle.

For example, if a person changes companies, should their email still map to the
same person? Should an old phone number remain associated? Should two source
systems that disagree trigger a config error or a review queue?

Those are production entity-resolution questions. The current config is a good
foundation, but not a complete entity graph.

## 10. Strict Config Loading

The loader raises errors for malformed entries and conflicting mappings.

This is important. Config files are code-like in data pipelines. A bad config
can cause silent corruption.

The project catches:

- Missing expected fields.
- Same normalized value mapped to different tokens.
- Conflicting ownership across entities.

It allows idempotent duplicates, such as case variants of the same email mapping
to the same token.

That is a good balance:

- Strict enough to catch real bugs.
- Flexible enough not to punish harmless repetition.

### Tradeoff

Strict loading can prevent the pipeline from starting. That is correct for
config errors, because a broken config is a systemic issue. It is different from
a malformed input file, which can be isolated to one manifest row.

The distinction is mature:

- Bad input file: isolate and continue.
- Bad configuration: fail loudly.

That is the right boundary because the config affects every file.

## 11. Replacement Order

The replacement order is:

```text
emails -> phones -> persons -> organizations
```

This order is not arbitrary. It protects against overlapping matches and partial
corruption.

### Why Emails First

Consider:

```text
sarah@betahealth.io
```

If organization aliases run first, `BetaHealth` could be replaced inside the
email domain:

```text
sarah@ORG_001.io
```

Now the email regex no longer sees the original email. The output leaks partial
raw information.

If emails run first, the whole address becomes:

```text
EMAIL_002
```

and there is no inner organization match left to corrupt.

### Why Phones Before Names And Orgs

Phone patterns are more bounded than broad aliases. If any alias contains digits
or overlaps with phone-like text, replacing aliases first could damage the phone
match.

Phones are taken off the table before broader alias rules run.

### Why Longest Alias First

If both `Sarah Chen` and `Sarah` are aliases, replacing `Sarah` first can leave:

```text
PERSON_002 Chen
```

Replacing the longest alias first produces:

```text
PERSON_002
```

This same principle applies to organizations like `Acme Inc.` before `Acme`.

### Tradeoff

Hard-coding replacement order makes the system predictable. The cost is that the
order becomes an invariant future maintainers must understand. If someone later
adds new PII kinds, such as addresses, account IDs, SSNs, IP addresses, product
names, or case IDs, they must reason carefully about where those kinds belong in
the priority order.

A production version might make this a documented priority table with tests for
overlap behavior.

## 12. Alias Boundaries

The README calls out an important regex nuance: aliases use lookarounds instead
of `\b`.

The reason is aliases that end in punctuation, such as:

```text
Acme Inc.
```

Word boundary `\b` is defined as a transition between word and non-word
characters. The position after a period followed by a space is not necessarily a
word boundary. That makes `\b` fragile for aliases ending in non-word
characters.

Using:

```text
(?<!\w) ... (?!\w)
```

means:

- The character before the alias cannot be a word character.
- The character after the alias cannot be a word character.

This prevents matching aliases inside larger words while still allowing aliases
that end in punctuation.

### Why This Is A Good Detail

This is the kind of nuance interviewers like because it shows the implementation
was tested against real edge cases rather than guessed. It is not flashy, but it
prevents subtle bugs.

The example `Mark` inside `Marketing` is also important. The system should not
replace a person alias inside an unrelated word.

### Tradeoff

Regex boundaries are always a compromise. Lookarounds work well for ASCII-ish
word semantics, but names in real enterprise data can include:

- Apostrophes.
- Hyphens.
- Accents.
- Non-Latin scripts.
- Mixed punctuation.
- Irregular spacing.

The current design is good for the sample and common English enterprise data.
A global production system would need more sophisticated tokenization and
language-aware handling.

## 13. Three-Phase De-Identification

The three-phase `apply()` design is probably the strongest implementation nuance
in the project.

It works like this:

1. **Scan original text**
   - Find all candidate matches.
   - Record original offsets.
   - Drop lower-priority overlaps.
   - Create `Finding` objects.

2. **Replace**
   - Perform cascading replacement in the defined order.
   - Produce the sanitized string.

3. **Render snippets**
   - Locate the corresponding tokens/placeholders in sanitized text.
   - Render safe snippets from the fully sanitized output.

### Why This Matters

A naive implementation would combine scanning and replacement in one pass.
That feels simpler but creates two serious problems.

#### Problem 1: Offsets Become Dishonest

If a replacement changes string length, every later offset shifts.

For example, replacing:

```text
vendor.support@externalpartner.com
```

with:

```text
<UNMAPPED_EMAIL>
```

changes the character count. A later phone number might be reported at the wrong
line or column if the system computes location after prior replacements.

The three-phase design records offsets against the original text, so line and
column locations point to what the operator actually opens.

#### Problem 2: Snippets Can Leak Neighboring PII

If snippets are rendered from original text, the focal value might be masked but
nearby PII could remain raw in the context window.

The current design renders snippets from the fully sanitized text. That means
the focal match is tokenized and neighboring PII is also tokenized.

This is a major privacy improvement.

### Tradeoff

The three-phase design is more complex than a simple replace loop. It requires:

- Findings with offsets.
- Overlap handling.
- Replacement order.
- Snippet re-location in sanitized text.
- Tests that protect the invariant.

But that complexity is justified because it solves correctness and privacy bugs.

If I were explaining this in an interview, I would say:

> I started with the simple mental model of scanning and replacing, but that
> breaks location accuracy and can leak neighboring PII in snippets. Splitting
> scan, replace, and snippet rendering made the audit trail truthful and the
> snippets safe.

That is a strong explanation because it shows learning from failure.

## 14. Overlap Handling

The de-identifier tracks covered spans during scanning.

This prevents double-counting and corrupt overlapping replacements.

Examples:

- A phone-shaped substring inside an email should belong to the email, not also
  become a phone finding.
- `Sarah` inside `Sarah Chen` should not create a second finding if the longer
  alias already matched.
- A string that is both a person alias and an organization alias should not
  produce two conflicting replacements.

This is important because row-level reports are used as evidence. Double-counted
findings would break the trustworthiness of replacement totals.

### Tradeoff

The priority order decides which match wins. That is necessary, but it means
the system encodes a policy. For ambiguous aliases, the policy might be wrong.

For example:

- A company and person have the same name.
- A short alias is valid in one context but part of a longer unrelated phrase in
  another.
- An organization alias appears inside an email domain.

The current approach chooses deterministic behavior over contextual reasoning.
That is appropriate for this version. Production would need richer entity
resolution, confidence scores, or review workflows for ambiguous aliases.

## 15. Mapped Values

Mapped values are configured known entities:

- Emails map to `EMAIL_###`.
- Phones map to `PHONE_###`.
- People map to `PERSON_###`.
- Organizations map to `ORG_###`.

This preserves cross-document consistency. If Sarah appears in multiple files,
she becomes the same token.

That is important for downstream analytics or AI training. The sanitized data
can preserve relationships without revealing raw identities.

For example:

```text
PERSON_001 emailed PERSON_002 at EMAIL_002.
```

This is much more useful than replacing every person with the same generic
`<PERSON>` token. It allows downstream models or analysts to understand that
the same actor appears repeatedly.

### Tradeoff

Stable tokens can leak relational information. Even without raw names, a reader
can infer:

- Which pseudonymous person appears most often.
- Which people communicate together.
- Which organization is central.
- Whether the same email appears across files.

That is often desirable for utility, but it is still a privacy tradeoff.
Production systems need to define what relational information is safe to
preserve.

## 16. Unmapped Values

Unmapped email and phone matches are replaced with:

```text
<UNMAPPED_EMAIL>
<UNMAPPED_PHONE>
```

and written to `pii_quarantine.csv`.

This is one of the best workflow decisions in the project.

### Why Generic Placeholders Are Good

If an unknown email became:

```text
EMAIL_AUTO_a3f9b21c
```

it would look similar to a reviewed token like:

```text
EMAIL_002
```

A reader might assume it is approved. That is dangerous.

Using `<UNMAPPED_EMAIL>` makes uncertainty visible. It says:

- The value was detected.
- The raw value was removed from sanitized output.
- The value is not yet part of the approved config.
- An operator needs to review it.

That is honest design.

### The Triage Loop

The operator workflow is:

1. Open `pii_quarantine.csv`.
2. Sort or group by `value_hash`.
3. Review the raw `value` and safe `snippet`.
4. Decide whether the value should be added to `config/entities.json`.
5. Re-run the pipeline.
6. The finding moves from quarantine to transformations.

This loop is practical and concrete.

### Tradeoff

The generic placeholder loses distinctness in the sanitized output. If three
unknown emails appear in a file, they all become:

```text
<UNMAPPED_EMAIL>
```

The quarantine CSV preserves distinctness through `value` and `value_hash`, but
the sanitized text itself does not.

That is a reasonable choice because it prioritizes review honesty. But a
production system might use a hybrid:

```text
<UNREVIEWED_EMAIL_aedb8969>
```

where the suffix is HMAC-derived, not plain SHA. That would preserve unknown
entity distinctness while still visibly marking the value as unreviewed.

## 17. Value Hashes

The project uses:

```text
sha256(value)[:8]
```

as a stable deduplication key.

This is useful because the same unknown vendor email appearing in multiple files
gets the same hash. Operators can group repeated findings and resolve them with
one config update.

### Why This Is Good

The hash supports:

- Cross-document grouping.
- Idempotent triage.
- Report joins.
- Stable references without relying only on raw values.

### Security Limitation

Plain SHA-256 is not secret. It is deterministic and unsalted.

An attacker can precompute:

```text
sha256("target@example.com")[:8]
```

and check whether that hash appears in a report.

This is called a dictionary or membership attack. The risk is especially high
for emails and phone numbers because the possible search space may be small or
guessable.

### Production Upgrade

Use HMAC:

```text
HMAC-SHA256(secret_key, value)[:8]
```

Now the hash is still deterministic for authorized systems with the key, but an
attacker without the key cannot precompute values.

The secret should live in a managed secret store such as:

- AWS KMS.
- HashiCorp Vault.
- GCP Secret Manager.
- Azure Key Vault.

A hardened version would also support key rotation and possibly include a key
version in report metadata.

## 18. File Manifest

`file_manifest.jsonl` is the central audit artifact.

It has one row per input file, regardless of status.

Statuses include:

- `processed`
- `skipped_unsupported`
- `failed`
- `empty`

Each row includes information such as:

- Relative path.
- Extension.
- Status.
- Input hash.
- Output hash, if output exists.
- Output path, if output exists.
- Records processed.
- Replacement counts.
- Unmapped counts.
- Error message, if failed.

### Why This Is Strong

The manifest answers:

- What files did the pipeline discover?
- Which files were processed?
- Which files were skipped?
- Which files failed?
- Which outputs were created?
- What were their hashes?
- How much PII was replaced per file?
- Where were unmapped findings concentrated?

This is essential for review. Without a manifest, a sanitized output folder is
just a folder. With a manifest, it becomes an auditable dataset.

### Tradeoff

The manifest schema becomes an API. Once dashboards, CI checks, or data jobs
consume it, changing fields becomes a compatibility concern.

A production version should add:

- `schema_version`.
- `pipeline_version`.
- `config_hash`.
- `code_commit`.
- `input_snapshot_id`.
- `run_id`.
- Possibly `processor_version` per file.

That would improve lineage and forward compatibility.

## 19. Run Summary

`run_summary.json` is the compact run-level view.

It includes:

- Run ID.
- Start and completion timestamps.
- Input and output roots.
- Run status.
- File totals.
- Per-extension status histogram.
- Replacement totals.
- Unmapped totals.
- Validation summary.

This is the artifact most useful for dashboards, CI, DAG systems, and alerting.

### Why It Is Useful

The summary gives a quick answer to:

- Did the run complete?
- Were there warnings?
- How many files were processed?
- How many files failed?
- Did validation pass?
- How much PII was transformed?
- Is there quarantine work?

It is intentionally compact.

### Tradeoff

Summaries can hide details. A run with `completed_with_warnings` might include:

- One harmless unsupported `.png`.
- A failed JSON file containing critical data.
- Dozens of quarantined unknown emails.
- Empty files.

All of those are warnings, but they do not have the same severity.

A production version should add severity classification, such as:

- `warning_unsupported_only`
- `warning_quarantine_present`
- `warning_failed_files`
- `validation_failed`

or include policy evaluation:

```json
{
  "policy_result": "blocked",
  "reasons": ["failed_files_present", "unmapped_pii_above_threshold"]
}
```

That would prevent downstream systems from treating all warnings equally.

## 20. PII Transformation Report

`pii_transformations.csv` contains one row per mapped PII replacement.

The schema is:

```text
file,kind,value,value_hash,token,status,location,snippet
```

This is powerful because it provides row-level accountability.

For every mapped replacement, a reviewer can see:

- Which file contained it.
- What kind of PII it was.
- What normalized raw value was found.
- What token replaced it.
- Whether it was mapped or unmapped.
- Where it appeared.
- A safe snippet of context.

### Why CSV Makes Sense

The report is flat. CSV is appropriate because:

- It opens in Excel.
- It loads easily into pandas.
- It can be queried in BigQuery or similar tools.
- Operators can sort by `value_hash`, `kind`, `file`, or `status`.

JSON would be more verbose without adding much structure for this report.

### Tradeoff

The report carries raw values. This makes it operationally useful but sensitive.

It should not have the same access policy as sanitized outputs.

In production:

- Sanitized outputs might be broadly available to downstream processing.
- PII transformation and quarantine reports should be restricted.
- Access to raw-value reports should be audited.
- Retention should be limited.
- Exports should be encrypted.

This is one of the most important security compromises in the project.

## 21. PII Quarantine Report

`pii_quarantine.csv` contains one row per unmapped email or phone.

It uses the same schema as `pii_transformations.csv`.

That is a smart design. It means mapped and unmapped reports can be consumed by
the same tooling. A row can migrate from quarantine to transformations after a
config update without changing shape.

### Why Same Schema Is Good

Shared schema means:

- One parser.
- Easy unioning.
- Easy diffing between runs.
- Same operator mental model.
- Same downstream analytics.

The only semantic difference is `status`.

### Tradeoff

Quarantine is only implemented for regex-detected unknown emails and phones.
Unknown names or organizations that are not configured may remain raw because
there is no general NER layer to detect them.

This is a key limitation:

- Unknown email: detected and quarantined.
- Unknown phone: detected and quarantined.
- Unknown person name: likely missed.
- Unknown organization name: likely missed.

That is acceptable for a config-and-regex demo, but it is a major production
gap.

## 22. Validation Report

The validator runs four checks:

1. No raw emails in sanitized outputs.
2. No raw phone numbers in sanitized outputs.
3. Processed files have outputs.
4. All input files are accounted for in the manifest.

This is a good validation layer because it is independent from the processing
happy path.

### Why Validation After Writing Matters

It is not enough for the pipeline to say:

```text
I replaced emails.
```

The validator checks the actual files under `sanitized/`.

It is not enough for the pipeline to say:

```text
I processed all files.
```

The validator cross-checks input files against manifest rows.

This makes validation a second layer of evidence.

### Tradeoff

The validation is intentionally narrow.

It does not verify:

- Unknown person names.
- Unknown organizations.
- Token uniqueness.
- Config-to-output referential integrity.
- Whether every transformation row corresponds to an actual output token.
- Whether every output token has a row-level finding.
- Whether JSON output preserves schema.
- Whether CSV row counts match input row counts.
- Whether unsupported files violate policy.
- Whether failed files contain PII.
- Whether snippets are always safe for all PII kinds.

For the current project, four crisp checks are better than many vague checks.
For production, validation should expand substantially.

## 22.5 Analytics Dashboard

`analytics.html` is the visual layer over the same audit data the structured
reports already carry. It is rendered last in the run, after every other
artifact is on disk, and it is meant to be the reviewer's first stop —
the file you open before you start parsing JSON or querying CSVs.

The dashboard is a single self-contained HTML page with three regions:

1. **Header strip with run-level stat tiles.**
   - Run id and timestamps.
   - Eight color-coded tiles: `Files Discovered / Processed / Skipped /
     Failed / Empty / Mapped Replacements / Unmapped Records /
     Validation`.
   - Tiles flip yellow when there are warnings (skipped, empty, or
     unmapped non-zero), red on failures, and green when validation
     passes. The reviewer's first glance answers "did this run land
     cleanly?" without reading anything else.

2. **Entity ↔ file network graph.**
   - Force-directed graph rendered with the `vis-network` library.
   - File nodes are blue boxes, sized by replacement count so heavy
     files visually pop.
   - Entity nodes are color-coded ellipses by kind: green for persons,
     orange for organizations, purple for emails, pink for phones.
     Each one is labeled with its canonical token (`PERSON_001`,
     `ORG_002`, etc.); hovering reveals the raw value behind the
     token.
   - Edges connect file → entity, weighted by occurrence count
     (rendered as edge thickness). Hover shows a "N occurrences"
     tooltip.
   - On the bundled sample input this is a 16-node, 35-edge graph.
     On a real ingest, the graph immediately surfaces which files
     share which entities and which canonical entity is the busiest.

3. **Quarantine triage panel.**
   - Unmapped values grouped by `(kind, value, hash)`, sorted by
     occurrence count (most frequent first).
   - Each group shows the kind badge, the raw value, the content hash
     for cross-doc dedup, and the per-occurrence detail
     (`file @ location` plus the sanitized snippet).
   - On a clean run the panel collapses to a single green "No unmapped
     values - clean run" tile.

The header carries `<a href>` links back to all five sibling artifacts
(`run_summary.json`, `file_manifest.jsonl`, `validation_report.json`,
`pii_transformations.csv`, `pii_quarantine.csv`), so the dashboard is
an entry point rather than a replacement for them. A reviewer who
spots something interesting in the graph or the quarantine panel can
pivot into the underlying CSV/JSON one click later.

### Why It Belongs In This Pipeline

The brief asks for a run summary plus a per-file manifest. Those are
the floor — enough so a reviewer doesn't have to open every output
file to know whether the run succeeded.

Layered on top of that:

- The validation report says "the pipeline did what it claimed" by
  re-checking the artifacts independently.
- The two row-level PII CSVs answer "where exactly did each
  replacement happen?" and "show me every place we couldn't classify
  a value, and why."
- The analytics dashboard turns those structured answers into a
  visual one. It is the difference between "you need to know `jq`
  and `csvkit`" and "you need to open the file."

For Sunset's operating context — many files arriving from many
sources, reviewers landing cold on a run they didn't kick off
themselves — the visual entry point is more than a nice-to-have. It
is the layer that decides how quickly someone can decide whether to
trust the run.

### Implementation Notes

- **Stdlib-only on the Python side.** The renderer is one module,
  `sanitizer/analytics.py`. It builds a JSON-serializable dict from
  the in-memory `RunResult` (summary, manifest, transformations,
  quarantine) and writes a single HTML file. No new runtime
  dependencies.
- **One CDN script tag for the graph library.** The page references
  `vis-network@9.1.9` from unpkg. Modern browsers fetch it on first
  open. Offline rendering would require inlining the library
  (~200 KB), which is a swap-in if the deployment context calls for
  it.
- **Safe data embedding.** The data payload is embedded as a
  `<script type="application/json" id="dashboard-data">` block. The
  Python side replaces `<` with the Unicode escape `\u003c` in the
  JSON before embedding, which prevents any user-controlled string
  (a snippet that happens to contain `</script>`, a malformed file
  path, etc.) from breaking the surrounding HTML. `JSON.parse`
  handles the escape natively on the JS side.
- **Privacy contract preserved.** Snippets in the dashboard payload
  are the same ones that go into the row-level CSVs — rendered
  against the fully sanitized text, so the focal value appears as
  its placeholder/token and any other PII in the surrounding
  60-char window is also already a token. Tests assert this
  explicitly.
- **Cross-document dedup is visible.** The quarantine panel groups
  occurrences by `value_hash`, so the same vendor email appearing
  across N files surfaces as one group with N sub-entries — the
  triage unit is the value, not the file.

### Tradeoffs

- **CDN dependency at view time.** A reviewer with no internet on
  first open sees an empty graph container. The sanitized outputs
  and structured reports are unaffected. For a hardened deployment,
  inline the library.
- **Single HTML file means weight grows with run size.** For a run
  with 10K mapped findings, the embedded JSON and the rendered
  graph both grow proportionally. Production would want pagination
  or lazy loading.
- **No interactivity beyond hover/zoom.** The dashboard is a
  read-only view. It does not let an operator approve quarantine
  rows, edit the entity config, or kick off a re-run. That is the
  right boundary for an audit artifact (the dashboard is not
  load-bearing for any decision the pipeline is supposed to be
  making automatically), but it is also the line where production
  operator UX would extend it.
- **Same security posture as the PII CSVs.** The dashboard's
  embedded payload includes the raw `value` field for unmapped
  findings, just like `pii_quarantine.csv`. So in a tighter
  security posture, `analytics.html` belongs in the same access
  bucket as the PII reports — not with the sanitized outputs.

### Where It Lives In The Production Path

The dashboard is a step in the direction of an operator review UI,
not a replacement for one. A production version would extend it
into a service:

- Persistent review state per finding.
- Approval workflow ("add this value to entities.json and run
  again" as a button instead of a manual edit).
- Cross-run trends (quarantine volume over time, validation pass
  rate, redaction density per source).
- Diff views across runs.
- Bulk actions for repeated findings.
- Audit logs for who viewed what.

The current implementation deliberately stops short of all of that
because the goal here is per-run visibility, not a multi-run
governance UI. But the data contract — the same shape the
dashboard reads from in-memory, the same JSON payload it embeds
in the HTML — is the contract any future review UI would build on.

## 23. Run Status And Exit Codes

The project maps status to exit codes:

- `0`: completed and validation passed.
- `2`: completed with warnings or validation found a leak.
- `1`: catastrophic failure.

This is useful because command-line pipelines need machine-readable outcomes.

CI, Airflow, Dagster, cron jobs, or shell scripts can act on exit codes.

### Why Exit Code 2 Is Good

Exit code 2 communicates:

> The run produced artifacts, but a human or policy gate needs to inspect them.

That is different from:

> The pipeline crashed and may not have produced complete artifacts.

This distinction is important.

### Tradeoff

Exit code 2 combines multiple conditions:

- Unsupported files.
- Failed files.
- Empty files.
- Unmapped findings.
- Validation leaks.

Those are not equally severe. A production orchestrator may need more granular
policy outcomes.

For example:

- Unsupported `.png`: warning.
- Failed `.json` from a critical source: block.
- Any raw email leak after validation: block.
- Quarantine count above threshold: block.
- Empty file: warning unless required source.

So the current exit code design is a good starting point, but production policy
needs richer severity.

## 24. Failure Isolation

The pipeline catches per-file processor exceptions and records failed manifest
rows.

This is a core robustness feature.

Without failure isolation, one malformed JSON file could stop the entire run.
That would be bad for batch enterprise exports, where messy files are expected.

With failure isolation:

- The rest of the data still gets processed.
- The failed file is visible.
- The error is serialized.
- The run status reflects warnings.
- Reviewers can decide what to do.

### Tradeoff

Failure isolation can become dangerous if downstream consumers ignore warnings.

A completed output tree may be incomplete. If a failed file contained important
PII, the sanitized dataset may not be safe to use as a complete replacement for
the source.

That is why status and validation must be treated as part of the contract.

A production version should support:

- `--strict`: fail on first file error.
- `--fail-on-unsupported`.
- `--fail-on-quarantine`.
- Per-source severity rules.
- Retry logic for transient read errors.
- Dead-letter routing for failed files.

## 25. Empty File Handling

Empty files are not ignored. They are recorded as `empty`, and a 0-byte output is
created with the SHA-256 hash of empty bytes.

This is a small but mature detail.

It means the mirrored output tree preserves the fact that the input contained an
empty file. The manifest also proves the file was seen.

### Tradeoff

Some pipelines might prefer to skip empty files. Mirroring them is better for
auditability because absence can be ambiguous:

- Was the file missing?
- Was it skipped?
- Was it unsupported?
- Was it empty?

The explicit `empty` status removes ambiguity.

## 26. Unsupported File Handling

Unsupported files are recorded but never opened.

This is a clean boundary.

### Benefits

- Avoids accidentally trying to decode binary files as text.
- Keeps supported surface area explicit.
- Reduces risk of corrupt parsing.
- Makes unsupported formats visible in the manifest.

### Tradeoff

The pipeline cannot know whether unsupported files contain PII. It records their
existence, but it does not sanitize them.

In production, unsupported files should trigger policy decisions:

- Allow with warning.
- Block the run.
- Route to another processor.
- Extract text through OCR or document parsing.
- Require manual review.

For a local demo, recording unsupported files is enough. For a real data export
pipeline, unsupported files are a risk class.

## 27. JSON Processing

JSON processing recursively sanitizes string values while preserving numbers,
booleans, nulls, arrays, and objects.

This is the right behavior for structured data. It avoids converting all values
to strings or damaging non-text fields.

### Benefits

- Maintains data types.
- Preserves structural relationships.
- Allows JSON-path locations for findings.
- Keeps output machine-readable.

### Tradeoff

The output uses pretty `json.dumps(indent=2)`. This means:

- Formatting changes.
- Original whitespace is not preserved.
- Original key order is only preserved according to parsed dict order.
- Comments in non-standard JSON are not supported.
- Very large JSON files are fully loaded into memory.

That is acceptable for exported JSON data. It is not appropriate for workflows
that require byte-preserving transformations.

Production upgrade options:

- Use streaming parsers for large JSON.
- Add source-specific schema validation.
- Preserve compact output if downstream systems expect it.
- Add JSON schema versioning.
- Refuse huge JSON files unless streaming mode is enabled.

## 28. CSV Processing

CSV processing sanitizes cell values and preserves headers.

That is a good decision because CSV headers are structural metadata. Replacing
headers could break downstream consumers.

The location format:

```text
row N, column "X"
```

is operator-friendly.

### Benefits

- Row-level triage is easy.
- Column context is preserved.
- CSV output remains tabular.
- Reports are easy to compare with source rows.

### Tradeoff

CSV is deceptively complex.

Production concerns include:

- Dialect detection.
- Different delimiters.
- Encodings.
- Quoted newlines.
- Duplicate headers.
- Missing headers.
- Very wide rows.
- Formula injection risks when opened in Excel.
- Large files that need streaming and periodic flushing.

The current implementation is appropriate for controlled sample exports. A
production pipeline should add dialect handling and CSV-specific validation.

## 29. Text And Markdown Processing

Text and Markdown files are sanitized as whole documents.

This is straightforward and appropriate.

Line and column locations are useful because operators can open the file and
find the match.

### Tradeoff

Markdown may contain structured content:

- Links.
- Code blocks.
- Tables.
- Frontmatter.
- Inline HTML.

The current processor treats it as text. That is acceptable for sanitization
because PII can appear anywhere. But production systems may need special rules:

- Preserve code examples.
- Avoid replacing inside generated IDs.
- Sanitize link text and URL separately.
- Handle frontmatter schemas.

For the current project, whole-document sanitization is a good simple choice.

## 30. Record Counts

The manifest includes `records_processed`.

The meaning changes by format:

- Text/Markdown: 1.
- CSV: number of data rows.
- JSON: top-level array length.

This is useful but somewhat approximate.

### Benefit

It provides a quick sense of processing volume per file.

### Tradeoff

For JSON, top-level array length may not reflect meaningful record count if the
document is an object, nested structure, or mixed export.

A production version would make record counting processor-specific and possibly
source-specific. For example:

- Slack export: number of messages.
- Jira export: number of issues plus comments.
- Email export: number of messages.
- CSV: number of rows.

This is not critical for the current project, but it matters for real metrics.

## 31. Snippet Design

Snippets provide around 60 characters of context on each side and collapse
whitespace.

They are rendered from sanitized text.

This gives reviewers enough context to understand the finding without leaking
neighboring raw PII.

### Why This Is Good

The snippet is log-safe in a way raw context would not be.

For example:

```text
... vendor not yet onboarded: <UNMAPPED_EMAIL> Vendor escalation line: <UNMAPPED_PHONE> ...
```

This shows the operator where the finding appeared and what kind of context it
had, but it does not expose nearby raw values.

### Tradeoff

The `value` column still carries raw PII. So snippets are safer, but the report
as a whole is still sensitive.

Also, snippets may lose useful context due to:

- Whitespace collapse.
- Limited window size.
- CSV cell boundaries.
- Repeated identical tokens.

Production UX could improve this with a review interface that shows safe context
and requires permission to reveal raw values.

## 32. Testing

The README says the suite has 47 tests covering:

- Baseline scenarios.
- Replacement order.
- Boundary conditions.
- Alias matching.
- Phone regex edge cases.
- Idempotency.
- Config conflicts.
- Missing fields.
- PII reports.
- Snippet privacy.
- Location accuracy.
- Summary-to-row consistency.
- CSV format round trips.
- Validation tampering scenarios.
- Analytics dashboard generation: file is written, embedded JSON
  payload has the expected summary / graph / quarantine shape,
  snippets in the payload don't leak raw PII, and the header
  carries back-links to the other five report artifacts.

This is a strong testing story.

### Why It Matters

The most important tests are not just happy-path tests. They protect invariants:

- Emails are replaced before organization aliases can corrupt domains.
- Long aliases win before short aliases.
- Phone-like substrings inside emails do not double-count.
- ISO timestamps are not phone numbers.
- Snippets do not leak neighboring PII.
- Running twice produces byte-identical sanitized files.
- Validation catches tampering.

These tests show that the project was shaped by concrete edge cases.

### Tradeoff

The tests are likely sample-driven and deterministic. That is good, but
production de-identification also benefits from:

- Property-based tests.
- Fuzz tests for regex edge cases.
- Golden-file tests across full sample exports.
- Mutation testing for validators.
- Larger fixture corpora.
- Realistic messy enterprise data.
- Performance tests.
- Security tests around report leakage.

For the current project, 43 focused tests are a strong signal.

## 33. AI-Assisted Development Notes

The README mentions AI assistance and the need to monitor guardrails.

This is a thoughtful addition. It acknowledges that AI can help scaffold code,
draft regexes, and expand tests, but it can also drift past existing test logic.

The key lesson is:

> Guardrails need guardrails.

That applies both to AI-assisted coding and data sanitization pipelines.

In this project:

- Tests guard code behavior.
- Validation guards run outputs.
- Manifests guard file accounting.
- PII reports guard transformation visibility.
- Quarantine guards uncertainty.

The same pattern appears at multiple levels. That is a good narrative for
interviews because it connects engineering workflow to data pipeline design.

## 34. Major Strengths

### 34.1 Auditability

The pipeline emits enough artifacts for review:

- Summary.
- Manifest.
- Validation report.
- Transformation rows.
- Quarantine rows.
- Interactive analytics dashboard over the same data as the visual
  entry point.

This is much stronger than simply writing sanitized files. The
dashboard in particular shifts the cost of reviewing a run from
"learn `jq` and `csvkit`" to "open one HTML file."

### 34.2 Determinism

Determinism appears throughout:

- Sorted traversal.
- Stable placeholders.
- Stable `value_hash`.
- Stable CSV field order.
- Stable line endings.
- Deterministic JSON formatting.
- Byte-identical sanitized outputs across repeated runs.

This supports testing, review, and incident replay.

### 34.3 Failure Isolation

Bad files do not crash the entire run. They become evidence in the manifest.

### 34.4 Operator Workflow

The quarantine report creates a practical loop:

```text
detect unknown -> mask safely -> report with context -> update config -> rerun
```

### 34.5 Clear Scope

The README is honest about what is supported and what is not. That is better
than overclaiming.

### 34.6 Strong Edge-Case Awareness

Replacement order, alias boundaries, overlapping spans, ISO timestamps, and
snippet privacy show careful implementation.

### 34.7 Good Interview Alignment

The project maps well to data engineering and AI data-prep concerns:

- Cleaning messy enterprise exports.
- Preserving useful relationships.
- Producing safe training data.
- Making transformations reviewable.
- Building deterministic batch pipelines.

## 35. Major Limitations

### 35.1 Detection Recall

The system does not include NER, coreference, or semantic entity resolution.

It will miss:

- Unknown names.
- Unknown organizations.
- Pronouns.
- Roles.
- Nicknames not configured.
- Non-obvious identifiers.
- Addresses.
- Account numbers.
- IDs.
- PII embedded in unsupported files.

### 35.2 Raw Values In Reports

Both PII CSVs carry raw `value`.

That is useful for triage but sensitive. Production would need stronger access
controls.

### 35.3 Plain SHA Hashes

`sha256(value)[:8]` is useful for grouping but not secure against guessing.

Production should use HMAC.

### 35.4 Limited File Support

No PDF, image, spreadsheet, document, archive, or OCR processing.

### 35.5 Limited Validation

Validation checks email and phone leaks, output existence, and manifest
completeness. It does not prove full de-identification correctness.

### 35.6 Single-Process Runtime

The system reads each file into memory and processes sequentially.

This is fine for the sample but not enough for very large exports.

### 35.7 No Artifact Schema Versioning

The README describes report schemas, but the artifacts should include explicit
schema versions.

### 35.8 No Source-Specific Contracts

Slack, Jira, email, and CSV exports have different invariants. The current
system treats file formats generically.

## 36. Key Decision Tradeoffs

### 36.1 Stdlib Only

**Decision:** Use only the Python standard library at runtime.

**Why it was good:**

- Easy to run.
- Easy to audit.
- No dependency setup.
- Lower supply-chain risk.
- Better for a concise take-home project.

**Tradeoff:**

- Less robust phone parsing.
- No schema validation library.
- No streaming JSON parser.
- No OCR/PDF processing.
- No mature NER/DLP tools.

**What I would say in an interview:**

> I intentionally kept runtime dependencies at zero because the project is about
> auditability and deterministic pipeline behavior. In production, I would add
> targeted dependencies where they materially improve correctness, such as
> libphonenumber, JSON schema validation, document extraction, and NER.

### 36.2 Regex Plus Config

**Decision:** Use regex for emails/phones and config aliases for people/orgs.

**Why it was good:**

- Deterministic.
- Explainable.
- Testable.
- Easy to reason about.
- No probabilistic false confidence.

**Tradeoff:**

- Limited recall.
- Manual config maintenance.
- No semantic understanding.
- No unknown person or organization detection.

**What I would say:**

> I chose deterministic behavior over broad but opaque detection. The system is
> honest: it only claims what it can prove. A production version would add
> model-assisted detection as a reviewed layer, not as an unbounded black box.

### 36.3 Generic Unmapped Placeholders

**Decision:** Use `<UNMAPPED_EMAIL>` and `<UNMAPPED_PHONE>`.

**Why it was good:**

- Makes uncertainty visible.
- Prevents unknown raw values from leaking.
- Keeps validation clean.
- Drives operator triage.

**Tradeoff:**

- Loses distinctness in sanitized output.
- Repeated unknown values are not distinguishable in the sanitized file itself.

**Potential improvement:**

Use visibly unreviewed but distinct HMAC-backed placeholders:

```text
<UNREVIEWED_EMAIL_aedb8969>
```

This preserves distinction while communicating review status.

### 36.4 Manifest As Source Of Truth

**Decision:** Compute run summary totals from the manifest.

**Why it was good:**

- Single source of truth.
- Summary cannot drift from per-file accounting as easily.
- Reviewers can audit details behind totals.

**Tradeoff:**

- Manifest schema stability becomes important.
- Future changes need versioning.

### 36.5 Separate Validation Module

**Decision:** Put validation in its own module.

**Why it was good:**

- Easy to audit.
- Clear separation of processing and checking.
- Builds confidence.

**Tradeoff:**

- Current checks are narrow.
- More comprehensive validation will require a larger validation framework.

### 36.6 Failure Isolation

**Decision:** Catch per-file errors and continue.

**Why it was good:**

- Robust against messy exports.
- Produces partial results and explicit failures.
- Avoids losing the entire run due to one bad file.

**Tradeoff:**

- Partial success can be misused.
- Downstream systems need to honor warnings.

### 36.7 CSV For Row-Level Reports

**Decision:** Use CSV for flat PII reports.

**Why it was good:**

- Excel-friendly.
- Easy for analysts.
- Easy to load into data tools.
- Schema is flat.

**Tradeoff:**

- CSV has quoting edge cases.
- Raw values in CSV are easy to copy and exfiltrate.
- CSV is less self-describing than JSON with schema metadata.

### 36.8 Pretty JSON Output

**Decision:** Write JSON with indentation.

**Why it was good:**

- Human-readable.
- Deterministic.
- Easy to diff.

**Tradeoff:**

- Does not preserve original formatting.
- May change file size significantly.
- Not byte-preserving.

## 37. What I Would Have Done Differently

The current project is strong for its scope. If I were rebuilding it or evolving
it, these are the main things I would change.

### 37.1 Add A Threat Model Earlier

The README does mention raw values in reports and plain SHA limitations, but I
would make the security model explicit near the top.

Questions to answer:

- Who can read sanitized outputs?
- Who can read PII reports?
- Who can read raw inputs?
- Who can edit the config?
- Are pseudonyms reversible?
- Is membership testing a concern?
- How long are reports retained?
- What happens if a report leaks?

Why this matters:

The system has artifacts with different sensitivity levels. Sanitized outputs
are safer than raw inputs, but PII reports still contain raw values. A threat
model would make that distinction impossible to miss.

### 37.2 Add Schema Versions To Artifacts

I would add `schema_version` to:

- `run_summary.json`
- `validation_report.json`
- Manifest rows
- PII CSV metadata or companion metadata file

Why:

The reports are effectively APIs. Once another tool consumes them, fields and
semantics matter. Schema versioning shows that the project is ready to evolve
without breaking consumers silently.

### 37.3 Add A Hardened Mode

I would separate local demo behavior from production behavior.

For example:

```bash
python -m sanitizer --input raw --output out --mode demo
python -m sanitizer --input raw --output out --mode hardened
```

Hardened mode could require:

- HMAC key.
- Separate PII report output path.
- Strict policy for unsupported files.
- Strict policy for failed files.
- Encryption-aware storage.
- No raw-value report unless explicitly enabled.

Why:

Demo defaults are designed for usability. Production defaults should be designed
for safety.

### 37.4 Formalize Processor Interfaces

I would define a processor contract:

- Supported extensions or MIME types.
- Read/parse behavior.
- String traversal behavior.
- Location annotation behavior.
- Write behavior.
- Source-specific validation.

Why:

Adding PDFs, spreadsheets, or Slack-specific processors would be easier and less
risky. It would also communicate that file support is extensible by design.

### 37.5 Add Stronger Validation Invariants

I would expand validation to include:

- Every processed manifest row has a matching output file.
- Every output file has a manifest row.
- Output hashes match actual output bytes.
- PII CSV row totals match summary counts.
- Every transformation row token appears in the corresponding output.
- No raw configured aliases remain in sanitized outputs.
- CSV row counts match input row counts.
- JSON output remains parseable.
- Quarantine rows correspond to unmapped counts.
- Unsupported files trigger configurable policy.

Why:

The current validator is a good start. Production validation should prove more
of the artifact contract.

### 37.6 Add Golden Run Fixtures

I would keep a small fixture input and expected full output artifacts under
tests.

Why:

Golden tests catch accidental artifact drift. They are especially useful for a
deterministic pipeline where output stability is a core feature.

### 37.7 Make Policy Configurable

Right now warning behavior is mostly hard-coded.

I would allow policy like:

```json
{
  "fail_on": {
    "failed_files": true,
    "unsupported_files": false,
    "unmapped_emails": false,
    "unmapped_phones": false,
    "validation_findings": true
  },
  "thresholds": {
    "max_quarantine_rows": 10
  }
}
```

Why:

Different workflows have different tolerance. A local exploratory run may allow
warnings. A production training-data export should probably block on them.

## 38. Possible Enhancements

### 38.1 Near-Term Enhancements

These are relatively small and high-value:

1. Add artifact schema versions.
2. Add config schema validation.
3. Add HMAC-backed `value_hash`.
4. Add `--strict`.
5. Add `--fail-on-unsupported`.
6. Add `--fail-on-quarantine`.
7. Add config hash to run summary.
8. Add code version or git commit to run summary.
9. Add output hash validation.
10. Add golden-output tests.
11. Add README threat model.
12. Add report sensitivity classification.

### 38.2 Medium-Term Enhancements

These require more design:

1. Processor plugin architecture.
2. PDF text extraction.
3. XLSX support.
4. DOCX support.
5. MIME sniffing.
6. Streaming text processing.
7. Streaming or size-gated JSON.
8. Bounded worker pool for many files.
9. Structured logging.
10. Metrics for throughput and redaction density.
11. Per-source schema validators.
12. Policy-as-code for run acceptance.

### 38.3 Advanced Enhancements

These move toward production platform territory:

1. NER-based person and organization detection.
2. Coreference resolution.
3. Human review UI for quarantine.
4. Token vault with scoped access.
5. Reversible pseudonyms under policy.
6. Dataset lineage across input, config, code, and outputs.
7. Model evaluation for PII detection precision and recall.
8. Audit logs for reading sensitive reports.
9. Key rotation for HMAC or tokenization.
10. Multi-tenant isolation.
11. Object storage integration.
12. Workflow states: generated, validated, reviewed, approved, exported.

## 39. What World-Class Looks Like

A world-class version of this project is not simply "more regexes" or "add AI."
It is a governed data sanitization platform.

### 39.1 Detection Quality

World-class detection would combine:

- Deterministic regexes for structured identifiers.
- Configured entity mappings.
- NER for unknown people and organizations.
- Coreference resolution.
- Source-specific parsers.
- Language-aware tokenization.
- Human review loops.
- Continuous evaluation.

It would measure:

- Precision.
- Recall.
- False positives.
- False negatives.
- Performance by file type.
- Performance by source system.
- Performance by language.
- Drift over time.

The current system is deterministic and explainable. World-class keeps that
explainability while expanding recall.

### 39.2 Security

World-class security would include:

- HMAC or token-vault pseudonyms.
- Managed keys.
- Key rotation.
- Encryption at rest.
- Encryption in transit.
- Scoped IAM.
- Separate storage for raw-value reports.
- Audit logs for every access.
- Retention policies.
- Break-glass workflows.
- Least-privilege access.

The key principle:

> Sanitized output, raw input, config, token mappings, and PII reports should not
> all have the same permissions.

### 39.3 Governance

World-class governance would connect every output to:

- Input snapshot.
- Config version.
- Code commit.
- Pipeline version.
- Processor versions.
- Validation report.
- Reviewer approval.
- Export destination.
- Retention policy.

This creates lineage.

If someone asks:

> Why did this value appear in a training dataset?

the system should answer:

- Which raw file it came from.
- Which sanitizer version processed it.
- Which config was used.
- Which validation checks passed.
- Who approved the dataset.
- Where the output was exported.

### 39.4 Operator Experience

CSV is fine for a local project. A world-class system would have a review UI.

The UI would support:

- Grouping quarantine by value hash.
- Showing safe snippets.
- Revealing raw values only with permission.
- Bulk approving mappings.
- Suggesting entity config updates.
- Tracking review status.
- Comparing runs.
- Showing validation failures.
- Assigning review tasks.

The goal is to make the human review loop efficient and auditable.

### 39.5 Scale

World-class runtime would support:

- Many small files.
- Very large files.
- Streaming reads.
- Bounded concurrency.
- Retryable processing.
- Object storage.
- Resumable runs.
- Distributed execution if needed.
- Deterministic final artifact ordering.

The important nuance is that scale should not destroy determinism. Even if files
are processed concurrently, reports should be written in stable order.

### 39.6 Extensibility

World-class extensibility would include:

- Processor SDK.
- Source-specific plugins.
- Schema validators.
- Policy hooks.
- Custom PII detectors.
- Configurable output sinks.
- Versioned artifact contracts.

Adding a new export type should not require rewriting the core pipeline.

### 39.7 Observability

World-class observability would include:

- Structured logs.
- Metrics.
- Traces or lineage IDs.
- Run dashboards.
- Alerting.
- Quarantine volume trends.
- Failure taxonomies.
- Redaction density by source.
- Validation failure rates.
- Processing latency by file type.

Operators should know not just that a run failed, but why it failed and what to
do next.

## 40. Interview Talking Points

### 40.1 Strong Opening

Use this framing:

> I built a deterministic local data sanitization pipeline for messy enterprise
> exports. The actual de-identification mechanism is intentionally simple:
> regexes and a config-driven alias map. The engineering focus is the pipeline
> around it: deterministic traversal, failure isolation, manifesting, validation,
> and row-level audit artifacts that let a reviewer trust the run.

### 40.2 Best Technical Detail To Highlight

The three-phase de-identification design:

> The subtle bug I wanted to avoid was computing locations after replacements
> had shifted the text. So I scan the original text for findings and offsets,
> then perform replacement, then render snippets from the fully sanitized output.
> That keeps locations honest and prevents neighboring raw PII from leaking into
> snippets.

### 40.3 Best Product Detail To Highlight

The quarantine loop:

> Unknown emails and phones are not made to look like reviewed pseudonyms. They
> become obvious unmapped placeholders in the sanitized output and structured
> rows in `pii_quarantine.csv`, which gives operators a concrete triage loop:
> review, update config, rerun.

### 40.4 Best Systems Detail To Highlight

Determinism:

> The pipeline sorts traversal, uses stable placeholders, stable CSV field
> order, stable line endings, and content-derived hashes. That makes the
> sanitized outputs byte-identical across repeated runs on the same input and
> config, which is critical for regression tests and incident replay.

### 40.5 Best Security Caveat To Acknowledge

Raw values and SHA:

> The row-level PII reports intentionally carry raw values for triage, so they
> need stricter permissions than sanitized outputs. Also, the current
> `value_hash` is a plain SHA prefix for deterministic grouping, not a secret
> pseudonym. In production I would switch that to HMAC with a managed key.

### 40.6 Best Limitation To Acknowledge

Detection recall:

> The system is intentionally deterministic and explainable, but it does not do
> NER or coreference. It catches configured aliases plus email and phone regexes.
> A production system would add model-assisted detection and human review while
> preserving the deterministic audit trail.

## 41. How To Explain The Project In One Minute

Here is a concise spoken version:

> I built a local Python pipeline that sanitizes exported enterprise data. It
> walks an input folder deterministically, processes supported text-like formats
> like text, markdown, JSON, and CSV, and writes a mirrored sanitized output tree.
> Known people, organizations, emails, and phones are replaced using a config,
> while unknown emails and phones are masked and routed to a quarantine report.
>
> The main design focus was trust. Every input file gets a manifest row,
> malformed files are isolated instead of crashing the run, every PII replacement
> gets a row-level audit record with location and safe snippet, and a separate
> validation pass re-scans the written outputs and checks manifest completeness.
> Each run also renders an interactive `analytics.html` dashboard over the
> same data — run-stat tiles, an entity ↔ file network graph, and a quarantine
> triage panel — so a reviewer can land on one file and form a complete
> picture before drilling into the structured reports. The pipeline is
> deterministic so repeated runs produce byte-identical sanitized outputs,
> which makes review, CI, and incident replay much easier.
>
> The tradeoff is that the de-identification engine is deliberately simple:
> regex plus config, not full NER or document extraction. In production I would
> add HMAC-backed pseudonyms, stricter access controls for raw-value reports,
> more file processors, source-specific validators, observability, and an
> operator review UI.

## 42. How To Explain It In Five Minutes

If you have more time, structure it like this:

1. **Problem**
   - Enterprise exports are messy.
   - Sanitization is not enough by itself.
   - Reviewers need evidence.

2. **Architecture**
   - CLI.
   - Deterministic walker.
   - Extension dispatch.
   - Format-aware processors.
   - Config-driven de-identifier.
   - Mirrored output tree.
   - Reports and validation.

3. **De-identification**
   - Emails, phones, persons, organizations.
   - Replacement order.
   - Longest alias first.
   - Lookaround boundaries.
   - Three-phase scan/replace/snippet.

4. **Auditability**
   - Manifest.
   - Run summary.
   - Transformation CSV.
   - Quarantine CSV.
   - Validation report.
   - Interactive `analytics.html` dashboard rendered per run as the
     visual entry point over all of the above.

5. **Tradeoffs**
   - Deterministic and explainable, but limited recall.
   - Stdlib-only, but fewer mature parsing tools.
   - Raw-value reports are useful, but sensitive.
   - Failure isolation is robust, but downstream policy must respect warnings.

6. **Production hardening**
   - HMAC.
   - Token vault.
   - File processors.
   - NER/coreference.
   - Access controls.
   - Observability.
   - Lineage.
   - Review UI.

## 43. The Most Important Nuances

If you only remember a few details, remember these:

1. **The project is an audit pipeline, not just a sanitizer.**
   - The evidence artifacts are the real differentiator.

2. **Determinism is a product feature.**
   - Stable outputs make review, CI, and incident replay possible.

3. **The three-phase de-id design solves real bugs.**
   - Original offsets stay honest.
   - Sanitized snippets avoid neighboring PII leaks.

4. **Quarantine is a workflow, not a dump.**
   - Unmapped findings become a structured operator backlog.

5. **Failure isolation is useful but must be paired with policy.**
   - Partial success is only safe if warnings are respected.

6. **Raw-value reports are sensitive.**
   - They need stricter controls than sanitized outputs.

7. **Regex plus config is intentionally explainable but incomplete.**
   - Production needs stronger detection and evaluation.

8. **The visual layer is an entry point, not a replacement.**
   - `analytics.html` makes the run reviewable at a glance, but it
     reads from the same data the structured reports carry. Both
     coexist on purpose: the dashboard is for humans, the JSON/CSV
     artifacts are for tooling and downstream consumers.

## 44. Final Assessment

This is a strong interview project because it shows judgment beyond the obvious
implementation.

The obvious implementation would be:

```text
walk files -> replace PII -> write outputs
```

Your implementation is closer to:

```text
walk files deterministically
classify support honestly
isolate failures
sanitize with stable mappings
record row-level evidence
quarantine uncertainty
write reproducible artifacts
validate after the fact
summarize for operators
render a visual evidence layer over all of the above
test the invariants
```

That is a much better story.

The biggest production gaps are also clear:

- Detection recall is limited.
- Raw-value reports need stronger security.
- Plain SHA hashes should become HMAC.
- Unsupported file types are a major real-world gap.
- Validation should become broader.
- Artifact contracts should be versioned.
- Processing should eventually support streaming and concurrency.
- Operator review should move beyond CSV.

The right way to describe the project is not as a finished production DLP
platform. It is a well-scoped, deterministic, auditable local sanitization
pipeline that demonstrates the foundations of one.

The world-class version would preserve the strongest current properties:

- Determinism.
- Auditability.
- Failure isolation.
- Row-level evidence.
- Safe snippets.
- Quarantine workflow.
- Validation.
- Visual evidence layer (`analytics.html`) as the per-run entry
  point.

and then add:

- HMAC/token vault security.
- NER and coreference.
- More file processors.
- Source-specific validation.
- Strong access controls.
- Observability.
- Lineage.
- Human review UX.
- Scalable runtime.
- Policy-driven release gates.

That is the evolution path: keep the trustworthy core, then expand capability
around it without sacrificing the evidence trail.
