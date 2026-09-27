"""Independent browser acceptance checks for diary creation and persistence."""

from playwright.sync_api import expect, sync_playwright


def diary_form(page):
    """The diary form is the one with a Date field. Fields are looked up inside it, because the
    note form on the same page also has a Title."""
    return page.locator("form").filter(has=page.get_by_label("Date"))


def test_diary_form_has_required_date_title_and_body(web_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(3000)
        page.goto(web_url)
        for label in ("Date", "Title", "Body"):
            expect(diary_form(page).get_by_label(label)).to_have_js_property("required", True)
        browser.close()


def test_diary_form_submits_and_shows_entry(web_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(3000)
        page.goto(web_url)
        diary_form(page).get_by_label("Date").fill("2026-09-26")
        diary_form(page).get_by_label("Title").fill("A good day")
        diary_form(page).get_by_label("Body").fill("Walked by the water; café after.")
        diary_form(page).get_by_role("button", name="Create diary").click()
        entry = page.get_by_role("listitem").filter(has_text="A good day")
        expect(entry).to_contain_text("Walked by the water; café after.")
        response = page.request.get(web_url + "/api/diaries")
        assert response.status == 200
        assert any(item["title"] == "A good day" for item in response.json())
        browser.close()


def test_diary_entries_remain_visible_after_reload(web_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(3000)
        page.goto(web_url)
        diary_form(page).get_by_label("Date").fill("2026-09-26")
        diary_form(page).get_by_label("Title").fill("A saved day")
        diary_form(page).get_by_label("Body").fill("Persisted after a reload")
        diary_form(page).get_by_role("button", name="Create diary").click()
        page.reload()
        expect(page.get_by_role("listitem").filter(has_text="A saved day")).to_be_visible()
        browser.close()
