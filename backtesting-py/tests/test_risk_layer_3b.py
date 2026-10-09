"""3b contracts: strict parameters, closed-bar execution and actual integer fills."""
from decimal import Decimal as D
import pytest
from test_risk_layer import p, bars_frame, http_request

KEYS = ['trailing_stop_pct', 'breakeven_stop', 'max_holding_bars',
        *[f'tp{n}_{s}' for n in (1, 2, 3) for s in ('r', 'close_pct')]]
BASE = dict(risk_layer_enabled=True, stop_loss_pct=10)
LEVELS = dict(trailing_stop_pct=20, tp1_r=1, tp1_close_pct=30, tp2_r=2, tp2_close_pct=30,
              tp3_r=3, tp3_close_pct=40)

def valid(key, value):
    params = dict(BASE, **{key: value})
    if key.startswith('tp') and value:
        n = int(key[2])
        params.update(trailing_stop_pct=20)
        for i in range(1, n+1):
            params[f'tp{i}_r'] = i
            params[f'tp{i}_close_pct'] = 10
        params[key] = value
        if key.endswith('_r'):
            for i in range(1, n):
                params[f'tp{i}_r'] = value * i / n
    return params

@pytest.mark.parametrize('key', KEYS)
def test_parameter_defaults_normalize_away(key):
    default = False if key == 'breakeven_stop' else 0
    assert p._parse_fixed_risk_params({key: default}) == {}
    assert p._parse_fixed_risk_params(dict(BASE, **{key: default})) == p._parse_fixed_risk_params(BASE)

@pytest.mark.parametrize('key', KEYS)
@pytest.mark.parametrize('bad', [None, '1', float('nan'), float('inf'), -float('inf'), 10**400, [], {}])
def test_parameter_bad_types_and_nonfinite(key, bad):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(dict(BASE, **{key: bad}))

@pytest.mark.parametrize('key', [k for k in KEYS if k != 'breakeven_stop'])
@pytest.mark.parametrize('bad', [True, False, -1])
def test_numeric_bool_and_negative_rejected(key, bad):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(dict(BASE, **{key: bad}))

@pytest.mark.parametrize('key,value', [('trailing_stop_pct', 99.999), ('breakeven_stop', True),
    ('max_holding_bars', 1000000), *[(k,100) for k in KEYS if k.startswith('tp')]])
def test_parameter_upper_bound_accepted(key, value):
    assert p._validate_params_against_schema({key: value}, p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES) is None
    if key.endswith('_close_pct'):
        value -= 10 * (int(key[2]) - 1)
    assert p._parse_fixed_risk_params(valid(key, value))[key] == value

@pytest.mark.parametrize('key,value', [('trailing_stop_pct',100), ('trailing_stop_pct',101),
    ('breakeven_stop',1), ('breakeven_stop',0), ('max_holding_bars',1000001),
    ('max_holding_bars',1.0), ('max_holding_bars',0.0),
    *[(k,101) for k in KEYS if k.startswith('tp')]])
def test_parameter_upper_bounds_and_strict_integer(key, value):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(valid(key, value))

@pytest.mark.parametrize('key', KEYS)
def test_nondefault_requires_explicit_risk_gate(key):
    params = valid(key, True if key == 'breakeven_stop' else 1)
    params['risk_layer_enabled'] = False
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(params)

@pytest.mark.parametrize('extra', [dict(tp1_r=1), dict(tp1_close_pct=50),
    dict(tp2_r=2,tp2_close_pct=100), dict(tp1_r=2,tp1_close_pct=50,tp2_r=1,tp2_close_pct=50),
    dict(tp1_r=1,tp1_close_pct=60,tp2_r=2,tp2_close_pct=50),
    dict(tp1_r=1,tp1_close_pct=50),
    dict(tp1_r=1,tp1_close_pct=100,take_profit_pct=5),
    dict(tp1_r=1,tp1_close_pct=100,take_profit_r=2),
    dict(tp1_r=1,tp1_close_pct=50,tp3_r=3,tp3_close_pct=50)])
def test_cross_field_levels_rejected(extra):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(dict(BASE, **extra))

@pytest.mark.parametrize('extra', [dict(breakeven_stop=True), dict(take_profit_r=1),
                                  dict(tp1_r=1,tp1_close_pct=100)])
def test_r_based_features_require_initial_stop(extra):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(dict(risk_layer_enabled=True, **extra))
    assert p._parse_fixed_risk_params(dict(risk_layer_enabled=True, trailing_stop_pct=5, **extra))
