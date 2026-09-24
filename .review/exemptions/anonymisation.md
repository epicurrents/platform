# anonymisation — exemption registry

Read by [.review/agents/anonymisation.md](../agents/anonymisation.md) before it flags an occurrence of "anonymous", "anonymised", "anonymisation" or "de-personalised" under its check C1. The rule (AGENTS.md → *De-identification*) is that the platform never applies these words to its own output; the categories below are the contexts in which the words are correct because they describe something else. An occurrence fits a category or it is a finding; the category is named in the report's Exempt list.

## Format

One row per category: the category name the agent cites, what it covers, and the reason it is not a statement about the platform's output. Add a specific location only when an occurrence needs a reason of its own.

## Categories

| Category | Covers | Reason |
|---|---|---|
| `instrument` | The titles and citations of the EDPB Guidelines 02/2026 on Anonymisation, Opinion 05/2014 on anonymisation techniques, the Guidelines 01/2025 on Pseudonymisation, the EHDS and DGA wording, and quotations from them | Naming the instrument the platform is assessed against is not a claim about the output. |
| `finding` | Text describing a deployment's contextual finding or a publisher's statement: `assessment_reference`, the document-kind selector and its explanation in `frontend/src/components/AccessRightsPanel.vue` and `frontend/src/lib/assessment.ts`, the release run's record, the plan's and the assessment's own discussion | A finding is the sharer's, recorded and dated; the text says a sharer *may* conclude it, never that the platform has. |
| `report` | The identifiers `anonymity_report`, `dataset_anonymity_report`, `library.dataset_anonymity_report`, `library.dataset.anonymity_report`, `DatasetAnonymityReportArgs`, the "Dataset anonymity report" operation label, and prose naming that report | The report is the ¶ 41 record of a release's k, m and class sizes: the evidence for a deployment's finding, named for what it measures. |
| `negation` | Statements that the output is *not* anonymous, that the platform *never* anonymises, and tests asserting the word is absent (`test_neither_format_calls_the_file_anonymous`, the export's `data_classification` comment) | The negation is the rule itself. |
| `unauthenticated` | "Anonymous" meaning a caller with no account: `AnonymousUser`, `ANONYMOUS_ALLOWLIST`, "anonymous request", "anonymous share-token reader", `explain_access` printing `anonymous` for a caller with no username | The HTTP sense of the word; it describes the caller, not the data. |
| `alias` | `AnonymizeEDFHeader` in `federation/middleware.py` and the sentence that keeps it importable | A compatibility alias for project pipelines written before the rename; its removal is the breaking change the plan's *Versioning* section names. |
| `discussion` | `docs/anonymisation-compliance.md`, `docs/engineering-notes/anonymisation-compliance-plan.md`, `docs/engineering-notes/channel-deidentification-plan.md`, `docs/engineering-notes/hed-score-integration.md`, the project engineering notes, the agent's own instructions in `.review/agents/anonymisation.md`, and this registry | Where the words are the subject rather than a description. |
