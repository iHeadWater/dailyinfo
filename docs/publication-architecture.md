# Canonical Publication Architecture (Phase 2A / 2B / 2B-F / 2C / 2D)

DailyInfo now has a delivery-independent publication boundary:

```text
pipeline result
    -> StructuredPublicationAdapter
    -> PublicationFinalizer
    -> validation + relationship linking
    -> PublicationBundle
    -> PublicationStore
    -> Publisher / Delivery State
        ├── DiscordPublisher
        └── WebPublisher -> dailyinfo-web generated content
```

The existing `briefings/` and `pushed/` directories remain unchanged. The
canonical layer is additive; finalization itself does not send Discord
messages, write `dailyinfo-web`, or perform Git operations. Phase 2C adds
separate sink delivery state without putting it into canonical publication
objects.

## Contract v1

`SCHEMA_VERSION` is `1`. The only canonical categories are:

```text
papers, ai_news, code, resource, arxiv
```

Unknown schema versions and categories fail closed.

### Item

An `Item` contains `id`, `category`, title, nested source metadata, authors,
source publication time, retrieval/publication lifecycle times, summary,
optional significance note, tags, language, and `briefing_ids`.

Item identity is resolved in this order:

1. an explicitly supplied canonical id;
2. an arXiv base id, when available (revision suffixes such as `v2` are
   removed);
3. a SHA-256 digest of a stable external id (for example GitHub owner/repo or
   a feed GUID), using the canonical source namespace in the hash material;
4. a SHA-256 digest of the canonical public item URL, using the canonical
   source namespace in the hash material.

Titles, summaries, timestamps, random UUIDs, and file paths never create an
identity. The URL fallback removes fragments and common analytics query
parameters, but retains other query parameters. The finalizer stores this
canonical public URL in source metadata as well, so tracking-only URL changes
do not create false publication updates.

For source-derived identities, the identity material is
`external:{source_namespace}:{external_id}`. The namespace is a stable,
lowercase machine key derived from the configured source key; separators are
removed, and arXiv aliases use the `arxiv` namespace. Therefore display-name
variants such as `OpenReview` and `Open Review` do not create separate
namespaces, while two different source namespaces with the same external ID
cannot collide. Explicit IDs are still syntax-validated and remain subject to
Store collision and category checks.

Known upstream identities are normalized before resolution: arXiv ids use the
base id, DOI values use a lower-case bare DOI, RSS GUIDs are retained under the
configured source namespace, and GitHub/HuggingFace/DLUT identifiers are
source-derived facts. A malformed known identifier is not hashed as if it were
valid; the resolver uses the deterministic URL fallback when one is available.

`id` and `category` are immutable identity fields. Store writes reject an item
with an existing id in another category as an identity migration. Other item
fields can update when their semantic content changes.

### Briefing

The briefing id is always `{category}-{YYYY-MM-DD}`. The pair `(category,
date)` therefore has one canonical briefing. Re-finalization updates the
existing record; it cannot create a `-v2` record.

Briefing `body` is the complete existing DailyInfo Markdown. It is stored as
content, not parsed back into structured items. The ordered `item_ids` list is
the editorial order; authors preserve source order, tags are de-duplicated and
sorted, and the bundle's `items` list is serialized in id order for
determinism.

All internal datetimes are timezone-aware UTC and serialize as ISO-8601 with a
`Z` suffix. Date-only source values are interpreted in the configured business
timezone (`Asia/Shanghai` by default) and then normalized to UTC. A briefing
date is a calendar date and is not inferred from a host-local `datetime.now()`.

## Publication Field Semantics

`schema_version` is contract metadata and is fixed at `1`. The remaining Item
fields have these frozen meanings:

| Field | Class | Mutable | Semantic hash | Persistence |
| --- | --- | --- | --- | --- |
| `id`, `category` | Identity | No | Yes | Always; category migration rejected |
| `title`, `source`, `source_published_at`, `authors`, `summary`, `why_it_matters`, `tags`, `language` | Semantic content | Yes | Yes | On content change |
| `briefing_ids` | Relationship | Yes; set-like | No | On membership change; sorted deterministically |
| `retrieved_at` | Lifecycle metadata | Yes; latest retrieval | No | Persist latest supplied value |
| `published_at` | Lifecycle metadata | Restricted | No | Set on create; preserve first stored value |
| `updated_at` | Lifecycle metadata | Yes; caller-supplied record update time | No | Persist supplied value; preserve existing value when omitted |

For Briefing, `id` and `category` are identity fields; `item_ids` is ordered
editorial relationship/composition metadata and is included in the
Briefing/Bundle semantic hash; `generated_at`, `published_at`, and
`updated_at` are lifecycle metadata. Authors preserve source order, tags are
de-duplicated and sorted, and Bundle items are serialized by Item ID.

Lifecycle policy is explicit: an existing Item receives the latest incoming
`retrieved_at`; its first `published_at` is retained. A new `updated_at` is
persisted when supplied, while an omitted value preserves the previous one.
No clock is consulted by the Finalizer.

### Source time semantics

The three publication-related timestamps have deliberately separate meanings:

| Field | Meaning | Producer | Optional | Hash |
| --- | --- | --- | --- | --- |
| `source_published_at` | Upstream publish/create/first-public timestamp | datasource/source metadata | Yes; `null` when the source does not provide a reliable value | Yes |
| `retrieved_at` | When DailyInfo actually fetched/observed the item | retrieval boundary | No | No |
| `published_at` | First time DailyInfo finalized the canonical Item/Briefing | Finalizer/Store lifecycle | No | No |

An observation date, trending date, briefing date, or retrieval clock is never
used as `source_published_at`. In particular, GitHub Trending and HuggingFace
listing observations have no source publication time in the current adapters,
so their canonical value is `null`. A consumer should omit a Source Published
display field when the value is `null`; it must not display `Unknown` or
substitute `retrieved_at`.

The semantic hash includes the true upstream time when present and includes a
stable JSON `null` when absent. Therefore missing time is not silently made
equivalent to a fabricated observation timestamp.

### Stable external identity and provenance

Source facts are captured before structured LLM enrichment. The LLM receives
only `source_ref` for batch correlation and never generates `external_id` or
source timestamps.

| Field | Meaning | Producer |
| --- | --- | --- |
| `source_published_at` | Upstream publish/create/first-public time | datasource extractor |
| `retrieved_at` | DailyInfo retrieval clock | retrieval boundary |
| `published_at` | First canonical finalization time | Finalizer/Store |
| `source.external_id` | Upstream stable identity, normalized before the resolver applies its source namespace to `Item.id` | datasource metadata or deterministic URL parsing |
| `summary` | DailyInfo content summary | structured LLM response |

The current source identity inventory is:

| Source | Stable external identity | Fallback |
| --- | --- | --- |
| arXiv | base arXiv id parsed from feed GUID/item URL | namespaced canonical URL |
| RSS | feed GUID when FreshRSS exposes it | namespaced canonical item URL |
| GitHub | `full_name` (`owner/repo`) | namespaced canonical repo URL |
| HuggingFace | API `id`, preserved as `repo_id` (`namespace/name`) | namespaced canonical repo URL |
| Crossref | DOI, normalized to lower-case bare form | namespaced canonical URL |
| DLUT recruitment | API `item_id` when present | namespaced list/detail URL |

arXiv `v1` and `v2` resolve to one base-paper Item identity, so a revision is
a semantic update. DOI representations such as `https://doi.org/10.X/ABC`,
`doi:10.X/ABC`, and `10.x/abc` resolve to the same normalized identity when
they are valid DOI forms. Different configured source namespaces remain
isolated even when their external strings are equal.

## Structured pipeline boundary (Phase 2B)

`StructuredPublicationAdapter` accepts the current `datasource.Item` shape or
a mapping, but reads item summaries and other canonical fields only from
explicit structured fields. It intentionally ignores `content` as a summary
and never parses final Markdown.

The user-facing `dailyinfo run` now enables the Phase 2B integration. Each
category collects source items and explicit structured LLM enrichments, renders
the legacy Markdown presentation from those same results, and finalizes one
canonical category/date briefing after all of its sources finish:

```text
source Item + source facts
    -> batch-local source_ref prompt
    -> one structured JSON LLM response per batch/item
    -> strict correlation validation
    -> Python Markdown renderer + StructuredPublicationAdapter
    -> PublicationFinalizer
    -> PublicationStore
```

The JSON response must contain exactly one non-empty `summary` for every
`source_ref`. Missing, duplicate, unknown, malformed, or wrong-typed entries
fail closed. The adapter never uses source `content` or generated Markdown as
the canonical summary. `why_it_matters` and `tags` are optional structured
enrichments; the current default language is `zh-CN`, while source facts such
as title, URL, date, source name, and stable external identifiers remain owned
by the source item.

The five production category paths are integrated: regular RSS/scrape/API
sources for `papers` and `arxiv`, deep-content RSS for `ai_news`, the
GitHub/HuggingFace sources for `code`, and DLUT news/recruitment sources for
`resource`. A category has one collector and one finalizer call, so multiple
source files do not create multiple canonical briefings for the same date.
For regular/deep RSS paths, source seen-state is deferred until the atomic
canonical Store write succeeds, so a failed finalization remains retryable.
Direct calls to the old helper functions retain their legacy Markdown-only
behavior for compatibility; `dailyinfo run` is the supported integrated entry
point.

Current source-shape audit:

| Category | Pipeline/source shape retained before finalization | Current gap |
| --- | --- | --- |
| `papers` | RSS items retain source epoch time and FreshRSS GUID when available; Crossref retains publication date and DOI; scrapers expose parsed article dates/URLs; LLM response supplies per-item summary | most journal RSS feeds do not expose authors; feeds without GUID use URL fallback; tags/significance are model-optional |
| `ai_news` | RSS `use_content` items retain source epoch time, title, date, URL, and raw article body; one structured response is generated per article | raw body is retained only as input, not canonical summary; source does not expose authors/tags |
| `code` | GitHub items retain `full_name`; HuggingFace API items retain API `id` as `repo_id`; listing date remains observation metadata; structured response supplies summary | neither current listing source provides a reliable publish/create timestamp; programming-language metadata is not content language; authors are unavailable |
| `resource` | DLUT HTML items retain parsed list/detail publication dates; API recruitment items retain `item_id` and only use an explicit publish/create field for source time | recruitment `startTime` is not treated as publication time; authors are unavailable; malformed/unknown dates or missing public URLs fail publication rather than being fabricated |
| `arxiv` | RSS items retain source epoch time, feed GUID when present, and arXiv id derived from URL/GUID | feed items without a valid id use the namespaced canonical URL fallback; observation date is not source time |

The adapter therefore requires real structured values for required publication
fields. It does not silently use a feed URL, article body, title, or Markdown as
a substitute for missing canonical data.

## Validation and integrity

Validation covers schema, category, stable id syntax, required text, public
HTTP(S) source URLs, timezone-aware timestamps, unique ids, and the complete
briefing/item relationship. Source URLs reject credentials, localhost, local
hostnames, private/internal IPs, and non-HTTP schemes. Public fields reject
obvious authorization headers, bearer tokens, webhook URLs, key-shaped
secrets, stack traces, local absolute paths, and localhost references.

For a bundle:

```text
briefing.item_ids contains item.id
item.briefing_ids contains briefing.id
briefing.category == item.category
```

The store additionally checks all stored records, so a dangling reverse link,
missing object, duplicate identity, or category mismatch makes readback fail
closed.

## Hash semantics and idempotency

`item_content_hash`, `briefing_content_hash`, and `bundle_content_hash` are
SHA-256 over canonical UTF-8 JSON (`sort_keys=True`, compact separators,
`ensure_ascii=False`). Item ordering in a bundle is canonicalized by id, while
briefing item order remains significant.

Semantic hashes include identity/category, source metadata, source publication
time (including deterministic `null` when unavailable), content fields, and briefing composition/body. Item `retrieved_at`, Item
`published_at`, Item `updated_at`, Briefing `generated_at`/`published_at`/
`updated_at`, and Item relationship membership are excluded as
runtime/record metadata. This means a repeated fetch or re-finalize of
unchanged content does not change the semantic hash merely because lifecycle
timestamps changed. Lifecycle changes can still cause a Store write when the
complete persisted representation changes.

Store actions are:

```text
identity absent + valid bundle       -> create
identity present + complete record same -> no-op
identity present + semantic/relationship/lifecycle change -> update
same Item id + different category     -> reject identity migration
```

Therefore a same-hash relationship or lifecycle change is not swallowed by a
hash-only no-op check.

## Store and finalized state

The default root is `WORKSPACE_ROOT/publications`, where `WORKSPACE_ROOT` is
the same environment-aware data root used by the existing scripts. The current
layout is:

```text
publications/
├── items/{category}/{quoted-item-id}.json
└── briefings/{YYYY}/{MM}/{DD}/{category}/
    ├── briefing.json
    └── briefing.md
```

This filesystem layout is not the semantic contract and can later be replaced
by SQLite, object storage, or a Git-backed store without changing identities.
Each JSON and Markdown file is written through a same-directory temporary file,
flush/fsync, and atomic replace. Readback validates JSON and cross-object
integrity; there is no manifest because the canonical briefing and item files
already contain the required identity and relationship metadata without a
second copy that could drift.

An object is `FINALIZED` only after construction, full validation, and complete
store persistence succeed. Discord/Web success is not part of this state.
Phase 2C stores Discord delivery state separately; delivery state remains
deliberately absent from the canonical publication models.

Historical `briefings/` and `pushed/` files are not backfilled in Phase 2A/2B.
Backfill should be a separate, explicitly validated migration because old
Markdown lacks enough structured item metadata for safe reconstruction.

`conference` and `social` are existing legacy runtime categories but are
explicitly outside Publication Contract v1. They are not implicitly mapped to
`papers` or `ai_news`, and bypass this layer until a future contract revision
adds them.

## Publication State vs Delivery State (Phase 2C)

Phase 2C/2D adds a delivery boundary without changing the canonical models:

```text
PublicationStore
    -> PublicationBundle
    -> Publisher
    -> DiscordPublisher
    -> Discord
    -> DeliveryStateStore
```

`PublicationBundle` remains the authoritative description of what DailyInfo
finalized. It has no `discord_pushed`, `web_published`, message id, or delivery
counter. Delivery state describes where that briefing was sent and is stored
independently under `WORKSPACE_ROOT/deliveries/`.

The v1 delivery unit is a briefing, not an Item. Its deterministic identity is:

```text
{briefing_id}:{sink}
```

For example, `papers-2026-08-27:discord` and
`papers-2026-08-27:web` are independent records. The machine sink name is the
lowercase string `discord`; no WebPublisher is implemented in Phase 2C.

### Delivery state contract

Each JSON record contains:

```text
schema_version
briefing_id
sink
status                 # pending | success | failed
attempt_count          # briefing-level attempts, not chunk count
first_attempted_at
last_attempted_at
delivered_at
external_ref
last_error
```

Timestamps are timezone-aware UTC ISO-8601 values. Delivery JSON is validated
on read; unknown schema, wrong identity, malformed timestamps, invalid status,
or sensitive error metadata fail closed. Webhook URLs, authorization headers,
tokens, and local paths are never written to delivery state.

The normal state machine is:

```text
missing -> pending -> success
missing -> pending -> failed -> pending -> success
pending (process interrupted) -> pending -> success|failed
success -> no-op
success --force--> pending -> success|failed
```

`dailyinfo push` checks `success` before calling Discord, so a normal repeated
push makes no HTTP call. A failed attempt remains retryable and increments the
same delivery record's `attempt_count`. `--force` explicitly creates another
attempt while retaining the same delivery identity.

`DiscordPublisher` consumes only the canonical `Briefing.body`. It reuses the
existing Discord transport, Markdown-compatible content, chunking, retry, and
HTTP status handling. A partial chunk send is a failed briefing delivery even
when earlier chunks were accepted by Discord; a retry may duplicate those
earlier chunks.

### Legacy `pushed/` compatibility and failure windows

`pushed/` is retained as a legacy archive and historical success marker. When a
canonical briefing exists but its delivery state is absent, a matching legacy
`pushed/{category}/*{date}*.md` marker bootstraps a success state without
resending the historical briefing. New canonical deliveries archive their old
Markdown files only after external delivery and local success state have been
recorded. Canonical files remain unchanged during delivery.

For a real legacy pending Markdown file without a canonical bundle, the new
push path fails closed instead of sending that Markdown as a substitute. Empty
or placeholder-only legacy input retains the existing no-update notice
behavior. This avoids turning a failed Phase 2B finalization into an accidental
Discord delivery while still preserving historical archive compatibility.

The order for a canonical delivery is:

```text
load and validate canonical bundle
    -> check DeliveryStateStore
    -> atomically record pending attempt
    -> DiscordPublisher sends
    -> atomically record success or failed
    -> best-effort legacy archive maintenance
```

The external Discord request and the local filesystem write cannot be committed
atomically. If Discord accepts a message and the process fails before the local
success record is written, the next retry may send a duplicate. Therefore the
guarantee is best-effort idempotency during normally recorded operation, not
transactional exactly-once delivery. Archive move failure is reported without
changing a recorded delivery success or causing a resend; the next invocation
can retry the archive move because the canonical delivery remains successful.

`dailyinfo run` still produces canonical content only; it does not call a
Publisher. `dailyinfo push` performs Discord delivery and returns non-zero when
a canonical delivery or required local delivery-state operation fails. A
Phase 2D WebPublisher consumes the same `PublicationBundle` and uses `sink=web`
without changing Item identity, Briefing identity, canonical hashes, or Discord
state semantics.

The existing `weekly` recap is outside Publication Contract v1 and continues to
use its legacy Markdown push path until a future contract explicitly includes
that category. The canonical five categories remain fail-closed when a real
pending Markdown file has no corresponding canonical bundle.

## WebPublisher and cross-repository publishing (Phase 2D)

Web delivery is an independent briefing-level sink. Its delivery key is
`{briefing_id}:web`, and `DeliveryCoordinator` skips a recorded success unless
the caller passes `--force`. The WebPublisher never reads legacy
`briefings/`/`pushed/` Markdown and never changes Discord state.

The configured `DAILYINFO_WEB_REPO` must point to a persistent clean checkout of
`dailyinfo-web`. `DAILYINFO_WEB_REMOTE` and `DAILYINFO_WEB_BRANCH` default to
`git@github.com:CylenLC/dailyinfo-web.git` and `main`. The publisher holds a
process lock across fetch, fast-forward, generated-file write, Web validation,
commit, and push. It rejects a wrong checkout root, branch, origin, dirty
worktree, detached head, diverged/non-fast-forward history, and local commits
that are not publisher commits.

Only these generated paths are managed:

```text
src/content/items/generated/{category}/{item-id}.md
src/content/briefings/generated/{YYYY}/{MM}/{DD}/{category}.md
```

Frontmatter is deterministic UTF-8 JSON-compatible YAML. The Item and Briefing
Markdown bodies are generated from canonical structured data; a Briefing body
is copied as presentation content and is never parsed to reconstruct Items.
`source_published_at` and `why_it_matters` are emitted as explicit `null` when
the canonical contract has no value. The Web consumer accepts those nulls and
does not display a fabricated source time. The Web schema's stable-ID contract
is narrower than the legacy Python validator for explicit IDs; therefore the
WebPublisher rejects an uppercase or colon-containing ID before writing it,
rather than rewriting a stable identity or producing an invalid Web path.

Before any commit, the publisher runs `npm run validate`, `npm run test`,
`npm run check`, and `npm run build` in the target checkout. It stages only its
generated paths, creates at most one ordinary `DailyInfo Bot` commit per
Briefing, and pushes only `origin main` without force. A validation, staging,
commit, or push failure is non-successful. A push failure intentionally leaves
the local publisher commit for an auditable retry; a later retry recognizes the
publisher-only local-ahead state and does not create a duplicate content commit.
The transaction never rolls back a commit after it has been created.

`dailyinfo publish --sink web` publishes canonical briefings for the requested
date/categories. `--sink all` invokes Discord and Web independently and returns
non-zero if either sink fails; one sink's success is not rolled back by the
other. Web publishing is not part of `dailyinfo run` in Phase 2D, so a pipeline
run cannot silently create a cross-repository commit. An empty canonical store
is a successful no-op; legacy-only files are not publication input.
