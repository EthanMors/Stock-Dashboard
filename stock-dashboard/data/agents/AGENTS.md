# News Intelligence Agents

## Agent Pipeline

```
fetch_news() → HeadlineProcessor → scrape_article() → Summarizer → StrategicAnalyst
```

Each stage fails gracefully. Downstream stages receive whatever the previous stage produced.

## Agent Descriptions

| Agent | Model | Role |
|-------|-------|------|
| HeadlineProcessor | gemini-2.0-flash-lite | Deduplicates article titles, selects best source per unique story |
| Summarizer | gemini-2.0-flash | Scrapes and summarizes each selected article (2-3 sentences) |
| StrategicAnalyst | claude-sonnet-4-6 | Interprets news for stock impact, industry implications, and peer effects |

## Source Priority List

Used by HeadlineProcessor to select the best source when multiple articles cover the same story.
**Lower number = higher priority.** When two articles cover the same topic, keep the one with the lowest rank.
Tie-break: if same rank, keep the article with the longer description field (proxy for more complete coverage).

| Priority | Domain              | Type                        | Rationale                                      |
|----------|---------------------|-----------------------------|------------------------------------------------|
| 1        | reuters.com         | Wire service                | Fastest, most factual, no editorial slant      |
| 2        | apnews.com          | Wire service                | Authoritative, neutral, trusted globally       |
| 3        | marketwatch.com     | Major financial outlet      | Original market reporting, strong editorial    |
| 4        | cnbc.com            | Major financial outlet      | Breaking market news, original analysis        |
| 5        | morningstar.com     | Major financial outlet      | Deep fundamental analysis, independent ratings |
| 6        | investors.com       | Major financial outlet      | IBD — earnings-focused, institutional quality  |
| 7        | thestreet.com       | Major financial outlet      | Markets-focused original reporting             |
| 8        | investopedia.com    | Financial education         | Clear, accurate definitions and context        |
| 9        | kiplinger.com       | Financial education         | Practical investor focus                       |
| 10       | fool.com            | Financial education         | Long-term focus, accessible analysis           |
| 11       | zacks.com           | Financial analysis          | Quantitative earnings/ratings focus            |
| 12       | nasdaq.com          | Exchange editorial          | Exchange-native market coverage                |
| 13       | businessinsider.com | Business media              | Broad business coverage, good sourcing         |
| 14       | finance.yahoo.com   | Aggregator / original       | Mix of aggregated and original content         |
| 15       | benzinga.com        | Financial media / PR        | High volume, mixed quality, often PR-adjacent  |
| 16       | prnewswire.com      | Press release wire          | Company-authored; no editorial filter          |
| 17       | globenewswire.com   | Press release wire          | Company-authored; no editorial filter          |

### Why Press Releases Are Lowest Priority

prnewswire.com and globenewswire.com carry company-authored press releases. They are primary sources
for corporate announcements (earnings, acquisitions, product launches) but have no editorial oversight.
When a wire service or financial outlet has already reported on the same announcement, prefer their
version — it adds context, analyst reaction, and verification. Only keep a press release if no other
source covered the story.

## Cache TTL

| Function | TTL | Location |
|----------|-----|----------|
| fetch_news() | 900s (15 min) | @st.cache_data in news_fetcher.py |
| scrape_article() | 900s (15 min) | @st.cache_data in news_fetcher.py |
| run_pipeline() | 1800s (30 min) | @st.cache_data in orchestrator.py |
| get_stock_info() | 3600s (1 hr) | @st.cache_data in fetcher.py |
