from __future__ import annotations

from importlib.metadata import version

import pytest
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation import tracecontext
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


def _extended_traceparent(length: int) -> str:
    prefix = "01-" + "1" * 32 + "-" + "2" * 16 + "-01-"
    assert len(prefix) <= length
    return prefix + "a" * (length - len(prefix))


def test_opentelemetry_api_security_patch_is_installed():
    assert version("opentelemetry-api") == "1.45.2"


@pytest.mark.parametrize("length", [511, 512])
def test_traceparent_at_or_below_limit_reaches_parser(monkeypatch, length):
    propagator = TraceContextTextMapPropagator()
    header = _extended_traceparent(length)
    real_search = tracecontext.re.search
    calls = 0

    def observed_search(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_search(*args, **kwargs)

    monkeypatch.setattr(tracecontext.re, "search", observed_search)
    propagator.extract({"traceparent": header}, context=Context())

    assert calls == 1


@pytest.mark.parametrize("length", [513, 65_536])
def test_traceparent_over_limit_is_rejected_before_regex(monkeypatch, caplog, length):
    propagator = TraceContextTextMapPropagator()
    header = _extended_traceparent(length)

    def regex_must_not_run(*_args, **_kwargs):
        raise AssertionError("oversized traceparent reached the regex parser")

    monkeypatch.setattr(tracecontext.re, "search", regex_must_not_run)
    result = propagator.extract({"traceparent": header}, context=Context())

    assert trace.get_current_span(result).get_span_context().is_valid is False
    assert "exceeded the maximum length of 512 characters" in caplog.text


def test_standard_traceparent_still_extracts_remote_span():
    header = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

    result = TraceContextTextMapPropagator().extract({"traceparent": header})
    span = trace.get_current_span(result).get_span_context()

    assert span.is_valid is True
    assert span.is_remote is True
    assert span.trace_id == int("0af7651916cd43dd8448eb211c80319c", 16)
    assert span.span_id == int("b7ad6b7169203331", 16)
