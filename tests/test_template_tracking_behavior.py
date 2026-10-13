from pathlib import Path
from unittest.mock import MagicMock, Mock, call

import pytest
import sentry_sdk
from jinja2 import TemplateNotFound

from infra.error_tracking.adapter import ErrorTrackingAdapter
from infra.template_engine import TemplateEngineAdapter


def test_disk_template_renders_nested_path_with_escaping_and_empty_none(
    tmp_path: Path,
) -> None:
    template_dir = tmp_path / "mail" / "onboarding"
    template_dir.mkdir(parents=True)
    template_text = "\n  Hello {{ person }}!\n  Note: {{ note }}\n"
    (template_dir / "welcome.html").write_text(template_text, encoding="utf-8")
    engine = TemplateEngineAdapter(templates_path=tmp_path, base_path="mail")

    assert engine.get_template_text("onboarding/welcome.html") == template_text
    assert engine.render(
        "onboarding/welcome.html", person="<script>alert('x')</script>", note=None
    ) == "Hello &lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;!\nNote: "


@pytest.mark.parametrize("template_name", ["missing.html", "onboarding/missing.html"])
def test_missing_disk_template_is_reported_as_not_found(
    tmp_path: Path, template_name: str
) -> None:
    engine = TemplateEngineAdapter(templates_path=tmp_path)
    with pytest.raises(TemplateNotFound) as error:
        engine.render(template_name, person="anyone")
    assert error.value.name == template_name
    with pytest.raises(TemplateNotFound):
        engine.get_template_text(template_name)


def test_dynamic_template_tags_represent_inputs_and_rendered_output(
    tmp_path: Path,
) -> None:
    engine = TemplateEngineAdapter(templates_path=tmp_path)
    template_text = (
        "{% set salutation = 'Hello' %}"
        "{{ salutation }} {{ person.name }}: "
        "{% for item in items %}{{ item }}{% if not loop.last %}, {% endif %}{% endfor %}"
        "{{ optional }}"
    )
    assert set(engine.extract_tags_from_string(template_text)) == {
        "person", "items", "optional"
    }
    assert engine.render_string(
        template_text,
        person={"name": "<Sam>"},
        items=["<one>", "two"],
        optional=None,
    ) == "Hello &lt;Sam&gt;: &lt;one&gt;, two"


@pytest.mark.parametrize("template_text", [None, "", "{% if person %}hello"])
def test_invalid_template_syntax_is_rejected(
    tmp_path: Path, template_text: str | None
) -> None:
    engine = TemplateEngineAdapter(templates_path=tmp_path)
    assert not engine.is_valid(template_text)
    assert engine.is_valid("{% if person %}Hello {{ person }}{% endif %}")


def test_console_tracker_reports_exception_and_context_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    tracker = ErrorTrackingAdapter("console")
    tracker.init()
    try:
        raise ValueError("candidate answer could not be parsed")
    except ValueError as exc:
        tracker.capture_exception(exc, interview_id="interview-42", step="evaluation")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Captured exception" in captured.err
    assert "interview-42" in captured.err
    assert "evaluation" in captured.err
    assert "ValueError: candidate answer could not be parsed" in captured.err


def test_sentry_tracker_ignores_inactive_sdk_and_sends_exception_with_context_when_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = MagicMock()
    scope.__enter__.return_value = scope
    client = Mock()
    client.is_active.return_value = False
    sdk_init = Mock()
    sdk_capture = Mock()
    monkeypatch.setattr(sentry_sdk, "init", sdk_init)
    monkeypatch.setattr(sentry_sdk, "get_client", Mock(return_value=client))
    monkeypatch.setattr(sentry_sdk, "new_scope", Mock(return_value=scope))
    monkeypatch.setattr(sentry_sdk, "capture_exception", sdk_capture)

    tracker = ErrorTrackingAdapter("sentry", dsn="https://example.invalid/1")
    tracker.init()
    sdk_init.assert_called_once_with(dsn="https://example.invalid/1")
    error = RuntimeError("delivery failed")
    tracker.capture_exception(error, interview_id="interview-42")
    sdk_capture.assert_not_called()
    scope.set_extra.assert_not_called()

    client.is_active.return_value = True
    tracker.capture_exception(error, interview_id="interview-42", stage="delivery")
    assert scope.set_extra.call_args_list == [
        call("interview_id", "interview-42"),
        call("stage", "delivery"),
    ]
    sdk_capture.assert_called_once_with(error)
    scope.__exit__.assert_called_once()


@pytest.mark.parametrize("failure_site", ["get_client", "new_scope", "capture_exception"])
def test_sentry_failure_does_not_replace_caller_exception(
    monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    client = Mock()
    client.is_active.return_value = True
    scope = MagicMock()
    scope.__enter__.return_value = scope
    monkeypatch.setattr(sentry_sdk, "get_client", Mock(return_value=client))
    monkeypatch.setattr(sentry_sdk, "new_scope", Mock(return_value=scope))
    monkeypatch.setattr(sentry_sdk, "capture_exception", Mock())
    failing = Mock(side_effect=ConnectionError("tracking unavailable"))
    monkeypatch.setattr(sentry_sdk, failure_site, failing)

    tracker = ErrorTrackingAdapter("sentry")
    caller_error = ValueError("original failure")
    assert tracker.capture_exception(caller_error, stage="grading") is None
    failing.assert_called_once()
    assert str(caller_error) == "original failure"
