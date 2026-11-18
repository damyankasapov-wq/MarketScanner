"""
Regression tests for issues /qa found running the settings sweep on real data
(2024-01-01 to 2026-11-13, SPY/QQQ/GLD). Report: .gstack/qa-reports/.
"""
from datetime import date

import pytest

from marketscanner.backtest import sweep_report
from marketscanner.backtest.engine import AccountConfig
from marketscanner.backtest.strategy import OrbSettings
from marketscanner.backtest.sweep import HalfStats, Row, SweepResult

CFG = AccountConfig(initial_cash=10000.0, per_deal=1000.0, min_cash=2000.0)


# Regression: ISSUE-002 - `--candle 7` said "CONFIRM_CANDLE_MINUTES must divide 60",
# naming an env var the user never typed.
# Found by /qa on 2026-11-18
def test_bad_candle_names_the_setting_not_the_env_var():
    with pytest.raises(ValueError) as exc:
        OrbSettings(7)
    assert "candle minutes must divide 60" in str(exc.value)
    assert "CONFIRM_CANDLE_MINUTES" not in str(exc.value)


def _result(rows):
    return SweepResult(rows=rows, baseline=OrbSettings(5), is_dates=[], oos_dates=[], sessions=[],
                       symbols=["SPY"], start=date(2024, 1, 1), end=date(2026, 11, 13), cfg=CFG)


# Regression: ISSUE-003 - sweep.txt rows drifted right whenever a dR +/- 2SE cell was wider
# than 15 characters (e.g. "+37.80 +/- 109.37"): cells were padded with the 1-character '±'
# and only then translated to the 3-character '+/-'.
# Found by /qa on 2026-11-18
def test_text_table_columns_stay_aligned_with_wide_cells():
    wide = Row(settings=OrbSettings(15, 2.0), is_baseline=False,
               is_=HalfStats(closed=1076, avg_r=0.06, se_r=0.055), oos=HalfStats(closed=498, avg_r=-0.18, se_r=0.085),
               d_is=37.80, se_d_is=54.685, d_oos=-111.24, se_d_oos=37.185, rank_is=1)
    narrow = Row(settings=OrbSettings(5), is_baseline=True,
                 is_=HalfStats(closed=1237, avg_r=0.02, se_r=0.04), oos=HalfStats(closed=571, avg_r=0.04, se_r=0.065),
                 d_is=0.0, se_d_is=0.0, d_oos=0.0, se_d_oos=0.0, rank_is=2)
    result = _result([wide, narrow])
    lines = sweep_report.table_lines(result, [wide, narrow])
    head = lines[0]
    assert head.isascii()
    k = len(head) - len("mark")
    for line in lines[2:]:
        assert line.isascii()
        assert "+37.80 +/- 109.37" in line or "+0.00 +/- 0.00" in line
        assert line[k - 2:k] == "  " and line[k - 3] != " ", line    # DD% ends where the header says
    text = sweep_report.format_text(result)
    assert "+37.80 +/- 109.37" in text and "-111.24 +/- 74.37" in text


# Regression: ISSUE-001 - a second sweep of the same dates with a different grid wrote into the
# same <run>_sweep folder and silently replaced the first sweep's report.
# Found by /qa on 2026-11-18
def test_grid_tag_gives_each_grid_its_own_folder():
    from datetime import time

    from marketscanner.backtest.sweep import Grid

    assert Grid().tag() == ""
    assert Grid((5, 15), (1.0, 2.0), (None, time(14, 0))).tag() == "c5-15_r1-2_xnone-1400"
    assert Grid((10,), (1.5,), (None,)).tag() == "c10_r1.5_xnone"
    assert Grid((5, 15), (1.0, 2.0), (None,)).tag() != Grid((5, 15), (1.0, 2.0), (None, time(14, 0))).tag()
