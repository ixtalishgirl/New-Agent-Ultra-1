"""Unit tests for browser extraction, form handling, and navigation.

All fixtures are inline HTML strings — no network access.
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from agent.browser.advanced_extractor import (
    AdvancedExtractor, resolve_url, clean_text, strip_tags,
)
from agent.browser.form_handler import (
    FormHandler, infer_field_type, validate_input, is_valid_email,
    is_valid_phone, normalize_phone, mask_value, suggest_autocomplete,
    split_full_name, parse_date_loose, guess_country_from_phone,
    consolidate_radio_groups, strip_legend,
)
from agent.browser.navigation import (
    NavigationPlanner, CrawlFrontier, CrawlConfig,
    url_canonicalize, url_fingerprint, same_site, parent_url,
    breadcrumb_extract, detect_pagination, generate_page_urls,
    robots_allows, crawl_delay_for, extract_page_links, filter_links,
    group_links_by_section, learn_url_templates, build_site_tree,
    path_depth, sort_by_depth, make_crawl_plan, estimate_crawl_time,
)


SAMPLE_HTML = """
<!DOCTYPE html><html lang="en"><head>
<title>Test Page</title>
<meta name="description" content="A test page for extraction.">
<meta name="keywords" content="test, extraction">
<meta property="og:title" content="OG Test Page">
<meta property="og:image" content="/img/og.png">
<meta name="twitter:card" content="summary">
<link rel="canonical" href="https://example.com/page">
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Article",
 "headline": "Test Article", "author": "Agent"}
</script>
</head><body>
<nav><a href="/nav-link">Nav</a></nav>
<h1>Main Heading</h1>
<h2>Sub Heading</h2>
<p>First paragraph with <a href="https://example.com/internal">internal link</a>.</p>
<p>Second paragraph.</p>
<ul><li>item one</li><li>item two</li></ul>
<ol><li>step one</li><li>step two</li></ol>
<table id="prices">
<caption>Price list</caption>
<tr><th>Product</th><th>Price</th></tr>
<tr><td>Widget</td><td>$5</td></tr>
<tr><td>Gadget</td><td>$10</td></tr>
</table>
<table>
<tr><td colspan="2">merged header</td></tr>
<tr><td>a</td><td>b</td></tr>
</table>
<form action="/submit" method="post" id="contact">
<label for="email">Email address</label>
<input type="email" id="email" name="email" required placeholder="you@x.com">
<label>Full name <input type="text" name="full_name"></label>
<input type="hidden" name="csrf_token" value="abc123">
<select name="country"><option value="">--</option>
<option value="US" selected>United States</option></select>
<textarea name="message" maxlength="500"></textarea>
<input type="submit" value="Send">
</form>
<a href="https://external.com/page">External</a>
<a href="/files/report.pdf">Download PDF</a>
<a href="mailto:hi@example.com">Email us</a>
<a href="#section">Jump</a>
<footer>Footer boilerplate text here.</footer>
</body></html>
"""


class TestResolveHelpers(unittest.TestCase):
    def test_resolve_url_relative(self):
        self.assertEqual(resolve_url("/about", "https://example.com/x"),
                         "https://example.com/about")

    def test_resolve_url_absolute(self):
        self.assertEqual(resolve_url("https://a.com/b", "https://x.com"),
                         "https://a.com/b")

    def test_resolve_url_empty(self):
        self.assertEqual(resolve_url("", "https://x.com"), "")

    def test_clean_text(self):
        self.assertEqual(clean_text("  a &amp;  b\n c "), "a & b c")

    def test_strip_tags(self):
        self.assertEqual(strip_tags("<p>hi <b>there</b></p>"), "hi there")


class TestTables(unittest.TestCase):
    def setUp(self):
        self.ex = AdvancedExtractor()

    def test_simple_table(self):
        tables = self.ex.extract_tables(SAMPLE_HTML)
        self.assertEqual(len(tables), 2)
        t = tables[0]
        self.assertEqual(t.headers, ["Product", "Price"])
        self.assertEqual(len(t.rows), 2)
        self.assertEqual(t.rows[0], {"Product": "Widget", "Price": "$5"})
        self.assertEqual(t.caption, "Price list")
        self.assertEqual(t.table_id, "prices")

    def test_colspan_table(self):
        tables = self.ex.extract_tables(SAMPLE_HTML)
        t = tables[1]
        # A single spanning cell in row 0 is a title row, not a header.
        self.assertEqual(t.caption, "merged header")
        self.assertEqual(t.headers, ["col_1", "col_2"])
        self.assertEqual(t.raw_rows[0], ["merged header", "merged header"])
        self.assertEqual(t.raw_rows[1], ["a", "b"])

    def test_rowspan_table(self):
        html = ('<table><tr><td rowspan="2">R</td><td>a</td></tr>'
                '<tr><td>b</td></tr></table>')
        t = self.ex.extract_tables(html)[0]
        self.assertEqual(t.headers, ["R", "a"])
        self.assertEqual(t.raw_rows, [["R", "b"]])
        self.assertEqual(t.rows, [{"R": "R", "a": "b"}])

    def test_tables_as_dicts_json_serializable(self):
        import json
        dicts = self.ex.tables_as_dicts(SAMPLE_HTML)
        json.dumps(dicts)  # must not raise
        self.assertEqual(dicts[0]["rows"][1]["Product"], "Gadget")

    def test_no_tables(self):
        self.assertEqual(self.ex.extract_tables("<p>no tables</p>"), [])


class TestForms(unittest.TestCase):
    def setUp(self):
        self.ex = AdvancedExtractor()

    def test_form_parsed(self):
        forms = self.ex.extract_forms(SAMPLE_HTML,
                                      base_url="https://example.com")
        self.assertEqual(len(forms), 1)
        f = forms[0]
        self.assertEqual(f.action, "https://example.com/submit")
        self.assertEqual(f.method, "POST")
        names = [fld.name for fld in f.fields]
        self.assertIn("email", names)
        self.assertIn("full_name", names)
        self.assertIn("csrf_token", names)
        self.assertIn("country", names)
        self.assertIn("message", names)

    def test_field_details(self):
        f = self.ex.extract_forms(SAMPLE_HTML)[0]
        email = next(fld for fld in f.fields if fld.name == "email")
        self.assertTrue(email.required)
        self.assertEqual(email.field_type, "email")
        self.assertEqual(email.label, "Email address")
        country = next(fld for fld in f.fields if fld.name == "country")
        self.assertEqual(country.field_type, "select")
        self.assertEqual(len(country.options), 2)
        self.assertEqual(country.options[1]["value"], "US")

    def test_implicit_label(self):
        f = self.ex.extract_forms(SAMPLE_HTML)[0]
        name = next(fld for fld in f.fields if fld.name == "full_name")
        self.assertEqual(name.label, "Full name")

    def test_no_forms(self):
        self.assertEqual(self.ex.extract_forms("<p>x</p>"), [])


class TestMetadata(unittest.TestCase):
    def setUp(self):
        self.ex = AdvancedExtractor()

    def test_metadata(self):
        m = self.ex.extract_metadata(SAMPLE_HTML,
                                     base_url="https://example.com")
        self.assertEqual(m.title, "Test Page")
        self.assertEqual(m.description, "A test page for extraction.")
        self.assertEqual(m.keywords, "test, extraction")
        self.assertEqual(m.og["title"], "OG Test Page")
        self.assertEqual(m.og["image"], "/img/og.png")
        self.assertEqual(m.twitter["card"], "summary")
        self.assertEqual(m.canonical, "https://example.com/page")
        self.assertEqual(m.language, "en")

    def test_metadata_empty(self):
        m = self.ex.extract_metadata("<html></html>")
        self.assertEqual(m.title, "")


class TestLinks(unittest.TestCase):
    def setUp(self):
        self.ex = AdvancedExtractor()
        self.cats = self.ex.extract_links_categorized(
            SAMPLE_HTML, base_url="https://example.com/page")

    def test_internal(self):
        urls = [l.url for l in self.cats["internal"]]
        self.assertIn("https://example.com/internal", urls)
        self.assertIn("https://example.com/nav-link", urls)

    def test_external(self):
        urls = [l.url for l in self.cats["external"]]
        self.assertIn("https://external.com/page", urls)

    def test_downloads(self):
        urls = [l.url for l in self.cats["downloads"]]
        self.assertIn("https://example.com/files/report.pdf", urls)

    def test_emails(self):
        self.assertEqual(self.cats["emails"][0].url,
                         "mailto:hi@example.com")

    def test_anchors(self):
        self.assertEqual(len(self.cats["anchors"]), 1)

    def test_link_text_captured(self):
        ext = next(l for l in self.cats["external"])
        self.assertEqual(ext.text, "External")


class TestStructuredText(unittest.TestCase):
    def setUp(self):
        self.ex = AdvancedExtractor()

    def test_headings(self):
        st = self.ex.extract_text_structured(SAMPLE_HTML)
        self.assertEqual([(h.level, h.text) for h in st.headings],
                         [(1, "Main Heading"), (2, "Sub Heading")])

    def test_paragraphs(self):
        st = self.ex.extract_text_structured(SAMPLE_HTML)
        self.assertEqual(len(st.paragraphs), 2)
        self.assertTrue(st.paragraphs[0].startswith("First paragraph"))

    def test_lists(self):
        st = self.ex.extract_text_structured(SAMPLE_HTML)
        self.assertEqual(len(st.lists), 2)
        self.assertFalse(st.lists[0].ordered)
        self.assertEqual(st.lists[0].items, ["item one", "item two"])
        self.assertTrue(st.lists[1].ordered)

    def test_nav_footer_excluded(self):
        st = self.ex.extract_text_structured(SAMPLE_HTML)
        blob = " ".join(st.paragraphs)
        self.assertNotIn("Nav", blob)
        self.assertNotIn("Footer boilerplate", blob)

    def test_outline(self):
        outline = self.ex.extract_headings_outline(SAMPLE_HTML)
        self.assertEqual(outline[0]["indent"], 0)
        self.assertEqual(outline[1]["indent"], 1)


class TestJsonLd(unittest.TestCase):
    def setUp(self):
        self.ex = AdvancedExtractor()

    def test_json_ld_parsed(self):
        blocks = self.ex.extract_json_ld(SAMPLE_HTML)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["@type"], "Article")

    def test_json_ld_types(self):
        grouped = self.ex.extract_json_ld_types(SAMPLE_HTML)
        self.assertIn("Article", grouped)

    def test_bad_json_ld_skipped(self):
        html = ('<script type="application/ld+json">{not json</script>')
        self.assertEqual(self.ex.extract_json_ld(html), [])


class TestReadability(unittest.TestCase):
    def setUp(self):
        self.ex = AdvancedExtractor()

    def test_main_text_excludes_boilerplate(self):
        text = self.ex.readability_text(SAMPLE_HTML)
        self.assertIn("First paragraph", text)
        self.assertNotIn("Footer boilerplate", text)

    def test_paragraphs(self):
        paras = self.ex.readability_paragraphs(SAMPLE_HTML)
        self.assertTrue(any("First paragraph" in p for p in paras))

    def test_fallback_on_empty(self):
        text = self.ex.readability_text("<html><body><p>hi</p></body></html>")
        self.assertIn("hi", text)

    def test_extract_all_keys(self):
        all_data = self.ex.extract_all(SAMPLE_HTML,
                                       base_url="https://example.com")
        for key in ("metadata", "tables", "forms", "links", "headings",
                    "paragraphs", "lists", "json_ld_types", "main_text"):
            self.assertIn(key, all_data)
        import json
        json.dumps(all_data)  # serializable


class TestInferFieldType(unittest.TestCase):
    def test_email_by_name(self):
        self.assertEqual(infer_field_type({"name": "user_email",
                                           "field_type": "text"}), "email")

    def test_phone_by_label(self):
        self.assertEqual(infer_field_type({"name": "p1", "label": "Phone",
                                           "field_type": "text"}), "tel")

    def test_html_type_wins(self):
        self.assertEqual(infer_field_type({"name": "x",
                                           "field_type": "password"}),
                         "password")

    def test_first_name(self):
        self.assertEqual(infer_field_type({"name": "firstName",
                                           "field_type": "text"}),
                         "first_name")


class TestValidateInput(unittest.TestCase):
    def test_valid_email(self):
        self.assertTrue(validate_input("email", "a@b.com").ok)

    def test_invalid_email(self):
        r = validate_input("email", "not-an-email")
        self.assertFalse(r.ok)
        self.assertTrue(r.errors)

    def test_required_empty(self):
        r = validate_input("text", "", {"required": True, "name": "n"})
        self.assertFalse(r.ok)

    def test_optional_empty_ok(self):
        self.assertTrue(validate_input("email", "").ok)

    def test_phone(self):
        self.assertTrue(validate_input("tel", "+15551234567").ok)
        self.assertFalse(validate_input("tel", "abc").ok)

    def test_number_range(self):
        fld = {"min_value": "1", "max_value": "10", "name": "qty"}
        self.assertTrue(validate_input("number", "5", fld).ok)
        self.assertFalse(validate_input("number", "99", fld).ok)
        self.assertFalse(validate_input("number", "abc", fld).ok)

    def test_date(self):
        self.assertTrue(validate_input("date", "1990-01-15").ok)
        self.assertFalse(validate_input("date", "yesterday").ok)

    def test_maxlength(self):
        fld = {"maxlength": "3", "name": "code"}
        self.assertFalse(validate_input("text", "toolong", fld).ok)

    def test_card_luhn(self):
        self.assertTrue(validate_input("card_number",
                                       "4111111111111111").ok)
        self.assertFalse(validate_input("card_number",
                                        "4111111111111112").ok)

    def test_select_options(self):
        fld = {"name": "c",
               "options": [{"value": "US"}, {"value": "PK"}]}
        self.assertTrue(validate_input("select", "US", fld).ok)
        self.assertFalse(validate_input("select", "XX", fld).ok)


class TestFormHandler(unittest.TestCase):
    def _dict_form(self):
        return {
            "action": "/submit", "method": "post",
            "fields": [
                {"name": "email", "field_type": "email",
                 "label": "Email", "required": True, "value": ""},
                {"name": "age", "field_type": "number",
                 "label": "Age", "required": False, "value": "",
                 "min_value": "1", "max_value": "120"},
                {"name": "csrf", "field_type": "hidden",
                 "value": "tok123"},
            ],
        }

    def setUp(self):
        self.h = FormHandler()

    def test_analyze(self):
        a = self.h.analyze_form(self._dict_form())
        self.assertEqual(a.method, "POST")
        self.assertEqual(a.field_count, 3)
        self.assertEqual(a.required_count, 1)
        self.assertEqual(a.fields[0]["semantic_type"], "email")

    def test_login_detection(self):
        form = {"action": "/login", "method": "post", "fields": [
            {"name": "username", "field_type": "text"},
            {"name": "password", "field_type": "password"}]}
        a = self.h.analyze_form(form)
        self.assertTrue(a.is_login_form)

    def test_fill_strategy(self):
        s = self.h.fill_strategy(self._dict_form())
        self.assertIn("email", s["plan"])
        self.assertEqual(s["plan"]["csrf"], "tok123")  # hidden kept
        self.assertEqual(s["method"], "POST")

    def test_fill_strategy_override(self):
        s = self.h.fill_strategy(self._dict_form(),
                                 {"email": "me@example.com"})
        self.assertEqual(s["plan"]["email"], "me@example.com")
        self.assertEqual(s["errors"], {})

    def test_fill_strategy_bad_value_flagged(self):
        s = self.h.fill_strategy(self._dict_form(), {"email": "bad"})
        self.assertIn("email", s["errors"])

    def test_validate_plan(self):
        problems = self.h.validate_plan(self._dict_form(),
                                        {"email": "a@b.com", "age": "200"})
        self.assertIn("age", problems)
        self.assertNotIn("email", problems)

    def test_form_to_curl_post(self):
        curl = self.h.form_to_curl(self._dict_form(),
                                   {"email": "a@b.com"},
                                   base_url="https://example.com")
        self.assertTrue(curl.startswith("curl"))
        self.assertIn("--data-urlencode", curl)
        self.assertIn("https://example.com/submit", curl)
        self.assertIn("-X POST", curl)

    def test_form_to_curl_get(self):
        form = {"action": "/search", "method": "get", "fields": [
            {"name": "q", "field_type": "search"}]}
        curl = self.h.form_to_curl(form, {"q": "hi"})
        self.assertIn("--get", curl)

    def test_build_get_url(self):
        form = {"action": "/s", "method": "get",
                "fields": [{"name": "q", "field_type": "text"}]}
        url = self.h.build_get_url(form, {"q": "hello world"},
                                   base_url="https://e.com")
        self.assertIn("q=hello+world", url)

    def test_honeypot(self):
        form = {"action": "/", "method": "post", "fields": [
            {"name": "email2", "field_type": "hidden", "value": ""},
            {"name": "email", "field_type": "email"}]}
        pots = self.h.detect_honeypots(form)
        self.assertEqual(len(pots), 1)
        self.assertEqual(pots[0]["name"], "email2")

    def test_csrf(self):
        csrf = self.h.detect_csrf_fields(self._dict_form())
        self.assertEqual(csrf[0]["name"], "csrf")

    def test_sanitize(self):
        self.assertEqual(self.h.sanitize_value("email", "  A@B.COM "),
                         "a@b.com")
        self.assertEqual(self.h.sanitize_value("tel", "(555) 123-4567"),
                         "+5551234567")
        self.assertEqual(self.h.sanitize_value("text", "<b>hi</b>"), "hi")

    def test_completeness(self):
        c = self.h.completeness(self._dict_form(), {"email": "a@b.com"})
        self.assertEqual(c["required_filled"], 1)
        self.assertEqual(c["required_missing"], [])

    def test_to_json_schema(self):
        schema = self.h.to_json_schema(self._dict_form())
        self.assertIn("email", schema["required"])
        self.assertEqual(schema["properties"]["email"]["semantic"], "email")

    def test_diff(self):
        old = self._dict_form()
        new = dict(self._dict_form())
        new["fields"] = list(old["fields"]) + [
            {"name": "extra", "field_type": "text"}]
        d = self.h.diff_forms(old, new)
        self.assertEqual(d["added"], ["extra"])

    def test_fill_from_profile(self):
        s = self.h.fill_from_profile(
            self._dict_form(), {"email": "p@example.com"})
        self.assertEqual(s["plan"]["email"], "p@example.com")

    def test_hidden_fields(self):
        self.assertEqual(self.h.hidden_fields(self._dict_form()),
                         {"csrf": "tok123"})


class TestStandaloneHelpers(unittest.TestCase):
    def test_quick_validators(self):
        self.assertTrue(is_valid_email("x@y.com"))
        self.assertFalse(is_valid_email("x"))
        self.assertTrue(is_valid_phone("+15551234567"))
        self.assertFalse(is_valid_phone("zzz"))

    def test_normalize_phone(self):
        self.assertEqual(normalize_phone("(555) 123-4567", "+1"),
                         "+15551234567")
        self.assertEqual(normalize_phone("+92 300 1234567"), "+923001234567")

    def test_mask_value(self):
        self.assertEqual(mask_value("password", "secret"), "******")
        self.assertEqual(mask_value("card_number", "4111111111111111"),
                         "************1111")
        self.assertEqual(mask_value("email", "alex@example.com"),
                         "al***@example.com")

    def test_autocomplete(self):
        self.assertEqual(suggest_autocomplete({"name": "email"}), "email")
        self.assertEqual(
            suggest_autocomplete({"name": "first_name"}), "given-name")

    def test_split_full_name(self):
        self.assertEqual(split_full_name("Alex Rivera"),
                         {"first_name": "Alex", "last_name": "Rivera"})
        self.assertEqual(split_full_name("Madonna")["last_name"], "")

    def test_parse_date_loose(self):
        self.assertEqual(parse_date_loose("01/15/1990"), "1990-01-15")
        self.assertEqual(parse_date_loose("1990"), "1990-01-01")
        self.assertIsNone(parse_date_loose("not a date"))

    def test_guess_country(self):
        self.assertEqual(guess_country_from_phone("+92 300 1"), "PK")
        self.assertEqual(guess_country_from_phone("+1 555"), "US")
        self.assertEqual(guess_country_from_phone("+999"), "")

    def test_consolidate_radio(self):
        fields = [
            {"name": "color", "field_type": "radio", "value": "red",
             "label": "Red"},
            {"name": "color", "field_type": "radio", "value": "blue",
             "label": "Blue"},
            {"name": "email", "field_type": "email"},
        ]
        out = consolidate_radio_groups(fields)
        self.assertEqual(len(out), 2)
        radio = next(f for f in out if f["name"] == "color")
        self.assertEqual(len(radio["options"]), 2)

    def test_strip_legend(self):
        self.assertEqual(strip_legend("<b>Step 1</b>"), "Step 1")


class TestCanonicalize(unittest.TestCase):
    def test_lowercase_and_port(self):
        self.assertEqual(url_canonicalize("HTTP://Example.COM:80/a"),
                         "http://example.com/a")

    def test_tracking_stripped(self):
        u = url_canonicalize(
            "https://e.com/p?utm_source=x&b=2&a=1")
        self.assertEqual(u, "https://e.com/p?a=1&b=2")

    def test_fragment_dropped(self):
        self.assertEqual(url_canonicalize("https://e.com/p#frag"),
                         "https://e.com/p")

    def test_dot_segments(self):
        self.assertEqual(url_canonicalize("https://e.com/a/../b"),
                         "https://e.com/b")

    def test_fingerprint(self):
        self.assertEqual(url_fingerprint("https://e.com/p?x=1#y"),
                         "https://e.com/p")

    def test_same_site(self):
        self.assertTrue(same_site("https://e.com/a", "https://e.com/b"))
        self.assertFalse(same_site("https://e.com/a", "https://o.com/b"))

    def test_parent_url(self):
        self.assertEqual(parent_url("https://e.com/a/b/c"),
                         "https://e.com/a/b")


class TestBreadcrumbs(unittest.TestCase):
    def test_nav_breadcrumb(self):
        html = ('<nav aria-label="breadcrumb"><ol>'
                '<li><a href="/">Home</a></li>'
                '<li><a href="/cat">Category</a></li></ol></nav>')
        crumbs = breadcrumb_extract(html, "https://e.com")
        self.assertEqual([c["text"] for c in crumbs],
                         ["Home", "Category"])
        self.assertEqual(crumbs[1]["url"], "https://e.com/cat")

    def test_jsonld_breadcrumb(self):
        html = ('<script type="application/ld+json">'
                '{"@type":"BreadcrumbList","itemListElement":['
                '{"@type":"ListItem","position":1,'
                ' "item":{"@id":"https://e.com/","name":"Home"}}]}</script>')
        crumbs = breadcrumb_extract(html)
        self.assertEqual(crumbs[0]["text"], "Home")


class TestPagination(unittest.TestCase):
    def test_query_style_from_url(self):
        r = detect_pagination("", "https://e.com/list?page=3")
        self.assertEqual(r["style"], "query")
        self.assertEqual(r["current_page"], 3)

    def test_rel_next(self):
        html = '<a rel="next" href="/list?page=2">more</a>'
        r = detect_pagination(html, "https://e.com/list")
        self.assertEqual(r["style"], "link")
        self.assertEqual(r["next_url"], "https://e.com/list?page=2")

    def test_next_text(self):
        html = '<a href="/p2">Next &raquo;</a>'
        r = detect_pagination(html, "https://e.com/p1")
        self.assertEqual(r["next_url"], "https://e.com/p2")

    def test_generate_query_urls(self):
        urls = generate_page_urls("https://e.com/list", "query",
                                  start=1, end=3)
        self.assertEqual(urls[0], "https://e.com/list?page=1")
        self.assertEqual(len(urls), 3)

    def test_generate_path_urls(self):
        urls = generate_page_urls("https://e.com/blog", "path",
                                  start=1, end=2)
        self.assertEqual(urls[0], "https://e.com/blog/page/1/")

    def test_generate_offset_urls(self):
        urls = generate_page_urls("https://e.com/list", "offset",
                                  start=1, end=2, page_size=20)
        self.assertIn("offset=20", urls[1])


class TestRobots(unittest.TestCase):
    ROBOTS = "User-agent: *\nDisallow: /private/\nCrawl-delay: 5\n"

    def test_allows(self):
        self.assertTrue(robots_allows("*", "https://e.com/pub",
                                      self.ROBOTS))

    def test_disallows(self):
        self.assertFalse(robots_allows("*", "https://e.com/private/x",
                                       self.ROBOTS))

    def test_crawl_delay(self):
        self.assertEqual(crawl_delay_for("*", self.ROBOTS), 5.0)


class TestFrontier(unittest.TestCase):
    def _frontier(self, **kw):
        cfg = CrawlConfig(politeness_delay=0, respect_robots=False,
                          max_depth=2, max_pages=10, **kw)
        return CrawlFrontier(["https://e.com/"], cfg)

    def test_push_pop(self):
        f = self._frontier()
        seed = f.pop()  # the seed URL is queued first (FIFO)
        self.assertEqual(seed.url, "https://e.com/")
        self.assertTrue(f.push("https://e.com/a", depth=1))
        item = f.pop()
        self.assertEqual(item.url, "https://e.com/a")
        self.assertEqual(item.depth, 1)

    def test_dedup(self):
        f = self._frontier()
        self.assertTrue(f.push("https://e.com/a?utm_source=x", depth=1))
        # Same URL after canonicalization -> rejected
        self.assertFalse(f.push("https://e.com/a", depth=1))

    def test_depth_limit(self):
        f = self._frontier()
        self.assertFalse(f.push("https://e.com/deep", depth=5))

    def test_same_host_only(self):
        f = self._frontier()
        self.assertFalse(f.push("https://other.com/", depth=1))

    def test_exclude_patterns(self):
        f = self._frontier(exclude_patterns=[r"\.pdf$"])
        self.assertFalse(f.push("https://e.com/f.pdf", depth=1))
        self.assertTrue(f.push("https://e.com/page", depth=1))

    def test_push_many(self):
        f = self._frontier()
        n = f.push_many(["https://e.com/a", "https://e.com/b",
                         "https://e.com/a"], depth=1)
        self.assertEqual(n, 2)

    def test_done_and_stats(self):
        f = self._frontier()
        while not f.done:
            item = f.pop()
            if item is None:
                break
        self.assertTrue(f.done)
        stats = f.stats()
        self.assertEqual(stats["visited"], 1)


class TestPlanner(unittest.TestCase):
    def setUp(self):
        self.planner = NavigationPlanner()

    def test_direct_url(self):
        steps = self.planner.plan_navigation("https://e.com/",
                                             "https://e.com/target")
        self.assertEqual(steps[0].action, "goto")
        self.assertEqual(steps[0].target, "https://e.com/target")

    def test_relative_target(self):
        steps = self.planner.plan_navigation("https://e.com/a", "/b")
        self.assertEqual(steps[0].target, "https://e.com/b")

    def test_sitemap_route(self):
        steps = self.planner.plan_navigation(
            "https://e.com/", "pricing",
            sitemap_urls=["https://e.com/", "https://e.com/pricing"])
        self.assertEqual(steps[0].target, "https://e.com/pricing")

    def test_search_fallback(self):
        steps = self.planner.plan_navigation("https://e.com/", "widget xyz")
        self.assertEqual(steps[0].action, "search")
        self.assertIn("site:e.com", steps[0].target)

    def test_link_graph_route(self):
        graph = {"https://e.com/": ["https://e.com/a"],
                 "https://e.com/a": ["https://e.com/a/b"]}
        steps = self.planner.plan_navigation("https://e.com/", "a/b",
                                             known_links=graph)
        self.assertTrue(any(s.target == "https://e.com/a/b"
                            for s in steps))

    def test_plan_to_element(self):
        steps = self.planner.plan_to_element("https://e.com/", "Buy now")
        self.assertEqual([s.action for s in steps],
                         ["goto", "scroll", "click"])

    def test_paginate_plan(self):
        steps = self.planner.paginate_plan("https://e.com/list", 3)
        self.assertEqual(len(steps), 3)


class TestLinkHelpers(unittest.TestCase):
    HTML = ('<nav><a href="/n1">N1</a></nav>'
            '<main><a href="/m1">M1</a><a href="https://x.com/e">E</a></main>'
            '<footer><a href="/f1">F1</a></footer>')

    def test_extract_page_links(self):
        links = extract_page_links(self.HTML, "https://e.com")
        self.assertIn("https://e.com/n1", links)
        self.assertIn("https://x.com/e", links)
        self.assertEqual(len(links), len(set(links)))

    def test_filter_links(self):
        links = ["https://e.com/a", "https://e.com/b.pdf",
                 "https://x.com/c"]
        out = filter_links(links, exclude=[r"\.pdf$"])
        self.assertNotIn("https://e.com/b.pdf", out)
        out2 = filter_links(links, include=[r"e\.com"])
        self.assertEqual(len(out2), 2)

    def test_group_by_section(self):
        groups = group_links_by_section(self.HTML, "https://e.com")
        self.assertEqual(groups["nav"], ["https://e.com/n1"])
        self.assertEqual(groups["main"],
                         ["https://e.com/m1", "https://x.com/e"])
        self.assertEqual(groups["footer"], ["https://e.com/f1"])

    def test_learn_templates(self):
        urls = ["https://e.com/blog/2024/01/01/hello-world",
                "https://e.com/blog/2024/02/02/other-post",
                "https://e.com/product/12345"]
        templates = learn_url_templates(urls)
        by_example = {t["count"] for t in templates}
        self.assertIn(2, by_example)

    def test_site_tree(self):
        tree = build_site_tree(["https://e.com/a/b", "https://e.com/a/c"])
        self.assertIn("a", tree["e.com"])
        self.assertIn("b", tree["e.com"]["a"])

    def test_path_depth_sort(self):
        urls = ["https://e.com/a/b/c", "https://e.com/a", "https://e.com/"]
        self.assertEqual(path_depth(urls[0]), 3)
        self.assertEqual(sort_by_depth(urls)[0], "https://e.com/")

    def test_crawl_plan(self):
        plan = make_crawl_plan("https://e.com/",
                               ["https://e.com/pricing"])
        self.assertEqual(plan["seeds"], ["https://e.com/"])
        self.assertIn("https://e.com/pricing", plan["sitemap_seeds"])

    def test_estimate(self):
        est = estimate_crawl_time(100)
        self.assertEqual(est["pages"], 100)
        self.assertGreater(est["total_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
