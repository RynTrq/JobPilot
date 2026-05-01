import pytest
from playwright.async_api import async_playwright

from backend.scraping.adapters.browser_form import BrowserFormAdapter


def test_job_alert_and_chat_fields_are_not_application_form() -> None:
    adapter = BrowserFormAdapter()
    raw_fields = [
        {
            "label_text": "Email address",
            "field_type": "email",
            "element_id": "notifiedEmail",
            "placeholder": "name@email.com",
            "enabled": True,
            "visible": True,
        },
        {
            "label_text": "Ask anything",
            "field_type": "text",
            "element_id": "PhenomChatbotFooterInput",
            "placeholder": "Ask anything",
            "enabled": True,
            "visible": True,
        },
    ]

    assert adapter._raw_fields_look_like_non_application_widget(raw_fields)
    assert not adapter._raw_fields_look_like_application(raw_fields)
    assert adapter._raw_fields_look_like_search_filter(raw_fields)


def test_personal_information_fields_are_application_form() -> None:
    adapter = BrowserFormAdapter()
    raw_fields = [
        {
            "label_text": "First Name",
            "field_type": "text",
            "name": "firstName",
            "element_id": "firstName",
            "enabled": True,
            "visible": True,
        },
        {
            "label_text": "Last Name",
            "field_type": "text",
            "name": "lastName",
            "element_id": "lastName",
            "enabled": True,
            "visible": True,
        },
        {
            "label_text": "Email",
            "field_type": "email",
            "element_id": "email",
            "enabled": True,
            "visible": True,
        },
        {
            "label_text": "Phone Number",
            "field_type": "text",
            "element_id": "cellPhone",
            "enabled": True,
            "visible": True,
        },
    ]

    assert not adapter._raw_fields_look_like_non_application_widget(raw_fields)
    assert adapter._raw_fields_look_like_application(raw_fields)
    assert not adapter._raw_fields_look_like_search_filter(raw_fields)


@pytest.mark.asyncio
async def test_enumerate_fields_skips_aria_hidden_address_internals() -> None:
    adapter = BrowserFormAdapter()
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Playwright browser unavailable: {exc}")
        page = await browser.new_page()
        try:
            await page.set_content(
                """
                <form aria-label="Application form">
                  <label for="firstName">First name</label>
                  <input id="firstName" name="firstName" />
                  <label for="lastName">Last name</label>
                  <input id="lastName" name="lastName" />
                  <label for="email">Email</label>
                  <input id="email" name="email" type="email" />
                  <label for="phone">Phone</label>
                  <input id="phone" name="phone" />
                  <label>
                    Address
                    <input id="city" name="city" aria-hidden="true" tabindex="-1" style="display:block;width:20px;height:20px" />
                    <input id="postcode" name="postcode" aria-hidden="true" tabindex="-1" style="display:block;width:20px;height:20px" />
                  </label>
                </form>
                """
            )
            fields = await adapter.enumerate_fields(page)
        finally:
            await browser.close()

    selectors = {field.selector for field in fields}
    names = {field.name for field in fields}
    assert "#city" not in selectors
    assert "#postcode" not in selectors
    assert {"firstName", "lastName", "email", "phone"}.issubset(names)
