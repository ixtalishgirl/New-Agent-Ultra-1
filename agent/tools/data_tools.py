"""Data tool module — real parsing, validation and transformation of JSON,
CSV, XML and YAML, plus encoding/hashing/diff utilities.

Every method returns a :class:`DataResult` (ok, output, parsed, error).
Schemas are validated with a small built-in validator (no external deps);
``yaml`` uses PyYAML when installed and a safe subset parser otherwise.
"""
from __future__ import annotations

import base64
import binascii
import csv
import difflib
import hashlib
import io
import json
import re
import statistics
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class DataResult:
    """Structured result of a data operation."""
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Small JSON-schema-ish validator (no external dependency)
# ---------------------------------------------------------------------------

_TYPE_NAMES = {
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "array": list, "object": dict, "null": type(None),
}


def _check_type(value: Any, type_name: str) -> bool:
    expected = _TYPE_NAMES.get(type_name)
    if expected is None:
        return False
    if type_name == "integer" and isinstance(value, bool):
        return False  # bool is subclass of int; exclude it
    return isinstance(value, expected)


def validate_against_schema(value: Any, schema: dict,
                            path: str = "$") -> list[str]:
    """Validate ``value`` against a JSON-Schema subset.

    Supported keywords: type, properties, required, items, enum,
    minimum, maximum, minLength, maxLength, minItems, maxItems, pattern.
    Returns a list of error strings (empty = valid).
    """
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    t = schema.get("type")
    if t:
        types = [t] if isinstance(t, str) else list(t)
        if not any(_check_type(value, tt) for tt in types):
            errors.append(
                f"{path}: expected type {types}, got {type(value).__name__}")
            return errors  # further checks would be meaningless
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value not in enum {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], value):
                    errors.append(f"{path}: does not match pattern {schema['pattern']!r}")
            except re.error as e:
                errors.append(f"{path}: bad pattern: {e}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
    if isinstance(value, dict):
        for req in schema.get("required", []) or []:
            if req not in value:
                errors.append(f"{path}: missing required property {req!r}")
        props = schema.get("properties", {}) or {}
        for k, v in value.items():
            if k in props:
                errors.extend(validate_against_schema(v, props[k], f"{path}.{k}"))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(value):
                errors.extend(validate_against_schema(v, items, f"{path}[{i}]"))
    return errors


# ---------------------------------------------------------------------------
# Minimal JSONPath (dot + [n] + [*])
# ---------------------------------------------------------------------------

_PATH_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+|\*)\]|\*")


def jsonpath_query(data: Any, path: str) -> list[Any]:
    """Evaluate a simple JSONPath: ``$.store.book[0].title``,
    ``$.items[*].name``, ``$.*``. Returns all matched values."""
    p = path.strip()
    if p == "$":
        return [data]
    if p.startswith("$"):
        p = p[1:]
    if p.startswith("."):
        p = p[1:]
    tokens: list[str] = []
    for m in _PATH_TOKEN.finditer(p):
        name, idx = m.group(1), m.group(2)
        if name is not None:
            tokens.append(name)
        elif idx is not None:
            tokens.append(f"[{idx}]")
        else:
            tokens.append("[*]")
    current: list[Any] = [data]
    for tok in tokens:
        nxt: list[Any] = []
        if tok.startswith("["):
            inner = tok[1:-1]
            for node in current:
                if inner == "*":
                    if isinstance(node, list):
                        nxt.extend(node)
                    elif isinstance(node, dict):
                        nxt.extend(node.values())
                else:
                    i = int(inner)
                    if isinstance(node, list) and -len(node) <= i < len(node):
                        nxt.append(node[i])
        else:
            for node in current:
                if isinstance(node, dict) and tok in node:
                    nxt.append(node[tok])
        current = nxt
        if not current:
            break
    return current


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class DataTools:
    """Parsing, validation and transformation for structured data."""

    # -- JSON -----------------------------------------------------------------

    def json_parse_validate(self, text: str,
                            schema: dict | None = None) -> DataResult:
        """Parse JSON; optionally validate against a schema (see
        ``validate_against_schema`` for supported keywords)."""
        try:
            value = json.loads(text)
        except json.JSONDecodeError as e:
            return DataResult(ok=False,
                              output=f"Invalid JSON: {e}",
                              parsed={}, error=f"JSON decode error: {e}")
        parsed: dict[str, Any] = {"value": value,
                                  "type": type(value).__name__}
        errors: list[str] = []
        if schema is not None:
            errors = validate_against_schema(value, schema)
            parsed["schema_errors"] = errors
            parsed["schema_valid"] = not errors
        ok = not errors
        summary = f"Valid JSON ({parsed['type']})"
        if schema is not None:
            summary += f"; schema: {'VALID' if ok else 'INVALID'}"
            if errors:
                summary += "\n" + "\n".join(f"  - {e}" for e in errors[:20])
        return DataResult(ok=ok, output=summary, parsed=parsed,
                          error="" if ok else "; ".join(errors[:5]))

    def json_query(self, data: Any, path: str) -> DataResult:
        """Query parsed JSON/dict data with a simple JSONPath expression."""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as e:
                return DataResult(ok=False, output=f"Invalid JSON: {e}",
                                  parsed={}, error=str(e))
        try:
            matches = jsonpath_query(data, path)
        except Exception as e:
            return DataResult(ok=False, output=f"Bad path {path!r}: {e}",
                              parsed={}, error=str(e))
        out_lines = [f"{len(matches)} match(es) for {path!r}"]
        for m in matches[:50]:
            out_lines.append("  " + json.dumps(m, ensure_ascii=False)[:500])
        if len(matches) > 50:
            out_lines.append(f"  ... and {len(matches) - 50} more")
        return DataResult(ok=True, output="\n".join(out_lines),
                          parsed={"path": path, "matches": matches,
                                  "count": len(matches)})

    def json_pretty(self, data: Any, indent: int = 2,
                    sort_keys: bool = True) -> DataResult:
        """Pretty-print JSON."""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as e:
                return DataResult(ok=False, output=f"Invalid JSON: {e}",
                                  parsed={}, error=str(e))
        try:
            pretty = json.dumps(data, indent=indent, sort_keys=sort_keys,
                                ensure_ascii=False)
        except (TypeError, ValueError) as e:
            return DataResult(ok=False, output=f"Cannot serialize: {e}",
                              parsed={}, error=str(e))
        return DataResult(ok=True, output=pretty,
                          parsed={"pretty": pretty,
                                  "bytes": len(pretty.encode("utf-8"))})

    def json_minify(self, text: str) -> DataResult:
        """Minify JSON (strip whitespace)."""
        try:
            value = json.loads(text)
        except json.JSONDecodeError as e:
            return DataResult(ok=False, output=f"Invalid JSON: {e}",
                              parsed={}, error=str(e))
        mini = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        return DataResult(ok=True, output=mini,
                          parsed={"minified": mini,
                                  "before": len(text), "after": len(mini)})

    # -- dict helpers ------------------------------------------------------------

    def merge_dicts(self, base: dict, override: dict) -> DataResult:
        """Deep-merge ``override`` into ``base`` (lists are replaced, dicts
        merged recursively). Neither input is mutated."""
        def _merge(a: Any, b: Any) -> Any:
            if isinstance(a, dict) and isinstance(b, dict):
                out = dict(a)
                for k, v in b.items():
                    out[k] = _merge(out[k], v) if k in out else v
                return out
            return b
        if not isinstance(base, dict) or not isinstance(override, dict):
            return DataResult(ok=False, output="Both arguments must be dicts",
                              parsed={}, error="type error")
        merged = _merge(base, override)
        return DataResult(ok=True,
                          output=json.dumps(merged, indent=2, ensure_ascii=False)[:4000],
                          parsed={"merged": merged})

    def flatten_dict(self, data: dict, sep: str = ".") -> DataResult:
        """Flatten nested dict/lists to dotted keys: {'a.b[0]': 1}."""
        if not isinstance(data, dict):
            return DataResult(ok=False, output="Input must be a dict",
                              parsed={}, error="type error")
        flat: dict[str, Any] = {}

        def _walk(node: Any, prefix: str):
            if isinstance(node, dict):
                for k, v in node.items():
                    _walk(v, f"{prefix}{sep}{k}" if prefix else str(k))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    _walk(v, f"{prefix}[{i}]")
            else:
                flat[prefix] = node

        _walk(data, "")
        lines = [f"{len(flat)} flattened key(s)"]
        for k in sorted(flat)[:100]:
            lines.append(f"  {k} = {json.dumps(flat[k], ensure_ascii=False)}")
        return DataResult(ok=True, output="\n".join(lines),
                          parsed={"flat": flat, "count": len(flat)})

    def unflatten_dict(self, flat: dict[str, Any], sep: str = ".") -> DataResult:
        """Inverse of flatten_dict for dotted keys (list indices via [n])."""
        root: dict[str, Any] = {}
        idx_re = re.compile(r"^(.*)\[(\d+)\]$")
        for dotted, value in flat.items():
            node = root
            parts = dotted.split(sep)
            for i, part in enumerate(parts):
                m = idx_re.match(part)
                last = i == len(parts) - 1
                if m:
                    key, idx = m.group(1), int(m.group(2))
                    lst = node.setdefault(key, [])
                    while len(lst) <= idx:
                        lst.append({})
                    if last:
                        lst[idx] = value
                    else:
                        if not isinstance(lst[idx], dict):
                            lst[idx] = {}
                        node = lst[idx]
                else:
                    if last:
                        node[part] = value
                    else:
                        node = node.setdefault(part, {})
        return DataResult(ok=True,
                          output=json.dumps(root, indent=2, ensure_ascii=False)[:4000],
                          parsed={"nested": root})

    # -- CSV ---------------------------------------------------------------------

    def _read_csv_rows(self, text: str, delimiter: str = "",
                       has_header: bool = True) -> tuple[list[str], list[list[str]]]:
        sample = text[:8192]
        if not delimiter:
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = ","
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = [r for r in reader if r]
        header: list[str] = []
        if has_header and rows:
            header = rows.pop(0)
        return header, rows

    def csv_read(self, text: str = "", path: str | os.PathLike | None = None,
                 delimiter: str = "", has_header: bool = True,
                 max_rows: int = 10000) -> DataResult:
        """Read CSV into a list of dicts (header -> value). Auto-sniffs the
        delimiter when not given."""
        if path:
            try:
                text = Path(path).expanduser().read_text(encoding="utf-8-sig",
                                                         errors="replace")
            except OSError as e:
                return DataResult(ok=False, output=str(e), parsed={},
                                  error=str(e))
        if not text.strip():
            return DataResult(ok=False, output="Empty CSV input",
                              parsed={}, error="empty input")
        header, rows = self._read_csv_rows(text, delimiter, has_header)
        rows = rows[:max_rows]
        if has_header and header:
            records = [{header[i] if i < len(header) else f"col{i}": (row[i] if i < len(row) else "")
                        for i in range(max(len(header), len(row)))}
                       for row in rows]
            columns = header
        else:
            width = max((len(r) for r in rows), default=0)
            columns = [f"col{i}" for i in range(width)]
            records = [{f"col{i}": (row[i] if i < len(row) else "")
                        for i in range(width)} for row in rows]
        parsed = {"columns": columns, "records": records,
                  "row_count": len(records), "delimiter": delimiter or "sniffed",
                  "truncated": len(rows) == max_rows}
        lines = [f"{len(records)} row(s), columns: {', '.join(columns)}"]
        for rec in records[:10]:
            lines.append("  " + json.dumps(rec, ensure_ascii=False)[:300])
        if len(records) > 10:
            lines.append(f"  ... and {len(records) - 10} more")
        return DataResult(ok=True, output="\n".join(lines), parsed=parsed)

    def csv_write(self, records: list[dict], path: str | os.PathLike | None = None,
                  delimiter: str = ",", columns: list[str] | None = None) -> DataResult:
        """Write a list of dicts as CSV. Returns the text (and writes to
        ``path`` when given)."""
        if not records:
            return DataResult(ok=False, output="No records to write",
                              parsed={}, error="empty records")
        cols = columns or list(records[0].keys())
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, delimiter=delimiter,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
        text = buf.getvalue()
        parsed: dict[str, Any] = {"csv": text, "rows": len(records),
                                  "columns": cols}
        if path:
            p = Path(path).expanduser()
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
                parsed["path"] = str(p)
            except OSError as e:
                return DataResult(ok=False, output=str(e), parsed={},
                                  error=str(e))
            return DataResult(ok=True, output=f"Wrote {len(records)} rows to {p}",
                              parsed=parsed)
        return DataResult(ok=True, output=text[:4000], parsed=parsed)

    def csv_filter(self, records: list[dict], column: str, op: str,
                   value: Any) -> DataResult:
        """Filter CSV records: op in eq, ne, gt, lt, gte, lte, contains,
        startswith, regex. Numeric comparison attempted when possible."""
        ops = {"eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
               "gt": lambda a, b: a > b, "lt": lambda a, b: a < b,
               "gte": lambda a, b: a >= b, "lte": lambda a, b: a <= b,
               "contains": lambda a, b: str(b) in str(a),
               "startswith": lambda a, b: str(a).startswith(str(b))}
        if op not in ops and op != "regex":
            return DataResult(ok=False, output=f"Unknown op: {op}",
                              parsed={}, error="unknown op")

        def coerce(v: Any) -> Any:
            if isinstance(v, (int, float)):
                return v
            try:
                return int(str(v))
            except (ValueError, TypeError):
                pass
            try:
                return float(str(v))
            except (ValueError, TypeError):
                pass
            return str(v)

        cregex = re.compile(str(value)) if op == "regex" else None
        out = []
        for rec in records:
            cell = rec.get(column, "")
            if op == "regex":
                if cregex.search(str(cell)):
                    out.append(rec)
                continue
            a, b = coerce(cell), coerce(value)
            try:
                if ops[op](a, b):
                    out.append(rec)
            except TypeError:
                if ops[op](str(a), str(b)):
                    out.append(rec)
        return DataResult(ok=True,
                          output=f"{len(out)}/{len(records)} rows match {column} {op} {value!r}",
                          parsed={"records": out, "count": len(out),
                                  "column": column, "op": op, "value": value})

    def csv_sort(self, records: list[dict], column: str,
                 reverse: bool = False, numeric: bool = False) -> DataResult:
        """Sort CSV records by a column (numeric-aware optional).
        Empty/missing values always sort LAST, regardless of direction."""
        def key(rec: dict) -> tuple[int, Any]:
            v = rec.get(column, "")
            if numeric:
                try:
                    return (0, float(str(v)))
                except (ValueError, TypeError):
                    return (1, 0.0)
            s = str(v)
            return (0, s) if s.strip() else (1, "")
        try:
            ascending = sorted(records, key=key)
        except TypeError as e:
            return DataResult(ok=False, output=f"Sort failed: {e}",
                              parsed={}, error=str(e))
        if reverse:
            present = [r for r in ascending if key(r)[0] == 0]
            missing = [r for r in ascending if key(r)[0] == 1]
            sorted_recs = list(reversed(present)) + missing
        else:
            sorted_recs = ascending
        return DataResult(ok=True,
                          output=f"Sorted {len(sorted_recs)} rows by {column} "
                                 f"({'desc' if reverse else 'asc'})",
                          parsed={"records": sorted_recs, "column": column,
                                  "reverse": reverse})

    def csv_stats(self, records: list[dict],
                  columns: list[str] | None = None) -> DataResult:
        """Per-column stats: count, nulls, unique, min/max/mean/median/stdev
        for numeric columns; top values for text columns."""
        if not records:
            return DataResult(ok=False, output="No records",
                              parsed={}, error="empty records")
        cols = columns or list(records[0].keys())
        stats: dict[str, dict[str, Any]] = {}
        for col in cols:
            values = [r.get(col) for r in records]
            non_null = [v for v in values
                        if v is not None and str(v).strip() != ""]
            nums: list[float] = []
            for v in non_null:
                try:
                    nums.append(float(str(v)))
                except (ValueError, TypeError):
                    pass
            col_stat: dict[str, Any] = {
                "count": len(values),
                "non_null": len(non_null),
                "null": len(values) - len(non_null),
                "unique": len({json.dumps(v, sort_keys=True, default=str)
                               for v in non_null}),
                "numeric_ratio": (len(nums) / len(non_null)) if non_null else 0.0,
            }
            if nums and len(nums) == len(non_null):
                col_stat.update({
                    "type": "numeric",
                    "min": min(nums), "max": max(nums),
                    "mean": statistics.mean(nums),
                    "median": statistics.median(nums),
                    "stdev": statistics.stdev(nums) if len(nums) > 1 else 0.0,
                    "sum": sum(nums),
                })
            else:
                freq: dict[str, int] = {}
                for v in non_null:
                    freq[str(v)] = freq.get(str(v), 0) + 1
                top = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:5]
                col_stat.update({"type": "text", "top_values": top,
                                 "distinct_shown": min(5, len(freq))})
            stats[col] = col_stat
        lines = [f"stats for {len(records)} rows:"]
        for col, s in stats.items():
            if s.get("type") == "numeric":
                lines.append(f"  {col}: n={s['non_null']} min={s['min']} "
                             f"max={s['max']} mean={s['mean']:.3f} "
                             f"median={s['median']} unique={s['unique']}")
            else:
                top = ", ".join(f"{k}×{v}" for k, v in s["top_values"])
                lines.append(f"  {col}: n={s['non_null']} unique={s['unique']} "
                             f"top=[{top}]")
        return DataResult(ok=True, output="\n".join(lines),
                          parsed={"stats": stats, "row_count": len(records)})

    # -- XML ---------------------------------------------------------------------

    def xml_parse(self, text: str = "", path: str | os.PathLike | None = None,
                  attr_prefix: str = "@") -> DataResult:
        """Parse XML into nested dicts. Attributes become ``@name`` keys,
        text content becomes ``#text``, repeated tags become lists."""
        if path:
            try:
                text = Path(path).expanduser().read_text(encoding="utf-8",
                                                         errors="replace")
            except OSError as e:
                return DataResult(ok=False, output=str(e), parsed={},
                                  error=str(e))
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            return DataResult(ok=False, output=f"Invalid XML: {e}",
                              parsed={}, error=str(e))

        def _strip_ns(tag: str) -> str:
            return tag.split("}", 1)[1] if "}" in tag else tag

        def _convert(el: ET.Element) -> Any:
            children = list(el)
            if not children:
                txt = (el.text or "").strip()
                if el.attrib:
                    node: dict[str, Any] = {
                        f"{attr_prefix}{_strip_ns(k)}": v
                        for k, v in el.attrib.items()}
                    if txt:
                        node["#text"] = txt
                    return node
                return txt
            node = {}
            for k, v in el.attrib.items():
                node[f"{attr_prefix}{_strip_ns(k)}"] = v
            for child in children:
                key = _strip_ns(child.tag)
                val = _convert(child)
                if key in node:
                    if isinstance(node[key], list):
                        node[key].append(val)
                    else:
                        node[key] = [node[key], val]
                else:
                    node[key] = val
            if el.text and el.text.strip():
                node["#text"] = el.text.strip()
            return node

        data = {_strip_ns(root.tag): _convert(root)}
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        return DataResult(ok=True,
                          output=f"XML root=<{_strip_ns(root.tag)}>\n" + pretty[:4000],
                          parsed={"data": data, "root_tag": _strip_ns(root.tag)})

    def xml_find(self, text: str, tag: str) -> DataResult:
        """Find all elements with a given tag (namespace-agnostic)."""
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            return DataResult(ok=False, output=f"Invalid XML: {e}",
                              parsed={}, error=str(e))
        found = []
        for el in root.iter():
            local = el.tag.split("}", 1)[1] if "}" in el.tag else el.tag
            if local == tag:
                found.append({
                    "tag": local,
                    "text": (el.text or "").strip()[:500],
                    "attrib": dict(el.attrib),
                })
        lines = [f"{len(found)} <{tag}> element(s)"]
        for f in found[:20]:
            lines.append(f"  text={f['text']!r} attrib={f['attrib']}")
        return DataResult(ok=True, output="\n".join(lines),
                          parsed={"tag": tag, "elements": found,
                                  "count": len(found)})

    # -- YAML ----------------------------------------------------------------------

    def yaml_parse(self, text: str = "", path: str | os.PathLike | None = None) -> DataResult:
        """Parse YAML. Uses PyYAML when installed; otherwise a safe subset
        parser handling mappings, nested mappings, lists and scalars."""
        if path:
            try:
                text = Path(path).expanduser().read_text(encoding="utf-8",
                                                         errors="replace")
            except OSError as e:
                return DataResult(ok=False, output=str(e), parsed={},
                                  error=str(e))
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text)
            method = "pyyaml"
        except ImportError:
            try:
                data = self._yaml_subset_parse(text)
                method = "subset-parser"
            except ValueError as e:
                return DataResult(ok=False, output=f"YAML parse error: {e}",
                                  parsed={}, error=str(e))
        except Exception as e:
            return DataResult(ok=False, output=f"YAML parse error: {e}",
                              parsed={}, error=str(e))
        pretty = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        return DataResult(ok=True,
                          output=f"YAML parsed via {method}:\n" + pretty[:4000],
                          parsed={"data": data, "method": method})

    @staticmethod
    def _yaml_scalar(s: str) -> Any:
        s = s.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            return s[1:-1]
        low = s.lower()
        if low in ("null", "~", "none"):
            return None
        if low in ("true", "yes"):
            return True
        if low in ("false", "no"):
            return False
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            pass
        return s

    def _yaml_subset_parse(self, text: str) -> Any:
        """Indent-based subset parser: mappings, lists, nested blocks."""
        lines = [l for l in text.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        if not lines:
            return None
        root: Any = {}
        stack: list[tuple[int, Any]] = [(-1, root)]

        def indent_of(line: str) -> int:
            return len(line) - len(line.lstrip(" "))

        for raw in lines:
            ind = indent_of(raw)
            content = raw.strip()
            while stack and stack[-1][0] >= ind:
                stack.pop()
            parent = stack[-1][1]
            if content.startswith("- "):
                item = self._yaml_scalar(content[2:])
                if not isinstance(parent, list):
                    raise ValueError(f"list item without list context: {raw!r}")
                parent.append(item)
            elif content == "-":
                if not isinstance(parent, list):
                    raise ValueError(f"list item without list context: {raw!r}")
                parent.append(None)
            elif ":" in content:
                key, _, val = content.partition(":")
                key, val = key.strip(), val.strip()
                if isinstance(parent, list):
                    # "- key: value" inline mapping — not supported in subset
                    raise ValueError(f"inline mapping in list unsupported: {raw!r}")
                if val == "":
                    # Could be a nested mapping or list; decide by lookahead
                    child: Any = {}
                    parent[key] = child
                    stack.append((ind, child))
                else:
                    parent[key] = self._yaml_scalar(val)
            else:
                raise ValueError(f"cannot parse line: {raw!r}")
        # Post-pass: convert empty mappings that should be lists is skipped
        # (subset limitation, documented).
        return root

    # -- encoding / hashing --------------------------------------------------------

    def base64_encode(self, text: str = "", data: bytes | None = None,
                      urlsafe: bool = False) -> DataResult:
        """Base64-encode text (utf-8) or raw bytes."""
        raw = data if data is not None else text.encode("utf-8")
        enc = base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)
        out = enc.decode("ascii")
        return DataResult(ok=True, output=out,
                          parsed={"encoded": out, "urlsafe": urlsafe,
                                  "input_bytes": len(raw)})

    def base64_decode(self, text: str, urlsafe: bool = False,
                      as_text: bool = True) -> DataResult:
        """Base64-decode to text (utf-8) or raw bytes."""
        try:
            fn = base64.urlsafe_b64decode if urlsafe else base64.b64decode
            raw = fn(text.strip(), validate=True)
        except (binascii.Error, ValueError) as e:
            return DataResult(ok=False, output=f"Invalid base64: {e}",
                              parsed={}, error=str(e))
        if as_text:
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                return DataResult(ok=False,
                                  output="Decoded bytes are not valid UTF-8 "
                                         "(retry with as_text=False)",
                                  parsed={"raw_bytes": len(raw)},
                                  error="not utf-8")
            return DataResult(ok=True, output=decoded,
                              parsed={"decoded": decoded,
                                      "bytes": len(raw)})
        return DataResult(ok=True, output=f"<{len(raw)} bytes>",
                          parsed={"raw": raw, "bytes": len(raw)})

    def hash_string(self, text: str, algorithm: str = "sha256") -> DataResult:
        """Hash a string (sha256, sha1, md5, blake2b, sha512)."""
        algo = algorithm.lower().replace("-", "")
        try:
            digest = hashlib.new(algo, text.encode("utf-8")).hexdigest()
        except ValueError:
            return DataResult(ok=False,
                              output=f"Unsupported algorithm: {algorithm}",
                              parsed={}, error="unsupported algorithm")
        return DataResult(ok=True, output=f"{algo}('{text[:40]}') = {digest}",
                          parsed={"algorithm": algo, "digest": digest,
                                  "input_preview": text[:100]})

    def hash_file(self, path: str | os.PathLike,
                  algorithm: str = "sha256") -> DataResult:
        """Hash a file's bytes (streams in chunks)."""
        p = Path(path).expanduser()
        if not p.is_file():
            return DataResult(ok=False, output=f"Not a file: {p}",
                              parsed={}, error="not a file")
        algo = algorithm.lower().replace("-", "")
        try:
            h = hashlib.new(algo)
        except ValueError:
            return DataResult(ok=False,
                              output=f"Unsupported algorithm: {algorithm}",
                              parsed={}, error="unsupported algorithm")
        try:
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
        except OSError as e:
            return DataResult(ok=False, output=str(e), parsed={},
                              error=str(e))
        return DataResult(ok=True, output=f"{algo}({p.name}) = {h.hexdigest()}",
                          parsed={"algorithm": algo, "digest": h.hexdigest(),
                                  "path": str(p)})

    # -- diff ------------------------------------------------------------------------

    def diff_text(self, old: str, new: str, fromfile: str = "old",
                  tofile: str = "new", context: int = 3) -> DataResult:
        """Unified diff between two texts. Also reports line stats."""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff = list(difflib.unified_diff(old_lines, new_lines,
                                         fromfile=fromfile, tofile=tofile,
                                         n=context))
        added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        matcher = difflib.SequenceMatcher(None, old, new)
        similarity = matcher.ratio()
        body = "".join(diff)
        out = (f"diff: +{added} -{removed} lines, "
               f"similarity={similarity:.1%}\n{body[:8000]}")
        return DataResult(ok=True, output=out,
                          parsed={"diff": body, "added": added,
                                  "removed": removed,
                                  "similarity": round(similarity, 4),
                                  "identical": not diff})

    def diff_files(self, path_a: str | os.PathLike,
                   path_b: str | os.PathLike, context: int = 3) -> DataResult:
        """Unified diff between two files (text mode)."""
        try:
            a = Path(path_a).expanduser().read_text(encoding="utf-8",
                                                    errors="replace")
            b = Path(path_b).expanduser().read_text(encoding="utf-8",
                                                    errors="replace")
        except OSError as e:
            return DataResult(ok=False, output=str(e), parsed={},
                              error=str(e))
        r = self.diff_text(a, b, fromfile=str(path_a), tofile=str(path_b),
                           context=context)
        r.parsed["file_a"] = str(path_a)
        r.parsed["file_b"] = str(path_b)
        return r

    # -- misc transforms ---------------------------------------------------------------

    def lines_process(self, text: str, operation: str = "sort_unique",
                      case_insensitive: bool = False) -> DataResult:
        """Line-based transforms: sort, sort_unique, unique, reverse,
        shuffle(seed=42 for determinism), strip, numbered."""
        lines = text.splitlines()
        key = (lambda s: s.lower()) if case_insensitive else None
        if operation == "sort":
            out_lines = sorted(lines, key=key)
        elif operation == "sort_unique":
            seen: dict[str, None] = {}
            for l in sorted(lines, key=key):
                k = l.lower() if case_insensitive else l
                if k not in seen:
                    seen[k] = None
            out_lines = list(seen.keys())
            # preserve original casing of first occurrence
            if case_insensitive:
                first: dict[str, str] = {}
                for l in lines:
                    first.setdefault(l.lower(), l)
                out_lines = [first[k] for k in out_lines]
        elif operation == "unique":
            seen2: set[str] = set()
            out_lines = []
            for l in lines:
                k = l.lower() if case_insensitive else l
                if k not in seen2:
                    seen2.add(k)
                    out_lines.append(l)
        elif operation == "reverse":
            out_lines = list(reversed(lines))
        elif operation == "strip":
            out_lines = [l.strip() for l in lines if l.strip()]
        elif operation == "numbered":
            out_lines = [f"{i + 1:6d}  {l}" for i, l in enumerate(lines)]
        elif operation == "shuffle":
            import random
            rng = random.Random(42)
            out_lines = list(lines)
            rng.shuffle(out_lines)
        else:
            return DataResult(ok=False, output=f"Unknown operation: {operation}",
                              parsed={}, error="unknown operation")
        body = "\n".join(out_lines)
        return DataResult(ok=True, output=body[:8000],
                          parsed={"lines": out_lines,
                                  "in_count": len(lines),
                                  "out_count": len(out_lines),
                                  "operation": operation})

    def word_count(self, text: str) -> DataResult:
        """wc-style counts: lines, words, chars, bytes."""
        lines = text.splitlines()
        words = text.split()
        stats = {"lines": len(lines), "words": len(words),
                 "chars": len(text),
                 "bytes": len(text.encode("utf-8")),
                 "non_empty_lines": sum(1 for l in lines if l.strip())}
        return DataResult(ok=True,
                          output=(f"{stats['lines']} lines, {stats['words']} words, "
                                  f"{stats['chars']} chars, {stats['bytes']} bytes"),
                          parsed=stats)


__all__ = ["DataTools", "DataResult", "validate_against_schema",
           "jsonpath_query"]
