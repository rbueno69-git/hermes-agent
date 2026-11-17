from __future__ import annotations

from importlib.metadata import version

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation import tracecontext
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


def test_opentelemetry_api_security_patch_is_installed():
    assert version("opentelemetry-api") == "1.45.2"


def test_traceparent_over_512_bytes_is_rejected_before_regex(monkeypatch, caplog):
    propagator = TraceContextTextMapPropagator()
    oversized = "01-" + "1" * 32 + "-" + "2" * 16 + "-01-" + "a" * 457
    assert len(oversized) == 513

    def regex_must_not_run(*_args, **_kwargs):
        raise AssertionError("oversized traceparent reached the regex parser")

    monkeypatch.setattr(tracecontext.re, "search", regex_must_not_run)
    result = propagator.extract({"traceparent": oversized}, context=Context())

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
