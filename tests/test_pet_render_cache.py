from collections import OrderedDict
from types import SimpleNamespace

from PIL import Image

import canaan_companion_pet as pet


def test_avatar_reuses_frames_and_bounds_memory(monkeypatch):
    created = []
    monkeypatch.setattr(pet.ImageTk, "PhotoImage", lambda image: created.append(image.size) or object())
    avatar = SimpleNamespace(_render_cache=OrderedDict(), facing_left=False, CANVAS_W=280, CANVAS_H=300)
    image = Image.new("RGBA", (100, 120))
    render = lambda width, angle=0: pet.CanaanPetCore._render_avatar(avatar, image, "idle", width, 120, angle)
    first = render(100, .10)
    for _ in range(100):
        assert render(100, .12) is first
    assert len(created) == 1
    avatar.facing_left = True
    assert render(100) is not first
    for width in range(101, 220):
        render(width)
    assert len(avatar._render_cache) == 96
    big = Image.new("RGBA", (400, 400))
    _, size = pet.CanaanPetCore._render_avatar(avatar, big, "large", 400, 400, 25)
    assert size[0] <= 272 and size[1] <= 260
