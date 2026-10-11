"""The text variant of the exit-kinds strip removes exactly raw_report.trade_exit_kinds and nothing else."""
from _exit_kinds_strip import without_exit_kinds, without_exit_kinds_text

OLD = '{"result_status":"success","raw_report":{"turtle_groups":[{"group_id":"g1","n":"1"}],"x":"中"},"trades":[]}'


def test_top_level_key_is_removed_and_other_bytes_are_identical():
    new = ('{"result_status":"success","raw_report":{"turtle_groups":[{"group_id":"g1","n":"1"}],'
           '"trade_exit_kinds":[{"seq":1,"exit_kind":"signal_exit"}],"x":"中"},"trades":[]}')
    assert without_exit_kinds_text(new) == OLD
    assert without_exit_kinds_text(OLD) == OLD


def test_same_named_key_at_a_nested_position_is_not_stripped():
    nested = OLD.replace('"n":"1"}', '"n":"1","trade_exit_kinds":[{"seq":1,"exit_kind":"signal_exit"}]}')
    assert without_exit_kinds_text(nested) != OLD
    other_path = OLD.replace('"trades":[]', '"trades":[],"trade_exit_kinds":[]')
    assert without_exit_kinds_text(other_path) != OLD


def test_dict_variant_only_touches_raw_report_key():
    body = {'raw_report': {'a': 1, 'trade_exit_kinds': [1], 'g': [{'trade_exit_kinds': [2]}]}, 'trade_exit_kinds': [3]}
    assert without_exit_kinds(body) == {'raw_report': {'a': 1, 'g': [{'trade_exit_kinds': [2]}]},
                                        'trade_exit_kinds': [3]}
    assert body['raw_report']['trade_exit_kinds'] == [1]  # input is not mutated
