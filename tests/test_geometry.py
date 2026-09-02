import numpy as np
import pytest

from ledwall.config import Config, ConfigError
from ledwall.geometry import build_map, wled_bus_table


def make_cfg(**wiring):
    base = {
        "grid": {"width": 8, "height": 4},
        "wiring": {
            "controllers": [{"name": "a", "host": "10.0.0.1", "lines": [0, 4], "outputs": 1}],
            **wiring,
        },
    }
    return Config.from_dict(base)


def test_serpentine_rows_top_left():
    wall = build_map(make_cfg())
    take = wall.controllers[0].take
    # Run 0 travels left->right along grid row 0.
    assert list(take[:8]) == list(range(0, 8))
    # Run 1 doubles back right->left along row 1.
    assert list(take[8:16]) == list(range(15, 7, -1))
    assert wall.total_leds == 32


def test_non_serpentine_all_runs_same_direction():
    wall = build_map(make_cfg(serpentine=False))
    take = wall.controllers[0].take
    assert list(take[:8]) == list(range(0, 8))
    assert list(take[8:16]) == list(range(8, 16))


def test_start_corner_bottom_right():
    wall = build_map(make_cfg(start_corner="bottom-right"))
    take = wall.controllers[0].take
    # Run 0 is the bottom grid row (y=3), travelling right->left.
    assert list(take[:8]) == list(range(31, 23, -1))


def test_columns_orientation():
    cfg = Config.from_dict(
        {
            "grid": {"width": 4, "height": 8},
            "wiring": {
                "orientation": "columns",
                "controllers": [{"name": "a", "host": "10.0.0.1", "lines": [0, 4]}],
            },
        }
    )
    wall = build_map(cfg)
    take = wall.controllers[0].take
    # First run is grid column 0, top to bottom: flat indices 0,4,8,...
    assert list(take[:8]) == [0, 4, 8, 12, 16, 20, 24, 28]


def test_serpentine_scope_output_resets_each_output():
    cfg = Config.from_dict(
        {
            "grid": {"width": 8, "height": 4},
            "wiring": {
                "serpentine_scope": "output",
                "controllers": [{"name": "a", "host": "10.0.0.1", "lines": [0, 4], "outputs": 2}],
            },
        }
    )
    wall = build_map(cfg)
    take = wall.controllers[0].take
    # Output 1 starts at run 2 and restarts the serpentine phase, so run 2
    # travels left->right again rather than continuing the alternation.
    assert list(take[16:24]) == list(range(16, 24))


def test_serpentine_scope_wall_alternates_continuously():
    cfg = Config.from_dict(
        {
            "grid": {"width": 8, "height": 4},
            "wiring": {
                "serpentine_scope": "wall",
                "controllers": [{"name": "a", "host": "10.0.0.1", "lines": [0, 4], "outputs": 2}],
            },
        }
    )
    wall = build_map(cfg)
    take = wall.controllers[0].take
    assert list(take[16:24]) == list(range(16, 24))  # run 2 is even -> ltr
    assert list(take[24:32]) == list(range(31, 23, -1))


def test_every_pixel_driven_exactly_once_on_real_wall():
    cfg = Config.load("config/wall-50x100.yaml")
    wall = build_map(cfg)
    assert wall.total_leds == 5000
    assert np.array_equal(wall.coverage(), np.ones((50, 100), dtype=np.int32))
    # take is a permutation of every grid index
    allidx = np.sort(np.concatenate([c.take for c in wall.controllers]))
    assert np.array_equal(allidx, np.arange(5000))


def test_uneven_output_split_and_bus_table():
    cfg = Config.load("config/wall-50x100.yaml")
    wall = build_map(cfg)
    a = wall.controllers[0]
    assert [o.line_count for o in a.outputs] == [7, 6, 6, 6]
    assert [o.led_count for o in a.outputs] == [700, 600, 600, 600]
    assert [o.led_start for o in a.outputs] == [0, 700, 1300, 1900]
    assert a.led_count == 2500
    rows = wled_bus_table(wall)
    assert len(rows) == 8
    assert rows[0]["start"] == 0 and rows[0]["count"] == 700


def test_gap_in_coverage_is_rejected():
    with pytest.raises(ConfigError, match="unassigned"):
        Config.from_dict(
            {
                "grid": {"width": 8, "height": 4},
                "wiring": {"controllers": [{"name": "a", "host": "h", "lines": [0, 2]}]},
            }
        )


def test_overlapping_controllers_rejected():
    with pytest.raises(ConfigError, match="claimed by both"):
        Config.from_dict(
            {
                "grid": {"width": 8, "height": 4},
                "wiring": {
                    "controllers": [
                        {"name": "a", "host": "h", "lines": [0, 3]},
                        {"name": "b", "host": "h2", "lines": [2, 4]},
                    ]
                },
            }
        )


def test_bad_output_split_rejected():
    with pytest.raises(ConfigError, match="sum to"):
        Config.from_dict(
            {
                "grid": {"width": 8, "height": 4},
                "wiring": {
                    "controllers": [{"name": "a", "host": "h", "lines": [0, 4], "outputs": [1, 1]}]
                },
            }
        )


def test_unknown_key_is_reported():
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict({"grid": {"width": 8, "height": 4, "depth": 2},
                          "wiring": {"controllers": [{"name": "a", "host": "h", "lines": [0, 4]}]}})
