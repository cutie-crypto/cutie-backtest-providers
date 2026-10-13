"""Same-event-loop HTTP regression for PROV-HEALTH-1 (no external market data)."""
import asyncio
import json
import threading
import time

import httpx
import pytest

import cutie_backtesting_provider as p

TOOL = 'local.backtesting_py.ema_cross'


def payload(run_id='health1'):
    return {'backtest': dict(run_id=run_id, provider_tool_id=TOOL,
        provider_params={'ema_fast': 5, 'ema_slow': 20}, symbol='BTCUSDT',
        market='spot', timeframe='1h', start_at=1767225600, end_at=1767315600,
        initial_capital='10000', fee_bps='10', slippage_bps='5')}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=p.app,
        raise_app_exceptions=False), base_url='http://provider')


def test_health_200_under_100ms_during_cpu_backtest(setup, monkeypatch):
    started, finished = threading.Event(), threading.Event()

    def heavy(*args, **kwargs):
        started.set()
        until = time.perf_counter() + 0.5
        while time.perf_counter() < until:
            sum(range(1000))
        finished.set()
        raise RuntimeError('controlled CPU failure')

    monkeypatch.setitem(p.TOOL_SPECS[TOOL], 'build', heavy)

    async def scenario():
        async with client() as c:
            assert (await c.get('/health')).status_code == 200  # warm dependency imports
            task = asyncio.create_task(c.post('/cutie/backtest', json=payload()))
            try:
                while not started.is_set():
                    await asyncio.sleep(0.001)
                assert not finished.is_set(), 'CPU work blocked the HTTP event loop'
                before = time.perf_counter()
                response = await c.get('/health')
                elapsed = time.perf_counter() - before
                assert response.status_code == 200
                assert response.json()['ok'] is True
                assert elapsed < 0.1, elapsed
                assert not finished.is_set(), 'health only answered after CPU work ended'
                print(f'HEALTH_LATENCY_MS={elapsed * 1000:.2f}')
            finally:
                await task

    asyncio.run(scenario())


def test_two_backtests_cpu_sections_are_serial(setup, monkeypatch):
    first_started, release_first = threading.Event(), threading.Event()
    events = []
    lock = threading.Lock()

    def heavy(params, **kwargs):
        with lock:
            number = 1 + sum(event[0] == 'start' for event in events)
            events.append(('start', number))
        if number == 1:
            first_started.set()
            assert release_first.wait(2), 'first CPU section was not released'
        with lock:
            events.append(('end', number))
        raise RuntimeError('controlled CPU failure')

    monkeypatch.setitem(p.TOOL_SPECS[TOOL], 'build', heavy)

    async def scenario():
        async with client() as c:
            first = asyncio.create_task(c.post('/cutie/backtest', json=payload('first')))
            second = None
            try:
                while not first_started.is_set():
                    await asyncio.sleep(0.001)
                second = asyncio.create_task(c.post('/cutie/backtest', json=payload('second')))
                await asyncio.sleep(0.1)
                assert events == [('start', 1)], events
            finally:
                release_first.set()
                await first
                if second:
                    await second
        assert events == [('start', 1), ('end', 1), ('start', 2), ('end', 2)]

    asyncio.run(scenario())


@pytest.mark.parametrize('error,message,status,error_type', [
    (ValueError, 'INVALID_PARAMS:bad period', 200, 'INVALID_PARAMS'),
    (RuntimeError, 'engine exploded', 200, 'ENGINE_ERROR'),
])
def test_cpu_exception_preserves_http_response(setup, monkeypatch, error, message, status, error_type):
    def fail(*args, **kwargs):
        raise error(message)
    monkeypatch.setitem(p.TOOL_SPECS[TOOL], 'build', fail)

    async def scenario():
        async with client() as c:
            response = await c.post('/cutie/backtest', json=payload())
        assert response.status_code == status
        body = response.json()
        assert body['error_type'] == error_type
        assert body['error_message'] == ('bad period' if error_type == 'INVALID_PARAMS'
                                        else 'Strategy build failed: engine exploded')
        # Store full body for byte-for-byte pre/post comparison outside the fixture.
        print('ERROR_BODY=' + json.dumps(body, sort_keys=True))
    asyncio.run(scenario())


def test_unhandled_cpu_exception_preserves_500_response(setup, monkeypatch):
    monkeypatch.setattr(p, 'is_strategy_paper_tick_intent',
        lambda body: (_ for _ in ()).throw(RuntimeError('unhandled CPU failure')))
    async def scenario():
        async with client() as c:
            response = await c.post('/cutie/backtest', json=payload())
        assert response.status_code == 500
        assert response.json() == dict(schema=p.RESPONSE_SCHEMA, result_status='failed',
            provider_name=p.PROVIDER_NAME, error_type='ENGINE_ERROR',
            error_message='unhandled CPU failure')
    asyncio.run(scenario())
