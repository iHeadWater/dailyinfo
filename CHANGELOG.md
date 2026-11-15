# Changelog

All notable changes to DailyInfo are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version stays below 1.0.0, a minor bump may still change the CLI or
the configuration format. Pushing a `v*` tag publishes the section for that
version as the GitHub Release body (see `.github/workflows/release.yml`), so a
tag without a filled section here is rejected instead of shipping empty notes.

## [Unreleased]

### Added

- Pushing a `v*` tag now publishes the GitHub Release: the workflow runs the
  full suite, refuses a tag that disagrees with `pyproject.toml`, refuses a
  version without a `CHANGELOG.md` section, attaches the sdist and wheel built
  by `uv build`, and uses that section as the release body.
- `CHANGELOG.md` records every release from 0.1.0 on.

### Changed

- `tests.yml` runs on branch pushes and as a reusable workflow; tag pushes reach
  it through the release workflow, so the suite does not run twice per release.

## [0.3.1] - 2026-11-15

### Fixed

- Read-only MCP queries failed on every request under Docker Desktop: the
  consistency signature included `st_ino`, and the virtiofs bind mount returns
  a different inode for the same file on every `stat()`, so two consecutive
  scans never matched. The signature now covers relative path + `mtime_ns` +
  size, and a regression test varies `st_ino` while the file is unchanged.

## [0.3.0] - 2026-11-08

### Added

- Conference pipeline: OpenReview submissions for ICLR 2027, ICML 2027 and
  NeurIPS 2026 land in a dedicated SQLite store together with public reviews,
  ratings, decisions and author responses.
- Review-aware briefing: a deterministic listing plus an optional AI Chinese
  briefing for review-bearing events, delivered to Discord as a
  one-line-per-paper digest while the detailed files stay local.
- Canonical Publication v1 with independent Discord and Web delivery state, and
  a Web checkout pruned to a rolling seven-day window.
- Self-hosted scheduler: supercronic inside the compose stack with catch-up,
  healthcheck, single-writer lock and eight daily jobs at 03:00–07:00
  Asia/Shanghai.
- Read-only MCP over Streamable HTTP with per-user Bearer keys and
  generated/delivered visibility.
- FreshRSS 1.30.4 compose integration, env-controlled bind address/port/image,
  and automatic RSS subscription from `config/sources.json`.
- New sources: AGU Advances, JWRPM, Communications Earth & Environment, and the
  ruanyf weekly feed.
- Weekly GitHub Trending report (`dailyinfo code-weekly`).

### Changed

- The default model is `deepseek-flash`; the fallback moved from
  OpenRouter/Kimi to Zhipu `glm-5.3-flash`.
- Paper titles are kept in their original language instead of being translated.
- Resource pushes surface actionable DLUT notices instead of general school news.
- Web publishing runs as a host-side launchd task at 07:40 rather than inside
  the scheduler container.
- The meeting digest falls back to `DISCORD_CHANNEL_AI_NEWS` when
  `DISCORD_CHANNEL_CONFERENCE` is unset.

### Fixed

- A permanently redirected feed URL no longer creates a duplicate FreshRSS
  subscription.
- Fetch-failure placeholders are regenerated on a re-run instead of being
  treated as content.
- GLM fallback wiring and API-key handling.
- The default batch size never reached the sources.
- The CI decoy `.env` no longer leaks into the compose validation.

## [0.2.0] - 2026-08-06

### Added

- `dailyinfo clean-cache` to clear stale FreshRSS cache files.
- Weekly recap with AI-news event clustering.
- Retry-then-placeholder handling for a failed HuggingFace fetch, matching the
  fault tolerance of the papers pipeline.
- Deduplication of non-RSS paper sources.

### Changed

- DailyInfo became a standalone collector; the README was rewritten as a
  standalone project in English and Chinese, and the docs were regenerated.
- Paths became environment-aware (`dev` / `staging` / `prod`).

### Removed

- Download-PDF, Zotero sync, NotebookLM briefing, figure generation and
  Bilibili publishing moved to the separate mylibrary repository.

### Fixed

- DLUT pagination stops at an expired cursor, and expired recruitment listings
  are filtered out.
- The dropped 30-day seen-cache expiry no longer lets duplicate papers through.
- Discord push retries use exponential backoff.
- `.env` reads and briefing writes pin UTF-8.

## [0.1.0] - 2026-06-13

### Added

- Five pipelines: papers, AI news, arXiv CS.AI, GitHub Trending / HuggingFace
  code, and DLUT university news.
- DeepSeek as the primary model with an OpenRouter Kimi K2.5 fallback.
- Discord push with separate prod / dev / staging environments.
- Configuration-driven sources via `config/sources.json`.
- Idempotent Click CLI with external scheduling.
- FreshRSS Docker-based RSS collection; 150 tests at 80 %+ coverage.
