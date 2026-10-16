"""Q023: real fetch_rotation failures keep their contract and log safe fields only.

No provider service, network client, shared fixtures, or subprocess is needed.
These synthetic failures do not reproduce the production run or establish its cause.
"""
from __future__ import annotations

import logging

import pytest

import portfolio_rotation as rotation


SYMBOL = 'BNBUSDT'
T0 = 1736035200
EVENT = 'rotation_stream_failure'
# Synthetic sentinel only; never copy a real credential or production message here.
SENSITIVE = 'token=FAKE_TOKEN key=FAKE_KEY https://invalid.example/ohlcv?signature=FAKE_SIGNATURE'
PROVENANCE = {'source': 'synthetic.fixture', 'cache_hit': False}


class FetchFailure(ValueError):
    def __init__(self, error_type):
        super().__init__(SENSITIVE)
        self.error_type = error_type
        self.provenance = PROVENANCE


class TypedRateLimit(RuntimeError):
    error_type = 'RATE_LIMITED'
    provenance = PROVENANCE


@pytest.fixture
def config(monkeypatch):
    # Keep the production executor/rate limiter; reset only its prior-run timestamp.
    monkeypatch.setattr(rotation, '_NEXT_FETCH', 0.0)
    return rotation.RotationConfig(
        symbols=(SYMBOL,), specs={}, selected_at=T0, top_k=1,
        risk_enabled=False, start=T0, end=T0 + rotation.DAY,
        fetch_start=T0 - 8 * rotation.DAY,
    )


def valid_stream(config):
    rows = [{'ts': ts, 'open': '1', 'close': '1'}
            for ts in range(config.fetch_start, config.end, rotation.DAY)]
    return rows, 'synthetic.fixture', False, False


def failing_fetcher(config, exc):
    def fetcher(symbol, start, end):
        assert (start, end) == (config.fetch_start, config.end)
        if symbol == SYMBOL:
            raise exc
        return valid_stream(config)
    return fetcher


def diagnostic_records(caplog):
    return [r for r in caplog.records if r.name == rotation.logger.name]


def assert_safe_record(caplog, *, stage, exception_class):
    records = diagnostic_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert (record.event, record.stage, record.exception_class, record.symbol) == (
        EVENT, stage, exception_class, SYMBOL,
    )
    assert record.msg == 'rotation_stream_failure stage=%s exception_class=%s symbol=%s'
    assert record.args == (stage, exception_class, SYMBOL)
    assert not record.exc_info
    assert record.exc_text is None
    assert record.stack_info is None
    # Inspect the entire raw record as well as the formatted sink output.
    captured = caplog.text + repr(record.__dict__)
    for forbidden in (SENSITIVE, 'FAKE_TOKEN', 'FAKE_KEY', 'FAKE_SIGNATURE',
                      'https://invalid.example', 'Traceback (most recent call last)'):
        assert forbidden not in captured


def test_fetch_keyerror_logs_safe_fields_and_preserves_required_stream_missing(config, caplog):
    original = KeyError(SENSITIVE)
    with caplog.at_level(logging.WARNING, logger=rotation.logger.name):
        with pytest.raises(rotation.RotationError) as caught:
            rotation.fetch_rotation(config, failing_fetcher(config, original))
    error = caught.value
    assert (error.code, error.reason) == ('INSUFFICIENT_DATA', 'required_stream_missing')
    assert str(error) == f'required stream {SYMBOL} failed: {original}'
    assert error.provenance is None and error.stream is None
    assert error.__cause__ is original
    assert_safe_record(caplog, stage='fetch', exception_class='KeyError')


def test_bad_return_shape_logs_unpack_and_keeps_original_wrapping(config, caplog):
    def fetcher(symbol, start, end):
        return ([],) if symbol == SYMBOL else valid_stream(config)

    with caplog.at_level(logging.WARNING, logger=rotation.logger.name):
        with pytest.raises(rotation.RotationError) as caught:
            rotation.fetch_rotation(config, fetcher)
    error = caught.value
    assert (error.code, error.reason) == ('INSUFFICIENT_DATA', 'required_stream_missing')
    assert type(error.__cause__) is ValueError
    assert str(error) == f'required stream {SYMBOL} failed: {error.__cause__}'
    assert error.provenance is None and error.stream is None
    assert_safe_record(caplog, stage='unpack', exception_class='ValueError')


def test_fetch_valueerror_is_fetch_even_though_bad_unpack_also_raises_valueerror(config, caplog):
    original = ValueError(SENSITIVE)
    with caplog.at_level(logging.WARNING, logger=rotation.logger.name):
        with pytest.raises(rotation.RotationError) as caught:
            rotation.fetch_rotation(config, failing_fetcher(config, original))
    assert caught.value.__cause__ is original
    assert (caught.value.code, caught.value.reason) == ('INSUFFICIENT_DATA', 'required_stream_missing')
    assert_safe_record(caplog, stage='fetch', exception_class='ValueError')


def test_return_iterator_keyerror_is_unpack_even_though_fetch_can_raise_keyerror(config, caplog):
    original = KeyError(SENSITIVE)

    class BrokenReturn:
        def __iter__(self):
            raise original

    def fetcher(symbol, start, end):
        return BrokenReturn() if symbol == SYMBOL else valid_stream(config)

    with caplog.at_level(logging.WARNING, logger=rotation.logger.name):
        with pytest.raises(rotation.RotationError) as caught:
            rotation.fetch_rotation(config, fetcher)
    assert caught.value.__cause__ is original
    assert (caught.value.code, caught.value.reason) == ('INSUFFICIENT_DATA', 'required_stream_missing')
    assert_safe_record(caplog, stage='unpack', exception_class='KeyError')


@pytest.mark.parametrize('original', [
    rotation.RotationError('DATA_FETCH_TIMEOUT', 'fetch_deadline', SENSITIVE,
                           provenance=PROVENANCE),
    rotation.RotationError('NO_DATA', 'stream_no_data', SENSITIVE, stream=('fixture', False, True)),
], ids=['timeout', 'no-data'])
def test_existing_rotation_error_is_reraised_unchanged_without_new_log(config, caplog, original):
    with caplog.at_level(logging.WARNING, logger=rotation.logger.name):
        with pytest.raises(rotation.RotationError) as caught:
            rotation.fetch_rotation(config, failing_fetcher(config, original))
    assert caught.value is original
    assert caught.value.__cause__ is None
    assert diagnostic_records(caplog) == []


@pytest.mark.parametrize('original, code, reason, message', [
    (ValueError('NO_DATA'), 'NO_DATA', 'stream_no_data',
     f'required stream {SYMBOL}: no market data in requested range'),
    (FetchFailure('NO_DATA'), 'NO_DATA', 'stream_no_data',
     f'required stream {SYMBOL}: no market data in requested range'),
    (FetchFailure('FETCH_FAILED'), 'DATA_FETCH_FAILED', 'stream_fetch_failed',
     f'required stream {SYMBOL}: market data fetch failed'),
    # error_type still takes precedence over RuntimeError's RATE_LIMITED text.
    (TypedRateLimit('RATE_LIMITED ' + SENSITIVE), 'DATA_FETCH_FAILED', 'stream_fetch_failed',
     f'required stream {SYMBOL}: market data fetch failed'),
    (RuntimeError('RATE_LIMITED ' + SENSITIVE), 'RATE_LIMITED', 'rate_limited',
     f'required stream {SYMBOL}: exchange rate limit exceeded'),
], ids=['legacy-no-data', 'typed-no-data', 'typed-failure', 'typed-rate-limit', 'legacy-rate-limit'])
def test_existing_fetch_classification_and_provenance_stay_unchanged(
    config, caplog, original, code, reason, message,
):
    with caplog.at_level(logging.WARNING, logger=rotation.logger.name):
        with pytest.raises(rotation.RotationError) as caught:
            rotation.fetch_rotation(config, failing_fetcher(config, original))
    error = caught.value
    assert (error.code, error.reason, str(error)) == (code, reason, message)
    assert error.provenance == getattr(original, 'provenance', None)
    assert error.stream is None
    assert error.__cause__ is original
    assert_safe_record(caplog, stage='fetch', exception_class=type(original).__name__)


@pytest.mark.parametrize('failure_point', ['logger-call', 'sink'])
def test_logging_failure_cannot_mask_original_wrapping(config, caplog, monkeypatch, failure_point):
    original = KeyError(SENSITIVE)

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic logger failure ' + SENSITIVE)

    class BrokenSink(logging.Handler):
        def emit(self, record):
            fail()

    handler = BrokenSink()
    if failure_point == 'logger-call':
        monkeypatch.setattr(rotation.logger, 'warning', fail)
    else:
        rotation.logger.addHandler(handler)
    try:
        with caplog.at_level(logging.WARNING, logger=rotation.logger.name):
            with pytest.raises(rotation.RotationError) as caught:
                rotation.fetch_rotation(config, failing_fetcher(config, original))
        error = caught.value
        assert (error.code, error.reason) == ('INSUFFICIENT_DATA', 'required_stream_missing')
        assert str(error) == f'required stream {SYMBOL} failed: {original}'
        assert error.__cause__ is original
        assert error.__context__ is original
        assert error.provenance is None and error.stream is None
        assert SENSITIVE not in caplog.text
    finally:
        if failure_point == 'sink':
            rotation.logger.removeHandler(handler)


def test_valid_streams_preserve_pool_and_emit_no_failure_diagnostic(config, caplog):
    calls = []

    def fetcher(symbol, start, end):
        calls.append((symbol, start, end))
        return valid_stream(config)

    with caplog.at_level(logging.WARNING, logger=rotation.logger.name):
        streams = rotation.fetch_rotation(config, fetcher)
    assert set(streams) == {SYMBOL, rotation.BTC}
    assert streams == {s: valid_stream(config) for s in (SYMBOL, rotation.BTC)}
    assert sorted(calls) == sorted((s, config.fetch_start, config.end) for s in (SYMBOL, rotation.BTC))
    assert diagnostic_records(caplog) == []


def test_stream_gap_stays_separate_from_fetch_and_unpack_failures(config, caplog):
    def fetcher(symbol, start, end):
        rows, source, central, cached = valid_stream(config)
        return (rows[:-1] if symbol == SYMBOL else rows), source, central, cached

    with caplog.at_level(logging.WARNING, logger=rotation.logger.name):
        with pytest.raises(rotation.RotationError) as caught:
            rotation.fetch_rotation(config, fetcher)
    assert (caught.value.code, caught.value.reason) == ('INSUFFICIENT_DATA', 'stream_gap')
    assert caught.value.stream == ('synthetic.fixture', False, False)
    assert caught.value.__cause__ is None
    assert diagnostic_records(caplog) == []
