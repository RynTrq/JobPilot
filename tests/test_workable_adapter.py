"""Tests covering the WorkableAdapter bot-detection / form-fill hardening.

These tests use lightweight fakes for Playwright primitives so they can run
without spinning up a real browser.  The behaviour under test is purely the
Python-side bookkeeping: what selectors the adapter probes, when it decides a
URL field needs hard-clearing, and how it translates the
"Verification failed / Something went wrong" banner into a
:class:`SubmitResult` error string.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from backend.form.answerer import FormField
from backend.scraping.adapters.base import SubmitResult
from backend.scraping.adapters.workable import WorkableAdapter


# ---------------------------------------------------------------------------
# Lightweight Playwright fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeLocator:
    selector: str
    page: "_FakePage"

    @property
    def first(self) -> "_FakeLocator":
        # Playwright's Locator exposes `.first` as a property; the fake just
        # returns itself so call sites like ``page.locator(sel).first`` work.
        return self

    async def count(self) -> int:
        return 1

    async def evaluate(self, script: str, *args: Any) -> Any:
        # Capture clear-input calls so the test can assert they happened.
        self.page.locator_evaluate_calls.append((self.selector, script, args))
        return ""


class _FakePage:
    """Just enough of the Playwright Page API for adapter unit tests."""

    def __init__(self, *, evaluate_responses: list[Any] | None = None,
                 query_selector_responses: dict[str, Any] | None = None) -> None:
        self.evaluate_responses = list(evaluate_responses or [])
        self.evaluate_calls: list[str] = []
        self.query_selector_responses = dict(query_selector_responses or {})
        self.locator_evaluate_calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.url = "https://apply.workable.com/example/j/ABCDEF/apply/"

    async def evaluate(self, script: str, *args: Any) -> Any:
        self.evaluate_calls.append(script)
        if self.evaluate_responses:
            response = self.evaluate_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return ""

    async def query_selector(self, selector: str) -> Any:
        return self.query_selector_responses.get(selector)

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector=selector, page=self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Callable[..., Any]) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_adapter() -> WorkableAdapter:
    return WorkableAdapter()


def _field(label: str, *, ftype: str = "text", name: str | None = None,
           selector: str | None = "[data-jobpilot-field='1']") -> FormField:
    return FormField(
        label_text=label,
        field_type=ftype,
        selector=selector,
        name=name,
    )


# ---------------------------------------------------------------------------
# URL-field detection
# ---------------------------------------------------------------------------


class TestUrlFieldDetection:
    """Workable's React inputs can revert ``locator.fill('')`` and cause
    concatenated URLs.  The adapter must opt these labels into the hard-clear
    path while leaving unrelated text fields alone."""

    @pytest.mark.parametrize("label", [
        "LinkedIn URL",
        "GitHub Profile",
        "Personal website",
        "Portfolio (Optional)",
        "Other links",
        "Other links (Optional)",
        "Additional links",
        "Twitter URL",
        "Profile URL",
    ])
    def test_url_like_labels_trigger_hard_clear(self, label: str) -> None:
        assert WorkableAdapter._looks_like_url_field(_field(label)) is True

    @pytest.mark.parametrize("label", [
        "First Name",
        "Email",
        "Phone",
        "Cover Letter",
        "Why do you want to work here?",
        "Notice period",
    ])
    def test_non_url_fields_are_ignored(self, label: str) -> None:
        assert WorkableAdapter._looks_like_url_field(_field(label)) is False

    def test_non_text_field_types_are_ignored(self) -> None:
        # A select/dropdown labelled "Profile" must not trigger the URL clear,
        # since the hard-clear script only makes sense for HTMLInputElement.
        assert (
            WorkableAdapter._looks_like_url_field(_field("Profile", ftype="select"))
            is False
        )


# ---------------------------------------------------------------------------
# fill_field — hard clear path
# ---------------------------------------------------------------------------


class TestFillFieldHardClear:
    def test_url_field_runs_native_setter_clear(self, monkeypatch) -> None:
        adapter = _make_adapter()

        # Capture any super().fill_field call so we know delegation still happens.
        delegated: list[Any] = []

        async def fake_super_fill_field(self_inner, page_inner, field_inner, value_inner):
            delegated.append((field_inner.label_text, value_inner))

        monkeypatch.setattr(
            "backend.scraping.adapters.configured.ConfiguredPlatformAdapter.fill_field",
            fake_super_fill_field,
        )

        page = _FakePage()
        url_field = _field("Other links (Optional)", selector="#other-links")

        _run(adapter.fill_field(page, url_field, "https://example.com"))

        # Native-setter clear must run on the Workable URL field before the
        # generic fill_field is invoked.
        assert page.locator_evaluate_calls, (
            "expected the adapter to evaluate the hard-clear script on the URL field"
        )
        selector, script, _args = page.locator_evaluate_calls[0]
        assert selector == "#other-links"
        assert "Object.getOwnPropertyDescriptor" in script
        assert "dispatchEvent(new Event('input'" in script

        # And we must still delegate to the generic fill flow.
        assert delegated == [("Other links (Optional)", "https://example.com")]

    def test_non_url_field_skips_hard_clear(self, monkeypatch) -> None:
        adapter = _make_adapter()
        delegated: list[Any] = []

        async def fake_super_fill_field(self_inner, page_inner, field_inner, value_inner):
            delegated.append((field_inner.label_text, value_inner))

        monkeypatch.setattr(
            "backend.scraping.adapters.configured.ConfiguredPlatformAdapter.fill_field",
            fake_super_fill_field,
        )

        page = _FakePage()
        text_field = _field("First Name", selector="#first-name")

        _run(adapter.fill_field(page, text_field, "Raiyaan"))

        assert page.locator_evaluate_calls == []
        assert delegated == [("First Name", "Raiyaan")]


# ---------------------------------------------------------------------------
# Submit guard — Turnstile token + bot banner translation
# ---------------------------------------------------------------------------


class TestSubmitTurnstileGuard:
    def test_submit_returns_actionable_error_when_token_missing(self, monkeypatch) -> None:
        adapter = _make_adapter()

        async def fake_super_submit(self_inner, page_inner):
            # If the guard is working we should never reach this branch.
            raise AssertionError("super().submit() should not be invoked when Turnstile is unverified")

        monkeypatch.setattr(
            "backend.scraping.adapters.configured.ConfiguredPlatformAdapter.submit",
            fake_super_submit,
        )

        page = _FakePage(
            evaluate_responses=[
                "",  # _wait_for_turnstile_token: empty -> keeps polling, eventually times out
            ],
            query_selector_responses={
                "[data-sitekey]": object(),
            },
        )

        # Shorten the Turnstile wait so the test runs quickly.
        async def fast_wait(self_inner, page_inner, *, timeout_ms):
            return False

        monkeypatch.setattr(WorkableAdapter, "_wait_for_turnstile_token", fast_wait)

        async def fake_failed(self_inner, page_inner):
            return True

        monkeypatch.setattr(WorkableAdapter, "_turnstile_failed", fake_failed)

        result = _run(adapter.submit(page))
        assert isinstance(result, SubmitResult)
        assert result.ok is False
        assert "workable_turnstile_failed" in (result.error or "")
        assert WorkableAdapter.TURNSTILE_SITE_KEY in (result.error or "")

    def test_submit_translates_bot_banner_to_actionable_error(self, monkeypatch) -> None:
        adapter = _make_adapter()

        async def fake_super_submit(self_inner, page_inner):
            return SubmitResult(ok=False, error="no confirmation")

        monkeypatch.setattr(
            "backend.scraping.adapters.configured.ConfiguredPlatformAdapter.submit",
            fake_super_submit,
        )

        # No Turnstile widget is present -> super().submit() runs and fails;
        # the banner read returns Workable's bot-detection copy.
        async def fake_widget_absent(self_inner, page_inner):
            return False

        async def fake_banner(self_inner, page_inner):
            return "Something went wrong. We are working on this, please try again later."

        monkeypatch.setattr(WorkableAdapter, "_turnstile_widget_present", fake_widget_absent)
        monkeypatch.setattr(WorkableAdapter, "_read_error_banner", fake_banner)

        page = _FakePage()
        result = _run(adapter.submit(page))

        assert result.ok is False
        assert "workable_submit_blocked_by_bot_detection" in (result.error or "")
        assert "Something went wrong" in (result.error or "")

    def test_submit_passthrough_when_super_succeeds(self, monkeypatch) -> None:
        adapter = _make_adapter()

        async def fake_super_submit(self_inner, page_inner):
            return SubmitResult(ok=True, confirmation_text="Thank you for applying!")

        async def fake_widget_absent(self_inner, page_inner):
            return False

        monkeypatch.setattr(
            "backend.scraping.adapters.configured.ConfiguredPlatformAdapter.submit",
            fake_super_submit,
        )
        monkeypatch.setattr(WorkableAdapter, "_turnstile_widget_present", fake_widget_absent)

        result = _run(adapter.submit(_FakePage()))
        assert result.ok is True
        assert result.confirmation_text == "Thank you for applying!"


# ---------------------------------------------------------------------------
# Banner classification
# ---------------------------------------------------------------------------


class TestBannerClassification:
    @pytest.mark.parametrize("banner", [
        "Verification failed",
        "VERIFICATION FAILED",
        "Something went wrong. We are working on this, please try again later.",
        "Please try again later.",
    ])
    def test_recognised_bot_banners(self, banner: str) -> None:
        assert WorkableAdapter._looks_like_bot_banner(banner) is True

    @pytest.mark.parametrize("banner", [
        "Email is required",
        "First name must be at least 2 characters",
        "",
    ])
    def test_validation_banners_are_not_treated_as_bot(self, banner: str) -> None:
        assert WorkableAdapter._looks_like_bot_banner(banner) is False

    def test_recognised_confirmation_banners(self) -> None:
        assert WorkableAdapter._looks_like_confirmation("Thank you for applying!") is True
        assert WorkableAdapter._looks_like_confirmation("We received your application") is True
        assert WorkableAdapter._looks_like_confirmation("Email is required") is False


# ---------------------------------------------------------------------------
# Filler submit gate — contract tests
# ---------------------------------------------------------------------------


class TestSubmitGate:
    """The submit gate is the user-facing contract:

      "Hit submit only after filling the form AND attaching the resume."

    These tests exercise FormFiller._await_pending_document_tasks and
    FormFiller._verify_documents_attached without spinning up a real browser
    or LLM by stubbing every dependency.
    """

    def _make_filler(self):
        from backend.form.filler import FormFiller

        # FormFiller wires through app_state for streaming/orch/manual_takeover
        # — the stubs below cover everything the gate touches.
        class _Stream:
            async def publish(self, *_args, **_kwargs):
                pass

        class _OrchState:
            run_id = "test-run"
            correlation_id = "test-corr"
            current_stage = None
            current_stage_message = None

        class _Orch:
            state = _OrchState()

        class _AppState:
            stream = _Stream()
            orch = _Orch()
            candidate_data = {}

        return FormFiller.__new__(FormFiller).__class__(_AppState())

    def test_await_pending_document_tasks_blocks_until_done(self) -> None:
        filler = self._make_filler()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            order: list[str] = []

            async def slow_doc_task():
                await asyncio.sleep(0.05)
                order.append("doc_task_done")

            doc_task = loop.create_task(slow_doc_task())
            document_paths = {"_doc_task": doc_task}

            async def gate_then_record():
                await filler._await_pending_document_tasks(document_paths, "https://example.com")
                order.append("gate_returned")

            loop.run_until_complete(gate_then_record())
        finally:
            loop.close()

        # Gate must observe the doc task completing BEFORE returning.
        assert order == ["doc_task_done", "gate_returned"]

    def test_await_pending_document_tasks_returns_immediately_when_done(self) -> None:
        filler = self._make_filler()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)

            async def already_done_task():
                return None

            doc_task = loop.create_task(already_done_task())
            loop.run_until_complete(asyncio.sleep(0))  # let it complete

            assert doc_task.done()
            document_paths = {"_doc_task": doc_task}

            # Should not raise even though the task is already complete.
            loop.run_until_complete(
                filler._await_pending_document_tasks(document_paths, None)
            )
        finally:
            loop.close()

    def test_verify_documents_attached_raises_when_resume_missing(self, monkeypatch) -> None:
        """If the form has a required file input, no file is attached, and
        the resume PDF is not on disk, the gate must REFUSE to submit.
        """
        filler = self._make_filler()

        class _FakeAdapter:
            async def enumerate_fields(self, _page):
                # Single required file field labelled "Resume" with no
                # file attached and no resume_path on disk.
                return [
                    FormField(
                        label_text="Resume",
                        field_type="file",
                        selector="#resume",
                        required=True,
                    )
                ]

        page = _FakePage(evaluate_responses=[])

        # The locator(...).first.evaluate(...) call inside the gate returns
        # False to signal "no file attached".
        async def fake_locator_evaluate(self_inner, _script):
            return False

        monkeypatch.setattr(_FakeLocator, "evaluate", fake_locator_evaluate, raising=True)

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            with pytest.raises(RuntimeError, match="submit_gate_blocked"):
                loop.run_until_complete(
                    filler._verify_documents_attached(
                        page,
                        _FakeAdapter(),
                        {"resume_path": None},
                        "https://example.com",
                    )
                )
        finally:
            loop.close()

    def test_verify_documents_attached_passes_when_no_file_field(self) -> None:
        """A form without any file inputs must NOT block on document
        attachment checks (e.g. simple Workable jobs that only ask for
        custom questions)."""
        filler = self._make_filler()

        class _FakeAdapter:
            async def enumerate_fields(self, _page):
                return [FormField(label_text="First Name", field_type="text", selector="#fn", required=True)]

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                filler._verify_documents_attached(
                    _FakePage(),
                    _FakeAdapter(),
                    {"resume_path": None},
                    None,
                )
            )
        finally:
            loop.close()
