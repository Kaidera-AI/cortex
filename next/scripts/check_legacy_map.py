"""Check the pinned pg_dump field map, including table WITH-option boundaries."""
import hashlib
import json
from pathlib import Path
import re
import sys

CREATE = re.compile(r'^CREATE TABLE(?: IF NOT EXISTS)? ([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\s*\(', re.M)
CONSTRAINTS = {'CONSTRAINT', 'PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK', 'EXCLUDE'}


def mask_literals(sql):
    """Keep offsets while hiding quoted text/comments from parenthesis counting."""
    result = list(sql)
    index = 0
    while index < len(sql):
        start = index
        if sql.startswith('--', index):
            end = sql.find('\n', index)
            index = len(sql) if end < 0 else end
        elif sql.startswith('/*', index):
            depth = 1; index += 2
            while depth and index < len(sql):
                if sql.startswith('/*', index): depth += 1; index += 2
                elif sql.startswith('*/', index): depth -= 1; index += 2
                else: index += 1
            if depth: raise ValueError('Unterminated SQL comment')
        elif sql[index] in "'\"":
            quote = sql[index]; index += 1
            while index < len(sql):
                if sql[index] == quote:
                    index += 1
                    if index < len(sql) and sql[index] == quote: index += 1
                    else: break
                elif quote == "'" and sql[index] == '\\': index += 2
                else: index += 1
            else: raise ValueError('Unterminated SQL quote')
        elif sql[index] == '$' and (tag := re.match(r'\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$', sql[index:])):
            delimiter = tag.group(); end = sql.find(delimiter, index+len(delimiter))
            if end < 0: raise ValueError('Unterminated SQL dollar quote')
            index = end+len(delimiter)
        else:
            index += 1; continue
        result[start:index] = ' ' * (index-start)
    return ''.join(result)


def parse_fields(sql):
    masked = mask_literals(sql)
    tables = {}
    for match in CREATE.finditer(masked):
        name = match.group(1)
        if name in tables: raise ValueError('Duplicate source table')
        start = match.end(); depth = 1; end = start
        while end < len(sql):
            if masked[end] == '(': depth += 1
            elif masked[end] == ')': depth -= 1
            if depth == 0: break
            end += 1
        if depth: raise ValueError('Unterminated source table')
        fields = []; piece = start; nested = 0
        for index in range(start, end+1):
            char = masked[index] if index < end else ','
            if char == '(': nested += 1
            elif char == ')': nested -= 1
            if char == ',' and nested == 0:
                declaration = sql[piece:index].strip()
                token = re.match(r'^\s*([a-z_][a-z0-9_]*)\s', masked[piece:index], re.I)
                if token and token.group(1).upper() not in CONSTRAINTS:
                    fields.append((token.group(1), declaration))
                elif declaration and not token:
                    raise ValueError('Unsupported declaration in pinned source')
                piece = index+1
        if len({name for name, _ in fields}) != len(fields): raise ValueError('Duplicate source column')
        tables[name] = fields
    if not tables: raise ValueError('No source tables')
    return tables


def inspect_artifacts(source, mapping, inventory):
    expected = parse_fields(source.decode())
    digest = hashlib.sha256(source).hexdigest()
    problems = []
    if mapping['source_sha256'] != digest or inventory['sha256'] != digest:
        problems.append('Source digest differs')
    if mapping['source_ref'] != inventory['source_ref'] or mapping['source_file'] != inventory['source']:
        problems.append('Source identity differs')
    rows = mapping['tables']; names = [row['source_table'] for row in rows]
    if len(names) != len(set(names)) or set(names) != set(expected): problems.append('Mapped table set differs or duplicates')
    if len(inventory['tables']) != len(set(inventory['tables'])) or set(inventory['tables']) != set(expected): problems.append('Inventory table set differs or duplicates')
    if mapping['source_tables'] != len(expected): problems.append('Declared table count differs')
    by_name = {row['source_table']: row for row in rows}
    for table, fields in expected.items():
        row = by_name.get(table, {'source_columns': []})
        actual = [(column['name'],column['source_declaration']) for column in row['source_columns']]
        if len({name for name,_ in actual}) != len(actual): problems.append(table+': duplicate column names')
        if actual != fields: problems.append(table+': field declarations differ from own table body')
    count = sum(len(fields) for fields in expected.values())
    if 'source_field_count' in mapping and mapping['source_field_count'] != count: problems.append('Declared field count differs')
    return {'source_sha256':digest,'source_tables':len(expected),'source_field_count':count,
            'mapped_fields':sum(len(row['source_columns']) for row in rows),'problems':problems}


def main():
    result = inspect_artifacts(Path(sys.argv[1]).read_bytes(), json.loads(Path(sys.argv[2]).read_text()), json.loads(Path(sys.argv[3]).read_text()))
    print(json.dumps(result,indent=2))
    return 1 if result['problems'] else 0


if __name__ == '__main__': raise SystemExit(main())
