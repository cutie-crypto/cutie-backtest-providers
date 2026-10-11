"""#46-P: v3 basket timeframes follow the server's 15m / 1w admission."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy_kernel import StrategySpecV3Error, build_frames_v3, compile_strategy_v3
from test_strategy_kernel_basket import roc_spec, _rows


@pytest.mark.parametrize('timeframe,step', [('15m', 900), ('1w', 604800)])
def test_v3_basket_accepts_timeframe_and_builds_contiguous_frames(timeframe, step):
    spec = roc_spec(window=1)
    spec['market']['timeframe'] = timeframe
    for feature in spec['features']:
        feature['interval'] = timeframe
    plan = compile_strategy_v3(spec)
    leg_rows = {'a': _rows(['100', '110', '121']), 'b': _rows(['1', '1', '1'])}
    for rows in leg_rows.values():
        for i, row in enumerate(rows):
            row['open_time'] = i*step
    result = build_frames_v3(leg_rows, plan)
    assert result.skipped_bars == 0
    assert [f.bar_open_at for f in result.frames] == [0, step, 2*step]
    assert [f.bar_close_at-f.bar_open_at for f in result.frames] == [step]*3
    assert all(b.bar_open_at-a.bar_open_at == step for a,b in zip(result.frames, result.frames[1:]))
    assert result.frames[1].values['ratio_roc'] == '0.1'


def test_v3_basket_rejects_5m_with_updated_timeframe_message():
    spec = roc_spec(window=1)
    spec['market']['timeframe'] = '5m'
    with pytest.raises(StrategySpecV3Error, match='must be 15m, 1h, 4h, 1d or 1w') as error:
        compile_strategy_v3(spec)
    assert error.value.path == '$.strategy_spec.market.timeframe'
