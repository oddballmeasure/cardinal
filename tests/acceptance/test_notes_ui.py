"""Independent browser acceptance checks for note entry and list rendering."""

from playwright.sync_api import expect, sync_playwright

# The issue asks for a note form but names no button, so the checks submit the form the way a user
# would, through its submit control, rather than requiring a label the issue never specified.
SUBMIT = "button:not([type]), button[type=submit], input[type=submit]"


def note_form(page):
    """The note form is the one with a Tags field. Fields are looked up inside it, because other
    forms on the page (the diary form) may also have a Title."""
    return page.locator("form").filter(has=page.get_by_label("Tags"))


def submit(page) -> None:
    note_form(page).locator(SUBMIT).first.click()


def test_note_form_has_required_title_and_tags_fields(web_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(3000)
        page.goto(web_url)
        expect(note_form(page).get_by_label("Title")).to_have_js_property("required", True)
        expect(note_form(page).get_by_label("Tags")).to_be_visible()
        browser.close()


def test_note_form_submits_title_and_comma_separated_tags(web_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(3000)
        page.goto(web_url)
        note_form(page).get_by_label("Title").fill("Café, morning plan")
        note_form(page).get_by_label("Tags").fill("work, travel, é")
        submit(page)
        note = page.get_by_role("listitem").filter(has_text="Café, morning plan")
        expect(note).to_be_visible()
        response = page.request.get(web_url + "/api/notes")
        assert response.status == 200
        created = next(item for item in response.json() if item["title"] == "Café, morning plan")
        assert created["tags"] == ["work", "travel", "é"]
        browser.close()


def test_note_list_updates_without_reload_and_empty_title_is_rejected(web_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(3000)
        page.goto(web_url)
        before_count = page.get_by_role("listitem").count()
        note_form(page).get_by_label("Tags").fill("work")
        submit(page)
        expect(note_form(page).get_by_label("Title")).to_have_js_property("validity.valid", False)
        expect(page.get_by_role("listitem")).to_have_count(before_count)
        note_form(page).get_by_label("Title").fill("Current session note")
        submit(page)
        expect(page.get_by_role("listitem").filter(has_text="Current session note")).to_be_visible()
        browser.close()
