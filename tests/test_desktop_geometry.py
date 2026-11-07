from canaan_desktop_geometry import place, geometry


def test_work_area_clamp_and_snap():
    areas = [(0, 0, 1920, 1040)]
    assert place(1900, 1100, 280, 300, areas)[:2] == (1640, 740)
    assert place(-20, -15, 280, 300, areas)[:2] == (0, 0)
    assert place(1633, 733, 280, 300, areas, snap=10)[:2] == (1640, 740)


def test_secondary_monitor_and_unplug_recovery():
    areas = [(-1280, 0, 0, 984), (0, 0, 1920, 1040)]
    x, y, area = place(-1200, 900, 280, 300, areas)
    assert (x, y, area) == (-1200, 684, areas[0])
    assert place(x, y, 280, 300, areas[1:])[:2] == (0, 684)
    assert geometry(280, 300, x, y) == "280x300+-1200+684"


def test_small_work_area_never_produces_negative_primary_position():
    assert place(1600, 820, 280, 300, [(0, 0, 240, 240)])[:2] == (0, 0)
