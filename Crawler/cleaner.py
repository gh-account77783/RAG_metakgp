import json
import re
import os

def clean_markdown_table(table_text):
    """
    Converts markdown tables into a more natural language format.
    """
    lines = table_text.strip().split('\n')
    if len(lines) < 2:
        return table_text

    # Robust separator line removal
    cleaned_lines = []
    for line in lines:
        if re.match(r'^\|?[\s\d]*:?-+.*:?-+.*\|?$', line):
            continue
        cleaned_lines.append(line)

    # Split lines into cells and remove empty edge cells from pipe split
    data = []
    for line in cleaned_lines:
        row = [cell.strip() for cell in line.split('|')]
        if row and not row[0]: row.pop(0)
        if row and not row[-1]: row.pop(-1)
        if row:
            data.append(row)

    if not data:
        return ""

    # Case 1: Simple Key-Value Table (2 columns)
    if len(data[0]) == 2:
        result = []
        for row in data:
            if len(row) == 2:
                k, v = row[0], row[1]
                if not v:
                    continue
                v = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', v)
                if k and v:
                    result.append(f"{k}: {v}")
        if result:
            return "\n".join(result)

    # Case 2: Complex Tables
    headers = data[0]
    headers = [h if "Unnamed" not in h else f"Col_{i}" for i, h in enumerate(headers)]

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
    """
    Performs general cleaning of the wiki content.
    """
    if not text:
        return ""

    # 1. Remove "page does not exist" noise from links
    def replace_broken_link(match):
        text_part = match.group(1)
        url_part = match.group(2)
        if 'page does not exist' in url_part:
            return f"[{text_part}]"
        return match.group(0)

    text = re.sub(r'\[(.*?)\]\((.*?)\)', replace_broken_link, text)

    # 2. Remove "Unnamed: X" labels
    text = re.sub(r'Unnamed: \d+', '', text)
    text = re.sub(r'Unnamed: \d+_level_\d+', '', text)

    # 3. Process tables
    current_table = []
    lines = text.split('\n')
    cleaned_lines = []

    for line in lines:
        if line.strip().startswith('|'):
            current_table.append(line)
        else:
            if current_table:
                table_text = '\n'.join(current_table)
                cleaned_table = clean_markdown_table(table_text)
                if cleaned_table:
                    cleaned_lines.append(cleaned_table)
                current_table = []
            cleaned_lines.append(line)

    if current_table:
        table_text = '\n'.join(current_table)
        cleaned_table = clean_markdown_table(table_text)
        if cleaned_table:
            cleaned_lines.append(cleaned_table)

    text = '\n'.join(cleaned_lines)

    # 4. Final cleanup of whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    return text

def main():
    input_path = 'Crawler/scraped_wiki.jsonl'
    output_path = 'Crawler/cleaned_wiki.jsonl'

    if not os.path.exists(input_path):
        print(f"Input file {input_path} not found.")
        return

    print(f"Cleaning data from {input_path}...")

    with open(input_path, 'r', encoding='utf-8') as infile, \
         open(output_path, 'w', encoding='utf-8') as outfile:

        count = 0
        for line in infile:
            try:
                data = json.loads(line)
                data['content'] = clean_content(data['content'])
                outfile.write(json.dumps(data) + '\n')
                count += 1
            except json.JSONDecodeError:
                continue

    print(f"Successfully cleaned {count} entries. Saved to {output_path}.")

if __name__ == "__main__":
    main()
