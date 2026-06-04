import httpx
import pandas as pd
from bs4 import BeautifulSoup
from markdownify import markdownify as md
import json
import time
import os
import re
import io
from urllib.parse import urljoin, urlparse
from datetime import datetime

# Configuration
BASE_URL = "https://wiki.metakgp.org"
START_URL = f"{BASE_URL}/w/Special:AllPages"
OUTPUT_FILE = "Crawler/scraped_wiki.jsonl"
VISITED_FILE = "Crawler/scraped_urls.txt"
FAILED_FILE = "Crawler/failed_pages.txt"
USER_AGENT = "GraphMindCrawler/1.0 (+https://wiki.metakgp.org/w/GraphMind)"
REQUEST_DELAY = 1.5

def is_content_page(url):
    """Filter out non-content pages based on the plan."""
    parsed = urlparse(url)
    # Must be on the wiki domain
    if parsed.netloc != "wiki.metakgp.org":
        return False

    path = parsed.path
    # Must be a wiki page (handle both /w/ and /wiki/)
    if not (path.startswith("/w/") or path.startswith("/wiki/")):
        return False

    # Extract page title by removing the prefix
    page_title = path.replace("/w/", "").replace("/wiki/", "")

    # Ignore Special:, File:, Talk:, User:, and other non-content namespaces
    if any(page_title.startswith(prefix) for prefix in ["Special:", "File:", "Talk:", "User:", "Category:", "Template:", "Help:"]):
        return False

    return True

def get_all_pages():
    """Discover all relevant content pages using Special:AllPages."""
    pages = set()
    current_url = START_URL

    print("Discovering pages via Special:AllPages...")
    while current_url:
        try:
            response = httpx.get(current_url, headers={"User-Agent": USER_AGENT}, timeout=40)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')

            # Find all links in the AllPages list
            # Typically they are in the main content area
            content = soup.find("div", id="bodyContent")
            if content:
                for link in content.find_all("a", href=True):
                    full_url = urljoin(BASE_URL, link['href'])
                    if is_content_page(full_url):
                        pages.add(full_url)

            # Find the "Next page" link
            next_link = soup.find("a", string=re.compile(r"Next page"))
            if next_link:
                current_url = urljoin(BASE_URL, next_link['href'])
                time.sleep(REQUEST_DELAY)
            else:
                current_url = None

        except Exception as e:
            print(f"Error discovering pages: {e}")
            break

    return pages

def clean_html(soup):
    """Isolate factual content and remove noise."""
    # Target primary content container
    content_div = soup.find("div", id="content") or soup.find("div", class_="mw-parser-output")
    if not content_div:
        return soup

    # DOM Purge: Destroy the rows causing table gore and email masking
    # We use a list because we'll be modifying the DOM during iteration
    all_rows = content_div.find_all('tr')
    for tr in all_rows:
        if tr.parent is None:
            continue

        row_text = tr.get_text(strip=True).lower()
        if 'previous year grade distribution' in row_text:
            # Double Strike Strategy:
            # 1. Identify the sibling (the actual bar chart)
            next_tr = tr.find_next_sibling('tr')
            # 2. Destroy the sibling FIRST to avoid orphaned DOM nodes
            if next_tr:
                next_tr.decompose()
            # 3. Destroy the header row LAST
            tr.decompose()
        elif 'email' in row_text:
            tr.decompose()

    # Remove script, style and redundant tags
    for element in content_div(["script", "style", "div", "span"], class_="mw-editsection"):
        element.decompose()

    # Remove wiki boilerplate: "Page last edited...", category lists, etc.
    catlinks = soup.find("div", id="catlinks")
    if catlinks:
        catlinks.decompose()

    return content_div

def process_tables(soup):
    """Handle complex tables using pandas and clean up noise (NaNs and empty tables)."""
    tables = soup.find_all("table", class_="wikitable")
    for i, table in enumerate(tables):
        try:
            # Wrap the table HTML string in StringIO
            html_str = str(table)
            dfs = pd.read_html(io.StringIO(html_str))
            if not dfs:
                continue

            df = dfs[0]

            # Replace NaNs with empty strings to avoid 'nan' in output
            df = df.fillna("")

            # Aggressive Empty Table Detection:
            # If the table has content but the data rows are effectively empty
            # (e.g. just headers and then empty cells), decompose it.
            # We check if any cell in the dataframe (excluding headers) has actual content.
            is_empty = df.astype(str).apply(lambda s: s.str.strip()).eq("").all().all()

            # If the DataFrame is empty OR all its values are effectively empty strings
            if df.empty or is_empty:
                table.decompose()
                continue

            # Replace table with its clean markdown representation
            table.replace_with(f"\n\n{df.to_markdown(index=False)}\n\n")
        except Exception as e:
            print(f"Could not process table {i}: {e}")
    return soup

def extract_links(soup):
    """Extract all internal wiki URLs found on the page.
    IMPORTANT: This should be called on the content-only soup to avoid global sidebar links.
    """
    links = []
    for a in soup.find_all("a", href=True):
        href = a['href']
        # Ensure it's an internal wiki link (starts with /w/ or /wiki/) and doesn't contain a colon (:)
        if (href.startswith("/w/") or href.startswith("/wiki/")) and ":" not in href:
            full_url = urljoin(BASE_URL, href)
            links.append({"text": a.get_text(strip=True), "url": full_url})
    return links

def scrape_page(url):
    """Fetch, clean, and transform a single page."""
    try:
        response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'lxml')

        # Metadata
        title = soup.find("h1", id="firstHeading")
        title_text = title.get_text(strip=True) if title else "Unknown Title"

        # Content cleaning - MUST be done before link extraction and table processing
        content_soup = clean_html(soup)

        # Fix Flaw 1: Extract links ONLY from the isolated content body to avoid the sidebar trap
        linked_to = extract_links(content_soup)

        # Table processing
        content_soup = process_tables(content_soup)

        # HTML to Markdown
        markdown_content = md(str(content_soup), heading_style="ATX").strip()

        # Fix Flaw 2: Remove unresolved Wikitext macros like {{{grades}}} or {{{semester}}}
        markdown_content = re.sub(r'\{\{\{.*?\}\}\}', '', markdown_content)

        # Fix Flaw 3: Regex Purge for Empty Headers at the bottom of the page
        # This targets specific boilerplate headers that appear at the end of the content
        boilerplate_pattern = r'(#+\s+(Concepts taught in class|Student Opinion|How to Crack the Paper|Classroom resources|Additional Resources|Time Table)\s*)+$'
        markdown_content = re.sub(boilerplate_pattern, '', markdown_content, flags=re.IGNORECASE | re.MULTILINE)

        # Cleanup excessive whitespace (3 or more newlines to 2)
        markdown_content = re.sub(r'\n{3,}', '\n\n', markdown_content).strip()

        return {
            "url": url,
            "title": title_text,
            "linked_to": linked_to,
            "content": markdown_content,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        print(f"Failed to scrape {url}: {e}")
        return None

def main(limit=None):
    # Ensure output directory exists
    os.makedirs("Crawler", exist_ok=True)

    # Load visited URLs
    visited = set()
    if os.path.exists(VISITED_FILE):
        with open(VISITED_FILE, "r") as f:
            visited = set(line.strip() for line in f if line.strip())

    # Discover all target pages
    all_pages = get_all_pages()
    to_scrape = list(all_pages - visited)

    if limit:
        to_scrape = to_scrape[:limit]

    print(f"Total pages discovered: {len(all_pages)}")
    print(f"Pages remaining to scrape: {len(to_scrape)}")

    with open(OUTPUT_FILE, "a", encoding="utf-8") as out_f, \
         open(VISITED_FILE, "a", encoding="utf-8") as vis_f, \
         open(FAILED_FILE, "a", encoding="utf-8") as fail_f:

        for url in to_scrape:
            print(f"Scraping: {url}")
            data = scrape_page(url)

            if data:
                # Write to JSONL
                out_f.write(json.dumps(data, ensure_ascii=False) + "\n")
                # Update visited
                vis_f.write(url + "\n")
                vis_f.flush()
            else:
                # Log failure
                fail_f.write(url + "\n")
                fail_f.flush()

            time.sleep(REQUEST_DELAY)

if __name__ == "__main__":
    import sys
    limit = None
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            pass
    main(limit)
