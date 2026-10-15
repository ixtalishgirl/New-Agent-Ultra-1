"""Form handling — analysis, fill strategies, validation, and curl replay.

Works on FormData/FormField objects from advanced_extractor, or on plain
dicts with the same shape. All network-relevant output is UNTRUSTED data.
"""
from __future__ import annotations

import re
import shlex
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime


# ---------------------------------------------------------------------------
# Field-type inference
# ---------------------------------------------------------------------------

#: Hints in a field's name/label that imply a semantic type.
_NAME_TYPE_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"e-?mail", re.I), "email"),
    (re.compile(r"(phone|mobile|tel|contact.?no|whatsapp)", re.I), "tel"),
    (re.compile(r"(pass(word)?|pwd)", re.I), "password"),
    (re.compile(r"(zip|postal|pin.?code)", re.I), "postal"),
    (re.compile(r"(country)", re.I), "country"),
    (re.compile(r"(city|town)", re.I), "city"),
    (re.compile(r"(state|province|region)", re.I), "state"),
    (re.compile(r"(address|street|addr)", re.I), "address"),
    (re.compile(r"(first.?name|given.?name|fname)", re.I), "first_name"),
    (re.compile(r"(last.?name|family.?name|surname|lname)", re.I), "last_name"),
    (re.compile(r"(full.?name|your.?name|contact.?name|name)", re.I), "full_name"),
    (re.compile(r"(user.?name|login|handle)", re.I), "username"),
    (re.compile(r"(dob|birth.?date|date.?of.?birth)", re.I), "date"),
    (re.compile(r"(age)", re.I), "number"),
    (re.compile(r"(price|amount|total|cost|salary|budget)", re.I), "number"),
    (re.compile(r"(quantity|qty|count)", re.I), "number"),
    (re.compile(r"(url|website|homepage|site)", re.I), "url"),
    (re.compile(r"(search|query|keyword)", re.I), "search"),
    (re.compile(r"(message|comment|description|bio|notes|details)", re.I), "textarea"),
    (re.compile(r"(subject|title|topic)", re.I), "text"),
    (re.compile(r"(company|organization|org)", re.I), "text"),
    (re.compile(r"(card.?number|cc.?num)", re.I), "card_number"),
    (re.compile(r"(cvv|cvc|security.?code)", re.I), "cvv"),
    (re.compile(r"(expir|exp.?date)", re.I), "card_expiry"),
]

#: Sample values used when auto-generating a fill plan.
_SAMPLE_VALUES: dict[str, str] = {
    "email": "agent@example.com",
    "tel": "+15551234567",
    "password": "Str0ng!Test-Pass-9",
    "postal": "10001",
    "country": "United States",
    "city": "Springfield",
    "state": "Illinois",
    "address": "123 Main Street",
    "first_name": "Alex",
    "last_name": "Rivera",
    "full_name": "Alex Rivera",
    "username": "agent_user",
    "date": "1990-01-15",
    "number": "1",
    "url": "https://example.com",
    "search": "test query",
    "textarea": "Sample text entered by the automated agent for testing.",
    "text": "sample",
    "card_number": "4111111111111111",
    "cvv": "123",
    "card_expiry": "12/30",
}


def infer_field_type(field: dict) -> str:
    """Infer the semantic type of a field from its HTML type, name and label.

    Returns a semantic label like 'email', 'tel', 'first_name', ... which is
    more useful for validation and sample generation than the raw HTML type.
    """
    raw_type = (field.get("field_type") or field.get("type") or "text").lower()
    name = str(field.get("name", "") or "")
    label = str(field.get("label", "") or "")
    placeholder = str(field.get("placeholder", "") or "")
    blob = f"{name} {label} {placeholder}"

    # Direct HTML types win when unambiguous.
    if raw_type in ("email", "tel", "url", "date", "number", "password",
                    "search", "month", "week", "time", "datetime-local",
                    "color", "range", "file", "checkbox", "radio",
                    "select", "textarea", "hidden"):
        # Still refine generic text inputs via name hints.
        if raw_type not in ("text",):
            return raw_type
    elif raw_type.startswith("button"):
        return raw_type

    for pattern, semantic in _NAME_TYPE_HINTS:
        if pattern.search(blob):
            return semantic
    return "text" if raw_type == "text" else raw_type


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_VALIDATORS: dict[str, re.Pattern] = {
    "email": re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"),
    "tel": re.compile(r"^\+?[0-9][0-9\s().-]{5,19}$"),
    "url": re.compile(
        r"^https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d+)?(/.*)?$"),
    "postal": re.compile(r"^[A-Za-z0-9][A-Za-z0-9\s-]{2,11}$"),
    "card_number": re.compile(r"^\d{13,19}$"),
    "cvv": re.compile(r"^\d{3,4}$"),
    "card_expiry": re.compile(r"^(0[1-9]|1[0-2])/\d{2}(\d{2})?$"),
    "username": re.compile(r"^[A-Za-z0-9_.-]{3,64}$"),
    "color": re.compile(r"^#[0-9a-fA-F]{6}$"),
}


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def validate_input(semantic_type: str, value: str,
                   field: dict | None = None) -> ValidationResult:
    """Validate a value against a semantic field type.

    Checks regex format, min/max bounds, maxlength, pattern attribute,
    and required-ness. Returns ValidationResult(ok, errors).
    """
    errors: list[str] = []
    value = "" if value is None else str(value)
    field = field or {}
    label = field.get("label") or field.get("name") or "field"

    if field.get("required") and not value.strip():
        errors.append(f"{label}: value is required")
        return ValidationResult(ok=False, errors=errors)
    if not value.strip():
        return ValidationResult(ok=True)  # optional + empty is fine

    v = value.strip()
    pattern = _VALIDATORS.get(semantic_type)
    if pattern and not pattern.match(v):
        errors.append(f"{label}: {v!r} is not a valid {semantic_type}")

    if semantic_type == "card_number" and not _luhn_ok(v):
        errors.append(f"{label}: card number fails Luhn check")

    if semantic_type in ("number", "range"):
        try:
            num = float(v)
        except ValueError:
            errors.append(f"{label}: {v!r} is not a number")
            num = None
        if num is not None:
            min_v = field.get("min_value") or field.get("min")
            max_v = field.get("max_value") or field.get("max")
            try:
                if min_v not in (None, "") and num < float(min_v):
                    errors.append(f"{label}: {num} below minimum {min_v}")
                if max_v not in (None, "") and num > float(max_v):
                    errors.append(f"{label}: {num} above maximum {max_v}")
            except (TypeError, ValueError):
                pass

    if semantic_type == "date":
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                datetime.strptime(v, fmt)
                break
            except ValueError:
                continue
        else:
            errors.append(f"{label}: {v!r} is not a recognizable date")

    maxlength = field.get("maxlength") or field.get("max_length")
    if maxlength:
        try:
            if len(v) > int(maxlength):
                errors.append(f"{label}: longer than maxlength {maxlength}")
        except (TypeError, ValueError):
            pass

    html_pattern = field.get("pattern")
    if html_pattern:
        try:
            if not re.fullmatch(html_pattern, v):
                errors.append(f"{label}: does not match field pattern")
        except re.error:
            pass  # broken page pattern — ignore

    select_options = field.get("options")
    if semantic_type == "select" and select_options:
        allowed = {str(o.get("value", "")) for o in select_options}
        allowed |= {str(o.get("text", "")) for o in select_options}
        if v not in allowed:
            errors.append(f"{label}: {v!r} not one of the select options")

    return ValidationResult(ok=not errors, errors=errors)


# ---------------------------------------------------------------------------
# Form analysis
# ---------------------------------------------------------------------------

@dataclass
class FormAnalysis:
    action: str
    method: str
    field_count: int
    required_count: int
    fields: list[dict] = field(default_factory=list)
    missing_names: list[int] = field(default_factory=list)  # field indexes
    warnings: list[str] = field(default_factory=list)
    is_login_form: bool = False
    is_search_form: bool = False
    has_file_upload: bool = False


def _as_dict(obj: object) -> dict:
    """Accept FormData/FormField dataclasses or plain dicts."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        d = dict(vars(obj))
        return d
    return {}


class FormHandler:
    """Analyze forms, build fill plans, validate values, replay via curl."""

    def __init__(self, default_values: dict[str, str] | None = None):
        self.default_values = dict(_SAMPLE_VALUES)
        if default_values:
            self.default_values.update(default_values)

    # -- analysis ----------------------------------------------------------
    def analyze_form(self, form: object) -> FormAnalysis:
        """Describe a form: fields, required flags, types, and warnings."""
        f = _as_dict(form)
        raw_fields = f.get("fields", []) or []
        fields: list[dict] = []
        warnings: list[str] = []
        for i, raw in enumerate(raw_fields):
            fld = _as_dict(raw)
            semantic = infer_field_type(fld)
            entry = {
                "index": i,
                "name": fld.get("name", "") or "",
                "html_type": (fld.get("field_type") or fld.get("type")
                              or "text"),
                "semantic_type": semantic,
                "label": fld.get("label", "") or "",
                "required": bool(fld.get("required", False)),
                "value": fld.get("value", "") or "",
                "placeholder": fld.get("placeholder", "") or "",
                "options": fld.get("options", []) or [],
                "multiple": bool(fld.get("multiple", False)),
                "min_value": fld.get("min_value", "") or "",
                "max_value": fld.get("max_value", "") or "",
                "pattern": fld.get("pattern", "") or "",
                "maxlength": fld.get("maxlength", "") or "",
            }
            if not entry["name"]:
                warnings.append(f"field #{i} has no name and will not submit")
            fields.append(entry)

        action = f.get("action", "") or ""
        method = (f.get("method", "GET") or "GET").upper()
        types = {fld["semantic_type"] for fld in fields}
        names_blob = " ".join(
            f"{fld['name']} {fld['label']}".lower() for fld in fields)

        is_login = ("password" in types and
                    any(k in names_blob for k in ("user", "login", "email")))
        is_search = any(fld["semantic_type"] == "search" for fld in fields)
        has_file = any(fld["html_type"] == "file" for fld in fields)

        if not action:
            warnings.append("form has no action; submits to the current page")
        if method not in ("GET", "POST"):
            warnings.append(f"unusual method {method}; treating as POST")

        return FormAnalysis(
            action=action,
            method=method,
            field_count=len(fields),
            required_count=sum(1 for fld in fields if fld["required"]),
            fields=fields,
            missing_names=[i for i, fld in enumerate(fields)
                           if not fld["name"]],
            warnings=warnings,
            is_login_form=is_login,
            is_search_form=is_search,
            has_file_upload=has_file,
        )

    def summarize_forms(self, forms: list[object]) -> list[dict]:
        """One-line summary per form for quick triage."""
        out = []
        for i, form in enumerate(forms):
            a = self.analyze_form(form)
            purpose = "login" if a.is_login_form else (
                "search" if a.is_search_form else (
                    "upload" if a.has_file_upload else "generic"))
            out.append({
                "index": i,
                "purpose": purpose,
                "method": a.method,
                "action": a.action,
                "fields": a.field_count,
                "required": a.required_count,
                "warnings": a.warnings,
            })
        return out

    # -- fill strategy -----------------------------------------------------
    def fill_strategy(self, form: object,
                      values: dict[str, str] | None = None) -> dict:
        """Build a fill plan: field -> value, with validation per field.

        `values` overrides auto-generated samples (keyed by field name).
        Returns {"plan": {name: value}, "skipped": [...], "errors": {...}}.
        """
        analysis = self.analyze_form(form)
        values = values or {}
        plan: dict[str, str] = {}
        skipped: list[str] = []
        errors: dict[str, list[str]] = {}

        for fld in analysis.fields:
            name = fld["name"]
            if not name:
                continue
            html_type = str(fld["html_type"]).lower()
            if html_type.startswith("button") or html_type in (
                    "submit", "reset", "image"):
                skipped.append(name or f"#{fld['index']}")
                continue
            if html_type == "hidden" and fld["value"]:
                plan[name] = fld["value"]  # keep server-provided tokens
                continue

            if name in values:
                chosen = values[name]
            elif fld["value"] and html_type not in ("checkbox", "radio"):
                chosen = fld["value"]
            elif fld["semantic_type"] == "select" and fld["options"]:
                chosen = self._pick_select_option(fld)
            elif fld["semantic_type"] in ("checkbox", "radio"):
                chosen = fld["value"] or "on"
            else:
                chosen = self.default_values.get(
                    fld["semantic_type"],
                    self.default_values.get("text", "sample"))

            result = validate_input(fld["semantic_type"], chosen, fld)
            if not result.ok:
                errors[name] = result.errors
            plan[name] = chosen

        return {"plan": plan, "skipped": skipped, "errors": errors,
                "action": analysis.action, "method": analysis.method}

    def _pick_select_option(self, fld: dict) -> str:
        options = fld.get("options", []) or []
        for opt in options:
            if str(opt.get("selected")) in ("1", "true", "True"):
                return str(opt.get("value", ""))
        # First non-empty option.
        for opt in options:
            if str(opt.get("value", "")).strip():
                return str(opt.get("value", ""))
        return options[0].get("value", "") if options else ""

    def validate_plan(self, form: object, plan: dict[str, str]) -> dict[str, list[str]]:
        """Validate every value in a fill plan; return {name: [errors]}."""
        analysis = self.analyze_form(form)
        by_name = {f["name"]: f for f in analysis.fields if f["name"]}
        problems: dict[str, list[str]] = {}
        for name, value in plan.items():
            fld = by_name.get(name, {})
            semantic = fld.get("semantic_type", "text") if fld else "text"
            r = validate_input(semantic, value, fld)
            if not r.ok:
                problems[name] = r.errors
        # Required fields missing from the plan entirely.
        for name, fld in by_name.items():
            if fld["required"] and name not in plan:
                problems.setdefault(name, []).append("required field not filled")
        return problems

    # -- curl replay -------------------------------------------------------
    def form_to_curl(self, form: object,
                     values: dict[str, str] | None = None,
                     base_url: str = "") -> str:
        """Convert a form + fill plan into an equivalent curl command.

        GET forms become --get --data-urlencode args; POST forms become
        --data-urlencode args (or -F for file uploads).
        """
        strategy = self.fill_strategy(form, values)
        plan = strategy["plan"]
        analysis = self.analyze_form(form)
        action = analysis.action or base_url
        if base_url and action and not urllib.parse.urlparse(action).netloc:
            action = urllib.parse.urljoin(base_url, action)

        parts = ["curl", "-sS", "-L"]
        method = analysis.method.upper()
        is_multipart = analysis.has_file_upload

        if method == "GET":
            parts.append("--get")
            for name, value in plan.items():
                parts += ["--data-urlencode", f"{name}={value}"]
            parts.append(shlex.quote(action))
        elif is_multipart:
            for name, value in plan.items():
                fld = next((f for f in analysis.fields if f["name"] == name), None)
                if fld and str(fld["html_type"]).lower() == "file":
                    parts += ["-F", f"{name}=@/path/to/file"]
                else:
                    parts += ["-F", f"{name}={value}"]
            parts += ["-X", "POST", shlex.quote(action)]
        else:
            for name, value in plan.items():
                parts += ["--data-urlencode", f"{name}={value}"]
            parts += ["-X", "POST", shlex.quote(action)]

        parts += ["-H", shlex.quote(
            "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")]
        return " ".join(parts)

    def form_to_requests_code(self, form: object,
                              values: dict[str, str] | None = None,
                              base_url: str = "") -> str:
        """Same as form_to_curl but emits Python `requests` code."""
        strategy = self.fill_strategy(form, values)
        plan = strategy["plan"]
        analysis = self.analyze_form(form)
        action = analysis.action or base_url
        if base_url and action and not urllib.parse.urlparse(action).netloc:
            action = urllib.parse.urljoin(base_url, action)
        method = analysis.method.upper()
        lines = ["import requests", "",
                 f"url = {action!r}",
                 f"data = {plan!r}", ""]
        if method == "GET":
            lines.append("r = requests.get(url, params=data, timeout=20)")
        else:
            lines.append("r = requests.post(url, data=data, timeout=20)")
        lines += ["r.raise_for_status()", "print(r.status_code)",
                  "print(r.text[:2000])"]
        return "\n".join(lines)

    # -- misc helpers ------------------------------------------------------
    def required_fields(self, form: object) -> list[dict]:
        """Only the required fields of a form."""
        return [f for f in self.analyze_form(form).fields if f["required"]]

    def detect_csrf_fields(self, form: object) -> list[dict]:
        """Fields that look like CSRF / anti-forgery tokens."""
        csrf_re = re.compile(r"(csrf|xsrf|authenticity|_token|nonce|state)",
                             re.I)
        out = []
        for fld in self.analyze_form(form).fields:
            if csrf_re.search(fld["name"]) and fld["value"]:
                out.append(fld)
        return out

    def field_names(self, form: object) -> list[str]:
        """All submittable field names in order."""
        return [f["name"] for f in self.analyze_form(form).fields if f["name"]]

    def to_json_schema(self, form: object) -> dict:
        """Describe a form as a JSON-schema-ish dict for the planner."""
        a = self.analyze_form(form)
        properties: dict[str, dict] = {}
        required: list[str] = []
        for fld in a.fields:
            if not fld["name"]:
                continue
            prop: dict[str, object] = {
                "type": "string",
                "description": fld["label"] or fld["name"],
                "semantic": fld["semantic_type"],
            }
            if fld["semantic_type"] == "number":
                prop["type"] = "number"
            if fld["semantic_type"] == "checkbox":
                prop["type"] = "boolean"
            if fld["options"]:
                prop["enum"] = [str(o.get("value", "")) for o in fld["options"]]
            properties[fld["name"]] = prop
            if fld["required"]:
                required.append(fld["name"])
        return {
            "type": "object",
            "action": a.action,
            "method": a.method,
            "properties": properties,
            "required": required,
        }

    # -- sanitization ------------------------------------------------------
    def sanitize_value(self, semantic_type: str, value: str) -> str:
        """Clean a raw value before validation/submission.

        Strips HTML tags from text-ish fields, normalizes phone digits,
        lowercases emails, trims whitespace everywhere.
        """
        v = str(value or "")
        if semantic_type in ("text", "textarea", "search", "full_name",
                             "first_name", "last_name", "username", "subject",
                             "city", "state", "country", "address"):
            v = re.sub(r"<[^>]+>", "", v)
        if semantic_type == "email":
            v = v.strip().lower()
        elif semantic_type == "tel":
            v = re.sub(r"[^\d+]", "", v)
            if v and not v.startswith("+"):
                v = "+" + v
        elif semantic_type in ("number",):
            v = v.strip().replace(",", "")
        elif semantic_type == "url":
            v = v.strip()
            if v and not re.match(r"^https?://", v, re.I):
                v = "https://" + v
        return v.strip()

    def build_get_url(self, form: object,
                      values: dict[str, str] | None = None,
                      base_url: str = "") -> str:
        """Render a GET form as a full URL with query string."""
        strategy = self.fill_strategy(form, values)
        analysis = self.analyze_form(form)
        action = analysis.action or base_url
        if base_url and action and not urllib.parse.urlparse(action).netloc:
            action = urllib.parse.urljoin(base_url, action)
        query = urllib.parse.urlencode(strategy["plan"])
        sep = "&" if urllib.parse.urlparse(action).query else "?"
        return f"{action}{sep}{query}" if query else action

    # -- honeypot / anti-bot ----------------------------------------------
    def detect_honeypots(self, form: object) -> list[dict]:
        """Fields that look like spam honeypots (tempting name, hidden).

        Heuristic: hidden input whose name suggests a real field
        (email2, website, url, phone2) OR a text field whose name
        contains 'honeypot'/'hp_' — bots fill these, humans don't.
        """
        honey_re = re.compile(
            r"(honeypot|^hp_|email2|email_confirm|website2|url2|phone2|"
            r"contact_me_by_fax|leaveblank|trap)", re.I)
        out = []
        for fld in self.analyze_form(form).fields:
            html_type = str(fld["html_type"]).lower()
            if honey_re.search(fld["name"]) and html_type in (
                    "hidden", "text"):
                out.append({**fld, "reason": "honeypot-like name"})
        return out

    # -- multi-step detection ----------------------------------------------
    def detect_steps(self, form_html: str) -> list[dict]:
        """Detect wizard-style multi-step forms in raw form HTML.

        Looks for <fieldset> groups, data-step attributes, and
        .step / .wizard-step classed containers.
        """
        steps: list[dict] = []
        for m in re.finditer(
                r"<fieldset[^>]*>(.*?)</fieldset>", form_html,
                re.DOTALL | re.IGNORECASE):
            legend = re.search(r"<legend[^>]*>(.*?)</legend>", m.group(1),
                               re.DOTALL | re.IGNORECASE)
            fields = re.findall(r'name="([^"]+)"', m.group(1))
            steps.append({
                "title": strip_legend(legend.group(1)) if legend else "",
                "fields": fields,
                "kind": "fieldset",
            })
        for m in re.finditer(
                r'<(?:div|section)[^>]*(?:data-step="([^"]*)"|'
                r'class="[^"]*(?:step|wizard)[^"]*")[^>]*>(.*?)</(?:div|section)>',
                form_html, re.DOTALL | re.IGNORECASE):
            step_no, body = m.group(1) or "", m.group(2) or ""
            fields = re.findall(r'name="([^"]+)"', body)
            steps.append({"title": step_no, "fields": fields, "kind": "step-div"})
        return steps

    # -- diffing -------------------------------------------------------------
    def diff_forms(self, old: object, new: object) -> dict:
        """Compare two snapshots of 'the same' form; report changes."""
        a_old = self.analyze_form(old)
        a_new = self.analyze_form(new)
        old_by = {f["name"]: f for f in a_old.fields if f["name"]}
        new_by = {f["name"]: f for f in a_new.fields if f["name"]}
        added = [n for n in new_by if n not in old_by]
        removed = [n for n in old_by if n not in new_by]
        changed = []
        for name in old_by:
            if name in new_by:
                o, n = old_by[name], new_by[name]
                diffs = [k for k in ("html_type", "semantic_type", "required",
                                     "placeholder")
                         if o.get(k) != n.get(k)]
                if o.get("options") != n.get("options"):
                    diffs.append("options")
                if diffs:
                    changed.append({"name": name, "changed": diffs})
        action_changed = a_old.action != a_new.action
        method_changed = a_old.method != a_new.method
        return {
            "added": added, "removed": removed, "changed": changed,
            "action_changed": action_changed, "method_changed": method_changed,
            "old_action": a_old.action, "new_action": a_new.action,
        }

    # -- profile-driven fill -------------------------------------------------
    def fill_from_profile(self, form: object,
                          profile: dict[str, str]) -> dict:
        """Fill a form from a named profile dict.

        Profile keys are semantic names ('email', 'first_name', ...).
        Fields are matched by semantic type first, then by name.
        """
        analysis = self.analyze_form(form)
        values: dict[str, str] = {}
        for fld in analysis.fields:
            name = fld["name"]
            if not name:
                continue
            semantic = fld["semantic_type"]
            if semantic in profile:
                values[name] = profile[semantic]
            elif name in profile:
                values[name] = profile[name]
        return self.fill_strategy(form, values)

    def empty_plan(self, form: object) -> dict[str, str]:
        """A plan with every field blank (useful for required-field probing)."""
        analysis = self.analyze_form(form)
        return {f["name"]: "" for f in analysis.fields if f["name"]}

    # -- encoding helpers ----------------------------------------------------
    def encode_body(self, form: object,
                    values: dict[str, str] | None = None) -> tuple[str, str]:
        """Return (content_type, body) for a POST of this form.

        Uses application/x-www-form-urlencoded. File uploads are not
        encodable here — use form_to_curl for multipart instead.
        """
        strategy = self.fill_strategy(form, values)
        body = urllib.parse.urlencode(strategy["plan"])
        return "application/x-www-form-urlencoded", body

    def hidden_fields(self, form: object) -> dict[str, str]:
        """Server-provided hidden fields (tokens, state) as name->value."""
        out = {}
        for fld in self.analyze_form(form).fields:
            if str(fld["html_type"]).lower() == "hidden" and fld["name"]:
                out[fld["name"]] = fld["value"]
        return out

    def completeness(self, form: object,
                     values: dict[str, str]) -> dict:
        """How complete is a candidate value set? Returns coverage stats."""
        analysis = self.analyze_form(form)
        names = {f["name"] for f in analysis.fields if f["name"]}
        required = {f["name"] for f in analysis.fields
                    if f["name"] and f["required"]}
        given = set(values)
        return {
            "total_fields": len(names),
            "filled": len(names & given),
            "required_total": len(required),
            "required_filled": len(required & given),
            "required_missing": sorted(required - given),
            "unknown_keys": sorted(given - names),
            "coverage": (len(names & given) / len(names)) if names else 1.0,
        }


def strip_legend(html_fragment: str) -> str:
    """Strip tags from a <legend> fragment."""
    return re.sub(r"<[^>]+>", " ", html_fragment or "").strip()


# ---------------------------------------------------------------------------
# Standalone validators (usable without a FormHandler instance)
# ---------------------------------------------------------------------------

def is_valid_email(value: str) -> bool:
    """Quick email check."""
    return validate_input("email", value).ok


def is_valid_phone(value: str) -> bool:
    """Quick phone check."""
    return validate_input("tel", value).ok


def is_valid_url(value: str) -> bool:
    """Quick URL check."""
    return validate_input("url", value).ok


def normalize_phone(value: str, default_country: str = "+1") -> str:
    """Normalize a phone number to E.164-ish form.

    Keeps an existing leading '+'; otherwise prepends default_country.
    """
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return ""
    if str(value).strip().startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    return default_country + digits


def mask_value(semantic_type: str, value: str) -> str:
    """Mask a sensitive value for logging (passwords, cards, CVV)."""
    v = str(value or "")
    if semantic_type in ("password",):
        return "*" * min(len(v), 12) or "(empty)"
    if semantic_type == "card_number" and len(v) >= 4:
        return "*" * (len(v) - 4) + v[-4:]
    if semantic_type == "cvv":
        return "***"
    if semantic_type == "email" and "@" in v:
        user, domain = v.split("@", 1)
        return user[:2] + "***@" + domain
    return v


def suggest_autocomplete(field: dict) -> str:
    """Return the HTML autocomplete attribute value for a semantic field."""
    mapping = {
        "email": "email", "tel": "tel", "full_name": "name",
        "first_name": "given-name", "last_name": "family-name",
        "username": "username", "password": "current-password",
        "address": "street-address", "city": "address-level2",
        "state": "address-level1", "postal": "postal-code",
        "country": "country-name", "url": "url", "date": "bday",
        "card_number": "cc-number", "cvv": "cc-csc",
        "card_expiry": "cc-exp",
    }
    semantic = infer_field_type(field)
    return mapping.get(semantic, "off" if semantic == "search" else "")


def coerce_checkbox(value: str | bool | None) -> str:
    """Coerce truthy/falsy input to 'on'/'' for checkbox submission."""
    if isinstance(value, bool):
        return "on" if value else ""
    v = str(value or "").strip().lower()
    return "on" if v in ("1", "true", "yes", "on", "checked") else ""


def split_full_name(full: str) -> dict[str, str]:
    """Split 'First Middle Last' into first/last components."""
    parts = (full or "").strip().split()
    if not parts:
        return {"first_name": "", "last_name": ""}
    if len(parts) == 1:
        return {"first_name": parts[0], "last_name": ""}
    return {"first_name": parts[0], "last_name": parts[-1]}


def parse_date_loose(value: str) -> str | None:
    """Parse many date formats into ISO YYYY-MM-DD; None if unparseable."""
    v = (value or "").strip()
    formats = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d",
               "%d-%m-%Y", "%m-%d-%Y", "%Y.%m.%d", "%d %b %Y",
               "%d %B %Y", "%b %d, %Y", "%B %d, %Y")
    for fmt in formats:
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Bare year or year-month.
    if re.fullmatch(r"\d{4}", v):
        return f"{v}-01-01"
    m = re.fullmatch(r"(\d{4})-(\d{2})", v)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    return None


def guess_country_from_phone(value: str) -> str:
    """Guess ISO country from a phone prefix (small built-in table)."""
    digits = re.sub(r"\D", "", value or "")
    prefixes = {
        "1": "US", "44": "GB", "49": "DE", "33": "FR", "39": "IT",
        "34": "ES", "31": "NL", "41": "CH", "43": "AT", "32": "BE",
        "91": "IN", "92": "PK", "880": "BD", "81": "JP", "86": "CN",
        "82": "KR", "61": "AU", "64": "NZ", "27": "ZA", "234": "NG",
        "20": "EG", "971": "AE", "966": "SA", "90": "TR", "7": "RU",
        "55": "BR", "52": "MX", "54": "AR", "56": "CL", "57": "CO",
    }
    for length in (3, 2, 1):
        if digits[:length] in prefixes:
            return prefixes[digits[:length]]
    return ""


# ---------------------------------------------------------------------------
# Submission via urllib (GET/POST replay with cookie jar)
# ---------------------------------------------------------------------------

def submit_form(form: object,
                values: dict[str, str] | None = None,
                base_url: str = "",
                timeout: int = 20,
                opener=None) -> dict:
    """Submit a form with urllib and return {status, url, text, error}.

    Uses an http.cookiejar-backed opener when none is supplied so a
    FormSession can share cookies across requests.
    """
    import http.cookiejar
    import urllib.request

    handler = FormHandler()
    strategy = handler.fill_strategy(form, values)
    analysis = handler.analyze_form(form)
    action = analysis.action or base_url
    if base_url and action and not urllib.parse.urlparse(action).netloc:
        action = urllib.parse.urljoin(base_url, action)
    if not action:
        return {"status": 0, "url": "", "text": "",
                "error": "form has no action and no base_url"}

    if opener is None:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))

    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
    try:
        if analysis.method.upper() == "GET":
            url = handler.build_get_url(form, values, base_url)
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                return {"status": resp.status, "url": resp.url,
                        "text": body[:20000], "error": ""}
        content_type, body = handler.encode_body(form, values)
        req = urllib.request.Request(
            action, data=body.encode(),
            headers={**headers, "Content-Type": content_type},
            method="POST")
        with opener.open(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
            return {"status": resp.status, "url": resp.url,
                    "text": text[:20000], "error": ""}
    except Exception as e:  # noqa: BLE001 - report, don't raise
        return {"status": 0, "url": action, "text": "",
                "error": f"submit failed: {e}"}


class FormSession:
    """Fetch pages and submit forms while sharing one cookie jar.

    Intended for multi-step flows: fetch page -> extract forms ->
    fill -> submit -> repeat on the response page.
    """

    def __init__(self, timeout: int = 20):
        import http.cookiejar
        import urllib.request
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.handler = FormHandler()
        self.history: list[dict] = []

    def fetch(self, url: str) -> dict:
        """GET a page; returns {status, url, text, error}."""
        import urllib.request
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
                result = {"status": resp.status, "url": resp.url,
                          "text": text, "error": ""}
        except Exception as e:  # noqa: BLE001
            result = {"status": 0, "url": url, "text": "",
                      "error": str(e)[:300]}
        self.history.append({"action": "fetch", **result})
        return result

    def submit(self, form: object,
               values: dict[str, str] | None = None,
               base_url: str = "") -> dict:
        """Submit a form through this session's cookie jar."""
        result = submit_form(form, values, base_url=base_url,
                             timeout=self.timeout, opener=self.opener)
        self.history.append({"action": "submit", **result})
        return result

    def cookies(self) -> list[dict]:
        """Current cookies as plain dicts."""
        return [{"name": c.name, "value": c.value, "domain": c.domain,
                 "path": c.path} for c in self.jar]

    def clear(self) -> None:
        """Drop all cookies and history."""
        self.jar.clear()
        self.history.clear()


def consolidate_radio_groups(fields: list[dict]) -> list[dict]:
    """Merge same-name radio inputs into one field with options."""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for fld in fields:
        d = _as_dict(fld)
        if str(d.get("field_type") or d.get("type") or "").lower() != "radio":
            order.append(f"field:{id(d)}")
            groups[f"field:{id(d)}"] = {**d, "_single": True}
            continue
        name = d.get("name", "")
        if name not in groups:
            groups[name] = {**d, "options": [], "_single": False}
            order.append(name)
        opt = {"value": d.get("value", ""), "text": d.get("label", ""),
               "selected": "1" if d.get("value") else "0"}
        groups[name]["options"].append(opt)
    out = []
    for key in order:
        g = dict(groups[key])
        g.pop("_single", None)
        out.append(g)
    return out

