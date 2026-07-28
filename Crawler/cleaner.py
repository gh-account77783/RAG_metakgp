import json
import os
import re


INPUT_PATH = "Crawler/scraped_wiki.jsonl"
OUTPUT_PATH = "Crawler/cleaned_wiki.jsonl"

def clean_markdown_table(table_text):
    """Convert a Markdown table into compact, searchable text."""
    lines = table_text.strip().split("\n")
    if len(lines) < 2:
        return table_text

    cleaned_lines = [
        line for line in lines
        if not re.match(r"^\|?[\s\d]*:?-+.*:?-+.*\|?$", line)
    ]

    data = []
    for line in cleaned_lines:
        row = [cell.strip() for cell in line.split("|")]
        if row and not row[0]:
            row.pop(0)
        if row and not row[-1]:
            row.pop(-1)
        if row:
            data.append(row)

    if not data:
        return ""

    if len(data[0]) == 2:
        result = []
        for row in data:
            if len(row) == 2:
                k, v = row[0], row[1]
                if not v:
                    continue
                v = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", v)
                if k and v:
                    result.append(f"{k}: {v}")
        if result:
            return "\n".join(result)

    headers = data[0]
    headers = [h if "Unnamed" not in h else f"Col_{index}" for index, h in enumerate(headers)]

    result = []
    for row_idx, row in enumerate(data[1:], 1):
        row_content = []
        for col_idx, cell in enumerate(row):
            if col_idx < len(headers) and cell:
                if not re.match(r'^:?-+$', cell):
                    row_content.append(f"{headers[col_idx]}: {cell}")
        if row_content:
            result.append(f"Row {row_idx}: " + ", ".join(row_content))

    return "\n".join(result) if result else ""

def clean_content(text):
    """Remove known wiki noise and normalize table content."""
    if not text:
        return ""

    def replace_broken_link(match):
        text_part = match.group(1)
        url_part = match.group(2)
        if "page does not exist" in url_part:
            return f"[{text_part}]"
        return match.group(0)

    text = re.sub(r"\[(.*?)\]\((.*?)\)", replace_broken_link, text)
    text = re.sub(r"Unnamed: \d+", "", text)
    text = re.sub(r"Unnamed: \d+_level_\d+", "", text)

    current_table = []
    lines = text.split("\n")
    cleaned_lines = []

    def flush_table():
        if not current_table:
            return
        cleaned_table = clean_markdown_table("\n".join(current_table))
        if cleaned_table:
            cleaned_lines.append(cleaned_table)
        current_table.clear()

    for line in lines:
        if line.strip().startswith("|"):
            current_table.append(line)
        else:
            flush_table()
            cleaned_lines.append(line)

    flush_table()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines)).strip()

def main():
    if not os.path.exists(INPUT_PATH):
        print(f"Input file {INPUT_PATH} not found.")
        return

    print(f"Cleaning data from {INPUT_PATH}...")

    with open(INPUT_PATH, "r", encoding="utf-8") as infile, \
         open(OUTPUT_PATH, "w", encoding="utf-8") as outfile:

        count = 0
        for line in infile:
            try:
                data = json.loads(line)
                data["content"] = clean_content(data["content"])
                outfile.write(json.dumps(data) + "\n")
                count += 1
            except json.JSONDecodeError:
                continue

    print(f"Successfully cleaned {count} entries. Saved to {OUTPUT_PATH}.")

if __name__ == "__main__":
    main()
