---
requires:
  bins: ["blogwatcher"]
---

# Blogwatcher Skill

Blogwatcher is a blog/content monitoring and analysis tool. This skill enables AI agents to interact with and utilize blogwatcher for content discovery and analysis.

## Basic Commands

### Scan for new content
```bash
blogwatcher scan
```
Runs a scan to discover and collect new blog content.

### Check specific feed
```bash
blogwatcher scan --feed <feed_url>
```
Scan a specific RSS or Atom feed URL.

### List watched sources
```bash
blogwatcher list
```
Display all currently monitored content sources.

### Get detailed report
```bash
blogwatcher report --format markdown
```
Generate a detailed analysis report in Markdown format.

## Usage in AI Workflows

When using blogwatcher in an AI agent context:
1. First invoke `blogwatcher list` to see available sources
2. Use `blogwatcher scan` to fetch latest content
3. Parse the output and incorporate findings into the agent's response
4. For targeted analysis, use `blogwatcher scan --feed <url>` for specific sources

## Integration with DailyInfo

In the DailyInfo workflow, blogwatcher can be called by OpenClaw to:
- Automatically scan configured tech news feeds
- Provide structured content summaries for downstream LLM processing
- Feed into the AI news rewriter pipeline via shared workspace
