"""
Smoke tests for render_chart(snapshot, market): every snapshot shape renders
to a non-empty PNG without raising, using only the OO matplotlib API.
"""
import io

import pandas as pd
import pytest

from marketscanner.strategies import orb_session
from marketscanner.ui.chart import render_chart
from tests.orb_fixtures import C_GREEN_ABOVE, C_RED_BELOW, ORB_BARS, TRAIL, bar, df_of, flat, utc

NOW = utc(2024, 11, 6, 15, 36)


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=80)
    data = buf.getvalue()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) > 1_000
    return data


def _full_orb():
    """A full first hour of 1-minute bars (12 complete candles) around 574.5-580."""
    bars = flat("2024-11-06 14:30", 60, 577.0)
    bars[0] = bar("2024-11-06 14:30:00+00:00", 575.0, 578.0, 574.5, 577.0)
    bars[59] = bar("2024-11-06 15:29:00+00:00", 578.0, 580.0, 577.0, 579.0)
    return bars


class TestRenderChart:
    def test_empty_snapshot(self):
        _png(render_chart(orb_session.snapshot(pd.DataFrame(), 5), "SPY"))

    def test_no_box_only_premarket_bar(self):
        snap = orb_session.snapshot(bar("2024-11-06 13:00:00+00:00", 570, 571, 569, 570.5), 5)
        assert snap.box is None
        _png(render_chart(snap, "SPY"))

    def test_box_only(self):
        snap = orb_session.snapshot(df_of(_full_orb() + flat("2024-11-06 15:30", 1)), 5)
        assert snap.box is not None and snap.proposal is None
        assert len(snap.candles) == 12
        _png(render_chart(snap, "SPY"))

    def test_proposal_open(self):
        snap = orb_session.snapshot(df_of(_full_orb() + C_GREEN_ABOVE + TRAIL), 5, now=NOW)
        assert snap.proposal is not None and snap.outcome.status == "OPEN"
        _png(render_chart(snap, "SPY"))

    def test_tp_hit(self):
        after = flat("2024-11-06 15:35", 10, 582.0)
        after[7] = bar("2024-11-06 15:42:00+00:00", 586.0, 587.0, 585.9, 586.5)
        snap = orb_session.snapshot(df_of(_full_orb() + C_GREEN_ABOVE + after + flat("2024-11-06 15:45", 1, 586.0)), 5)
        assert snap.outcome.status == "TP_HIT"
        assert pd.Timestamp(snap.outcome.exit_candle) in snap.candles.index
        _png(render_chart(snap, "SPY"))

    def test_sl_hit_sell(self):
        after = flat("2024-11-06 15:35", 10, 572.0)
        after[3] = bar("2024-11-06 15:38:00+00:00", 578.0, 579.0, 577.5, 578.5)
        snap = orb_session.snapshot(df_of(_full_orb() + C_RED_BELOW + after + flat("2024-11-06 15:45", 1, 572.0)), 5)
        assert snap.proposal.side == "SELL" and snap.outcome.status == "SL_HIT"
        _png(render_chart(snap, "SPY"))

    def test_exit_in_incomplete_bucket(self):
        # the touch sits in the newest (dropped) bucket → marker past the last candle
        after = flat("2024-11-06 15:35", 5, 582.0) + [
            bar("2024-11-06 15:40:00+00:00", 586.0, 587.0, 585.9, 586.5)]
        snap = orb_session.snapshot(df_of(_full_orb() + C_GREEN_ABOVE + after), 5)
        assert snap.outcome.status == "TP_HIT"
        assert pd.Timestamp(snap.outcome.exit_candle) not in snap.candles.index
        _png(render_chart(snap, "SPY"))

    def test_title_states_candle_size(self):
        snap = orb_session.snapshot(df_of(_full_orb() + C_GREEN_ABOVE + TRAIL), 5, now=NOW)
        fig = render_chart(snap, "QQQ")
        assert "QQQ" in fig._suptitle.get_text()
        assert "5-min" in fig._suptitle.get_text()
        assert "2024-11-06" in fig._suptitle.get_text()

    def test_no_pyplot_figure_registered(self):
        import matplotlib.pyplot as plt
        before = plt.get_fignums()
        render_chart(orb_session.snapshot(df_of(_full_orb() + C_GREEN_ABOVE + TRAIL), 5, now=NOW), "SPY")
        assert plt.get_fignums() == before
