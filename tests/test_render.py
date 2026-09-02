import numpy as np

from ledwall.config import Config
from ledwall.palettes import build_lut, names
from ledwall.render import Renderer, gamma_lut, rgb_to_rgbw
from ledwall.sources import fit_to_grid, soft_threshold


def cfg(**over):
    base = {
        "grid": {"width": 8, "height": 4},
        "output": {"rgbw": True, "brightness": 1.0, "gamma": 1.0},
        "render": {"palette": "white", "cycle_seconds": 1e9, "background": "off"},
        "wiring": {"controllers": [{"name": "a", "host": "h", "lines": [0, 4]}]},
    }
    for k, v in over.items():
        base.setdefault(k, {}).update(v)
    return Config.from_dict(base)


def test_gamma_lut_endpoints_and_monotonic():
    lut = gamma_lut(2.2)
    assert lut[0] == 0 and lut[255] == 255
    assert np.all(np.diff(lut.astype(int)) >= 0)
    # Gamma 2.2 pulls midtones down.
    assert lut[128] < 128


def test_rgbw_accurate_preserves_hue_and_moves_white_to_w():
    rgb = np.array([[[0.5, 0.5, 0.5]]], dtype=np.float32)
    out = rgb_to_rgbw(rgb, "accurate")
    # Pure grey becomes pure white channel, no coloured dies lit.
    assert np.allclose(out[..., :3], 0.0)
    assert np.isclose(out[..., 3], 0.5)

    rgb = np.array([[[1.0, 0.4, 0.2]]], dtype=np.float32)
    out = rgb_to_rgbw(rgb, "accurate")
    assert np.isclose(out[..., 3], 0.2)
    assert np.allclose(out[..., :3], [0.8, 0.2, 0.0])


def test_rgbw_modes_none_and_min():
    rgb = np.array([[[0.5, 0.5, 0.5]]], dtype=np.float32)
    assert np.isclose(rgb_to_rgbw(rgb, "none")[..., 3], 0.0)
    out = rgb_to_rgbw(rgb, "min")
    assert np.isclose(out[..., 3], 0.5)
    assert np.allclose(out[..., :3], 0.5)  # min mode leaves RGB alone


def test_renderer_output_shape_and_blank():
    r = Renderer(cfg())
    mask = np.zeros((4, 8), dtype=np.float32)
    out = r.render(mask, 0.0)
    assert out.shape == (32, 4) and out.dtype == np.uint8
    assert not out.any()  # nothing lit with an empty mask and background off
    assert r.blank().shape == (32, 4)


def test_renderer_lights_only_the_mask():
    r = Renderer(cfg())
    mask = np.zeros((4, 8), dtype=np.float32)
    mask[1, 3] = 1.0
    out = r.render(mask, 0.0).reshape(4, 8, 4)
    assert out[1, 3].sum() > 0
    assert out[0, 0].sum() == 0


def test_rgb_mode_gives_three_channels():
    r = Renderer(cfg(output={"rgbw": False, "brightness": 1.0, "gamma": 1.0}))
    assert r.render(np.ones((4, 8), dtype=np.float32), 0.0).shape == (32, 3)


def test_brightness_scales_output():
    dim = Renderer(cfg(output={"rgbw": True, "brightness": 0.25, "gamma": 1.0}))
    full = Renderer(cfg(output={"rgbw": True, "brightness": 1.0, "gamma": 1.0}))
    mask = np.ones((4, 8), dtype=np.float32)
    assert dim.render(mask, 0.0).max() < full.render(mask, 0.0).max()


def test_all_palettes_build():
    for n in names():
        lut = build_lut(n)
        assert lut.shape == (256, 3) and lut.dtype == np.uint8


def test_fit_to_grid_cover_crops_rather_than_squashing():
    # 4:3 source into a 2:1 grid should crop top/bottom, not squash.
    src = np.zeros((60, 80), dtype=np.float32)
    src[0:5, :] = 1.0  # a band at the very top, which cover should crop away
    out = fit_to_grid(src, 100, 50, "cover")
    assert out.shape == (50, 100)
    assert out[0].max() < 0.5


def test_fit_to_grid_stretch_and_contain_shapes():
    src = np.ones((60, 80), dtype=np.float32)
    assert fit_to_grid(src, 100, 50, "stretch").shape == (50, 100)
    out = fit_to_grid(src, 100, 50, "contain")
    assert out.shape == (50, 100)
    # A 4:3 source in a 2:1 grid is height-limited, so contain pillarboxes:
    # full height, bars down the left and right edges.
    assert out[:, 0].max() == 0.0
    assert out[:, 50].max() == 1.0


def test_soft_threshold_edges():
    m = np.array([0.0, 0.42, 0.5, 0.58, 1.0], dtype=np.float32)
    out = soft_threshold(m, 0.5, softness=0.15)
    assert out[0] == 0.0 and out[-1] == 1.0
    assert np.isclose(out[2], 0.5)
    # Values inside the soft band ramp rather than snapping to 0/1.
    assert 0.0 < out[1] < 0.5 < out[3] < 1.0
    # Outside the band it is a hard decision.
    assert soft_threshold(np.array([0.34], dtype=np.float32), 0.5, 0.15)[0] == 0.0
    assert soft_threshold(np.array([0.66], dtype=np.float32), 0.5, 0.15)[0] == 1.0


def test_fit_to_grid_respects_non_square_pixels():
    from ledwall.config import Grid

    # 60/m strips (16.67mm) on 24.4mm row centres: pixels are taller than wide.
    g = Grid(width=100, height=50, pixel_aspect=16.67 / 24.4)
    assert abs(g.display_aspect - 1.366) < 0.01

    # A 4:3 source into that wall crops much less than into a true 2:1 wall.
    src = np.zeros((60, 80), dtype=np.float32)
    src[0:6, :] = 1.0
    square = fit_to_grid(src, 100, 50, "cover", 2.0)
    wide_pixels = fit_to_grid(src, 100, 50, "cover", g.display_aspect)
    assert square[0].max() < wide_pixels[0].max()


def test_fit_to_grid_contain_is_bounded_and_centred():
    src = np.ones((60, 80), dtype=np.float32)
    for aspect in (2.0, 1.366, 0.8):
        out = fit_to_grid(src, 100, 50, "contain", aspect)
        assert out.shape == (50, 100)
        assert out.max() == 1.0
