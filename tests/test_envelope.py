"""Envelope tests: page-type detection, source authority, freshness, paywall
evidence parser (HTMLParser-based, PR #9 fix).

Tests the real detect_page_type/classify_source/compute_freshness functions
against real HTML. No mocks. Adversarial: false positives (bare "paywall"
token in copy/links/scripts) must not classify as paywall; real paywalls
(visible text, active attributes) must.
"""

from datetime import datetime, timedelta, timezone
from hound_mcp.envelope import (
    detect_page_type, classify_source, compute_freshness, page_type_from_error,
    _paywall_evidence,
)


# ─── Page type detection ───────────────────────────────────────────

class TestDetectPageType:

    def test_pdf_from_content_type(self):
        assert detect_page_type("", "https://x.com", "application/pdf") == "pdf"

    def test_json_from_content_type(self):
        assert detect_page_type("", "https://x.com", "application/json") == "json"

    def test_image_from_content_type(self):
        assert detect_page_type("", "https://x.com", "image/png") == "image"

    def test_empty_html_returns_unknown(self):
        assert detect_page_type("", "https://x.com", "text/html") == "unknown"

    def test_meta_refresh_is_redirect(self):
        html = '<meta http-equiv="refresh" content="0;url=https://x.com/new">'
        assert detect_page_type(html, "https://x.com", "text/html") == "redirect"

    def test_js_location_assignment_with_low_text_is_redirect(self):
        html = '<script>location.href="/new"</script><body></body>'
        assert detect_page_type(html, "https://x.com", "text/html", 100) == "redirect"

    def test_article_tag_detected(self):
        html = '<article><p>Real article content here that is long enough.</p></article>'
        assert detect_page_type(html, "https://example.com", "text/html", 80) == "article"

    def test_qa_markers_detected(self):
        html = '<div class="question">How to X?</div><div class="answer">Do Y.</div>'
        assert "qa" in detect_page_type(html, "https://stackoverflow.com/q/1", "text/html", 80)

    def test_forum_markers_detected(self):
        html = '<div class="post-body">Forum post text.</div>'
        assert "forum" in detect_page_type(html, "https://forum.example.com/t/1", "text/html", 80)

    def test_docs_markers_detected(self):
        html = '<div class="md-nav">Docs sidebar</div><div class="rst-content">Content</div>'
        assert detect_page_type(html, "https://docs.example.com", "text/html", 80) == "docs"

    def test_list_page_detected_many_links(self):
        # 25 same-domain links with little text -> list
        links = "".join(f'<a href="/page/{i}">Link {i}</a>' for i in range(25))
        html = f'<div>{links}</div>'
        assert detect_page_type(html, "https://example.com", "text/html", 200) == "list"

    def test_article_with_many_links_not_list(self):
        # <article> with many links -> article, not list
        links = "".join(f'<a href="/page/{i}">Link {i}</a>' for i in range(25))
        html = f'<article><p>Article text. {links}</p></article>'
        assert detect_page_type(html, "https://example.com", "text/html", 2000) == "article"


# ─── Paywall detection (PR #9 fix) ─────────────────────────────────

class TestPaywallDetection:
    """The bare 'paywall' token must NOT trigger paywall classification.
    Visible subscription phrases and active structural attributes must."""

    def test_real_paywall_phrase_detected(self):
        html = "<main><p>Subscribe to continue reading this article.</p></main>"
        assert detect_page_type(html, "https://x.com", "text/html", 80) == "paywall"

    def test_readme_login_paywall_link_not_paywall(self):
        html = '<article><a href="/login/paywall">login/paywall</a><p>Project README.</p></article>'
        assert detect_page_type(html, "https://github.com/x/y", "text/html", 80) != "paywall"

    def test_paywall_detector_explanation_not_paywall(self):
        html = "<article><p>The paywall detector labels subscription prompts.</p></article>"
        assert detect_page_type(html, "https://x.com", "text/html", 80) != "paywall"

    def test_paywall_phrase_in_script_not_detected(self):
        html = '<script>const x = "subscribe to continue";</script><article><p>Docs.</p></article>'
        assert detect_page_type(html, "https://x.com", "text/html", 80) != "paywall"

    def test_paywall_phrase_in_style_not_detected(self):
        html = '<style>.x::after { content: "subscribe to read"; }</style><article><p>Docs.</p></article>'
        assert detect_page_type(html, "https://x.com", "text/html", 80) != "paywall"

    def test_inline_split_paywall_phrase_detected(self):
        # "Sub<strong>scribe</strong> to continue" must still match visible text
        html = "<article>Sub<strong>scribe</strong> to continue</article>"
        assert detect_page_type(html, "https://x.com", "text/html", 80) == "paywall"

    def test_char_reference_paywall_phrase_detected(self):
        html = "<article>subscr&#105;be to continue</article>"
        assert detect_page_type(html, "https://x.com", "text/html", 80) == "paywall"

    def test_words_split_across_block_boundaries_not_paywall(self):
        html = "<article><p>Sub</p><p>scribe to continue.</p></article>"
        assert detect_page_type(html, "https://x.com", "text/html", 80) != "paywall"

    def test_active_data_paywall_attribute_detected(self):
        html = '<main><div data-paywall="true">Subscription required.</div></main>'
        assert detect_page_type(html, "https://x.com", "text/html", 80) == "paywall"

    def test_data_paywall_without_value_detected(self):
        html = '<aside data-content-gate>Subscription required.</aside>'
        assert detect_page_type(html, "https://x.com", "text/html", 80) == "paywall"

    def test_disabled_data_paywall_not_detected(self):
        html = '<main><div data-paywall="false">Public content.</div></main>'
        assert detect_page_type(html, "https://x.com", "text/html", 80) != "paywall"

    def test_zero_value_data_paywall_not_detected(self):
        html = '<main><div data-paywall="0">Public content.</div></main>'
        assert detect_page_type(html, "https://x.com", "text/html", 80) != "paywall"

    def test_data_paywall_in_template_not_detected(self):
        html = '<template><div data-paywall="true">Hidden</div></template><article><p>Docs.</p></article>'
        assert detect_page_type(html, "https://x.com", "text/html", 80) != "paywall"

    def test_data_paywall_in_noscript_not_detected(self):
        html = '<noscript><div data-paywall="true">Hidden</div></noscript><article><p>Docs.</p></article>'
        assert detect_page_type(html, "https://x.com", "text/html", 80) != "paywall"

    def test_prefixed_attribute_not_detected(self):
        html = '<div x-data-paywall="true">Public.</div>'
        assert detect_page_type(html, "https://x.com", "text/html", 80) != "paywall"

    def test_metadata_tags_ignored_for_attributes(self):
        html = ('<meta data-paywall="true" content="docs">'
                '<link data-content-gate="active">'
                '<base data-subscription-wall="true">'
                '<article><p>Docs.</p></article>')
        assert detect_page_type(html, "https://x.com", "text/html", 80) != "paywall"


# ─── _paywall_evidence (direct HTMLParser test) ────────────────────

class TestPaywallEvidence:

    def test_returns_visible_text(self):
        text, has_attr = _paywall_evidence("<article><p>Hello world</p></article>")
        assert "hello world" in text.lower()
        assert has_attr is False

    def test_excludes_script_content(self):
        text, _ = _paywall_evidence("<script>alert('secret')</script><p>visible</p>")
        assert "secret" not in text
        assert "visible" in text

    def test_excludes_style_content(self):
        text, _ = _paywall_evidence("<style>.x{color:red}</style><p>visible</p>")
        assert "color:red" not in text
        assert "visible" in text

    def test_detects_active_attribute(self):
        _, has_attr = _paywall_evidence('<div data-paywall="true">x</div>')
        assert has_attr is True

    def test_detects_attribute_without_value(self):
        _, has_attr = _paywall_evidence('<div data-content-gate>x</div>')
        assert has_attr is True

    def test_false_value_attribute_not_active(self):
        _, has_attr = _paywall_evidence('<div data-paywall="false">x</div>')
        assert has_attr is False

    def test_malformed_html_does_not_crash(self):
        text, has_attr = _paywall_evidence("<div><p>unclosed")
        assert isinstance(text, str)
        assert isinstance(has_attr, bool)


# ─── Source authority classification ──────────────────────────────

class TestClassifySource:

    def test_github_is_official(self):
        st, off = classify_source("https://github.com/ouli-1242/hound-mcp")
        assert st == "github" and off is True

    def test_github_raw_is_official(self):
        st, off = classify_source("https://raw.githubusercontent.com/user/repo/main/file")
        assert st == "github" and off is True

    def test_github_pages_is_official(self):
        st, off = classify_source("https://user.github.io/project/")
        assert st == "github" and off is True

    def test_gov_is_official(self):
        st, off = classify_source("https://www.nasa.gov/mission")
        assert st == "gov" and off is True

    def test_edu_is_official(self):
        st, off = classify_source("https://www.mit.edu/research")
        assert st == "edu" and off is True

    def test_docs_subdomain_is_official(self):
        st, off = classify_source("https://docs.python.org/3/")
        assert st == "docs-site" and off is True

    def test_developer_subdomain_is_official(self):
        st, off = classify_source("https://developer.mozilla.org/en-US/")
        assert st == "docs-site" and off is True

    def test_stackoverflow_is_qa_not_official(self):
        st, off = classify_source("https://stackoverflow.com/q/123")
        assert st == "qa" and off is False

    def test_reddit_is_forum(self):
        st, off = classify_source("https://www.reddit.com/r/python")
        assert st == "forum" and off is False

    def test_medium_is_blog(self):
        st, off = classify_source("https://medium.com/@user/post")
        assert st == "blog" and off is False

    def test_known_news_domain(self):
        st, off = classify_source("https://www.bbc.com/news/world")
        assert st == "news" and off is False

    def test_unknown_domain(self):
        st, off = classify_source("https://random-site.example.com/page")
        assert st == "unknown" and off is False

    def test_empty_url(self):
        st, off = classify_source("")
        assert st == "unknown" and off is False

    def test_url_with_userinfo_and_port(self):
        st, off = classify_source("https://user:pass@github.com:443/repo")
        assert st == "github" and off is True


# ─── Freshness ─────────────────────────────────────────────────────

class TestFreshness:

    def test_recent_content_not_stale(self):
        meta = {"published_time": "2026-07-01T00:00:00Z"}
        age, stale = compute_freshness(meta, "2026-07-22T00:00:00Z")
        assert age == 21
        assert stale is False

    def test_old_content_is_stale(self):
        meta = {"published_time": "2020-01-01T00:00:00Z"}
        age, stale = compute_freshness(meta, "2026-07-22T00:00:00Z")
        assert stale is True
        assert age > 365

    def test_modified_date_preferred_over_published(self):
        meta = {
            "published_time": "2020-01-01T00:00:00Z",
            "modified_time": "2026-07-20T00:00:00Z",
        }
        age, stale = compute_freshness(meta, "2026-07-22T00:00:00Z")
        assert age == 2
        assert stale is False

    def test_no_date_returns_negative(self):
        age, stale = compute_freshness({}, "2026-07-22T00:00:00Z")
        assert age == -1
        assert stale is False

    def test_future_date_returns_negative(self):
        meta = {"published_time": "2030-01-01T00:00:00Z"}
        age, stale = compute_freshness(meta, "2026-07-22T00:00:00Z")
        assert age == -1
        assert stale is False

    def test_compact_date_format(self):
        meta = {"date": "20260701"}
        age, stale = compute_freshness(meta, "2026-07-22T00:00:00Z")
        assert age == 21

    def test_no_fetched_at_falls_back_to_today(self):
        # Date relative to today so the test never rots: no fetched_at means
        # compute_freshness falls back to now(UTC), so age is exactly the delta.
        days_ago = 5
        published = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat() + "T00:00:00Z"
        meta = {"published_time": published}
        age, stale = compute_freshness(meta, "")
        assert days_ago - 1 <= age <= days_ago + 1
        assert stale is False


# ─── page_type_from_error ─────────────────────────────────────────

class TestPageTypeFromError:

    def test_js_shell_error(self):
        assert page_type_from_error("js_shell_detected: page requires JS") == "js_shell"

    def test_auth_required_error(self):
        assert page_type_from_error("auth_required: login needed") == "auth_wall"

    def test_geo_redirect_error(self):
        assert page_type_from_error("geo_redirect_detected: region selector") == "redirect"

    def test_empty_error_returns_empty(self):
        assert page_type_from_error("") == ""

    def test_unrelated_error_returns_empty(self):
        assert page_type_from_error("network_error: timeout") == ""
