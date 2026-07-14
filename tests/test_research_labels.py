from __future__ import annotations

from trade_assistant.research_labels import label_triple_barrier


def test_triple_barrier_labels_long_target_and_excursions() -> None:
    label = label_triple_barrier(
        side="long", entry=100, stop=95, target=110,
        future_bars=[(104, 99, 103), (111, 102, 109)], max_bars=3,
    )

    assert label.outcome == "target"
    assert label.return_r == 2.0
    assert label.mfe_r == 2.2
    assert label.mae_r == -0.2
    assert label.bars_held == 2


def test_triple_barrier_labels_short_stop() -> None:
    label = label_triple_barrier(
        side="short", entry=100, stop=105, target=90,
        future_bars=[(106, 98, 104)], max_bars=2,
    )

    assert label.outcome == "stop"
    assert label.return_r == -1.0


def test_triple_barrier_uses_time_barrier_when_no_price_barrier_is_hit() -> None:
    label = label_triple_barrier(
        side="long", entry=100, stop=95, target=110,
        future_bars=[(103, 99, 102), (105, 100, 104)], max_bars=2,
    )

    assert label.outcome == "time"
    assert label.return_r == 0.8
    assert label.bars_held == 2


def test_triple_barrier_resolves_same_candle_collision_to_stop() -> None:
    label = label_triple_barrier(
        side="long", entry=100, stop=95, target=110,
        future_bars=[(111, 94, 100)], max_bars=1,
    )

    assert label.outcome == "stop"
    assert label.return_r == -1.0
