# Crawler Implementation Plan for MetaKGP

This document outlines the strategy for building the data pipeline to scrape `wiki.metakgp.org` for the GraphMind project.

## 🛠️ Tech Stack
- **HTTP Requests:** `httpx` or `requests`
- **HTML Parsing:** `BeautifulSoup4`
- **Conversion:** `markdownify`
- **Serialization:** `json`
- **URL Management:** `urllib.parse`

---

## 📋 Implementation Phases

### Phase 1: URL Discovery (The Map)
The goal is to build a comprehensive list of all relevant content pages.
1. **`Special:AllPages` Strategy:** Target the `Special:AllPages` endpoint. Implement pagination logic to follow "Next page" links until all pages are discovered.
2. **Recursive Crawling:** Implement a BFS (Breadth-First Search) crawler that starts at the main page and follows internal links.
3. **Filter:** Ignore non-content pages (e.g., `Special:`, `File:`, `Talk:`, `User:`) to ensure only factual data is collected.
4. **Checkpointing:** Maintain a `visited` set and a list of successfully scraped URLs on disk (e.g., `scraped_urls.txt`) to allow resuming after crashes.

### Phase 2: Extraction & Cleaning (The Filter)
Isolate factual content from the MediaWiki UI elements.
1. **Content Isolation:** Identify the primary content container (typically `div#content` or `.mw-parser-output`) and discard sidebars, headers, footers, and navigation menus.
2. **Noise Removal:** Strip out `<script>`, `<style>`, and redundant `<a>` tags. Specifically remove wiki boilerplate such as category lists and "page last edited" footers to reduce RAG noise.
3. **Metadata Extraction:** Capture the page title and the original URL for future citations.

### Phase 3: Transformation & Storage (The Archive)
1. **HTML $\to$ Markdown:** Convert isolated HTML content into clean Markdown using `markdownify`. For complex tables (merged cells, etc.), use `pandas.read_html()` to ensure structured data is preserved for the LLM.
2. **JSONL Formatting:** Package the data into a JSON object per page with the following schema:
   - `url`: The normalized absolute source URL.
   - `title`: The page title.
   - `linked_to`: A list of internal wiki URLs found on the page. Store as a list of objects: `{"text": "anchor_text", "url": "absolute_url"}`. Ensure every extracted href starts with /wiki/ and does not contain a colon (:) before normalizing to an absolute URL.
   - `content`: The cleaned Markdown content.
   - `timestamp`: The date of scraping.
3. **Storage Path:** Save all pages as a single JSONL file at `Crawler/scraped_wiki.jsonl` (one JSON object per line).

### Phase 4: Robustness & Ethics (The Guardrails)
1. **Rate Limiting:** Implement a delay (e.g., 1 second) between requests to avoid triggering 429 errors.
2. **Error Handling:** Use `try-except` blocks for network timeouts and 404s, logging failed URLs to `failed_pages.txt`.
3. **User-Agent:** Set a descriptive `User-Agent` header.

---

## 🚀 Execution Roadmap
1. **Setup:** Install necessary Python libraries.
2. **Develop `crawler.py`:** Implement the discovery and extraction logic.
3. **Run & Validate:** Execute the crawler and verify the contents and structure of `Crawler/scraped_wiki.jsonl`.
4. **Verify Fidelity:** Compare random entries in the .jsonl file.
