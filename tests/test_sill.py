from app.sill import _depth_over_crest


def test_depth_clamps_at_zero():
    assert _depth_over_crest(0.3, 0.5) == 0.0   # sill exposed
    assert _depth_over_crest(0.5, 0.5) == 0.0


def test_depth_rounds_to_tenth():
    assert _depth_over_crest(2.34, 0.5) == 1.8
    assert _depth_over_crest(2.36, 0.5) == 1.9
