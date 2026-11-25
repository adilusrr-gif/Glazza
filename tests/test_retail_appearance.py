from io import BytesIO

from PIL import Image, ImageDraw
import pytest

from app.retail_appearance import MAX_IMAGE_BYTES, clothing_signature, compare_clothing, live_crop_box


def encoded(image, fmt='JPEG'):
    data = BytesIO()
    image.save(data, format=fmt)
    return data.getvalue()


def shopper(upper, lower, *, offset=0, background=(125, 120, 105)):
    # A padded Frigate-like crop. Only the body bands should influence the result.
    image = Image.new('RGB', (160, 160), background)
    draw = ImageDraw.Draw(image)
    draw.ellipse((68+offset, 6, 92+offset, 35), fill='#b7825d')
    draw.rectangle((55+offset, 37, 105+offset, 83), fill=upper)
    draw.rectangle((63+offset, 84, 97+offset, 144), fill=lower)
    return image


def test_similar_apparel_preserves_only_color_distributions_across_cameras():
    a = clothing_signature(encoded(shopper((210, 42, 36), (25, 50, 124))))
    b = clothing_signature(encoded(shopper((166, 32, 26), (35, 53, 111), offset=2, background='#8a887d')))
    assert len(a) == len(b) == 22
    assert sum(a[:11]) == sum(a[11:]) == 100
    assert compare_clothing(a, b) == compare_clothing(b, a) == 'compatible'


def test_distinct_or_swapped_garments_are_incompatible_even_on_same_background():
    a = clothing_signature(encoded(shopper('red', 'blue')))
    for image in (shopper('green', 'blue'), shopper('blue', 'red')):
        assert compare_clothing(a, clothing_signature(encoded(image))) == 'incompatible'


def test_monochrome_uniform_and_background_ambiguity_abstain():
    gray = clothing_signature(encoded(shopper('#333', '#bbb')))
    assert gray is None and compare_clothing(gray, gray) == 'unknown'
    assert clothing_signature(encoded(Image.new('RGB', (160, 160), 'green'))) is None
    shelves = Image.new('RGB', (160, 160), 'red')
    ImageDraw.Draw(shelves).rectangle((0, 84, 160, 160), fill='blue')
    assert clothing_signature(encoded(shelves)) is None


def test_known_snapshot_box_crops_full_scene_using_width_and_height():
    photo = shopper('red', 'blue')
    scene = Image.new('RGB', (640, 320), 'lime')
    scene.paste(photo, (218, 80))
    raw = encoded(scene, 'WEBP')
    assert clothing_signature(raw) is None  # Wide image was not already cropped.
    sig = clothing_signature(raw, box=[(218+54)/640, (80+5)/320, 52/640, 141/320])
    other = clothing_signature(encoded(photo))
    assert compare_clothing(sig, other) == 'compatible'


@pytest.mark.parametrize('box', [
    [0, .1, .01, .1], [.1, 0, .3, .8], [.1, .2, .3, .8], [.9, .1, .4, .5],
    [False, .2, .3, .3], [float('nan'), .2, .3, .3], [.4, .2, .3], 'bad',
])
def test_bad_truncated_or_distant_person_box_abstains(box):
    data = encoded(shopper('red', 'blue'))
    assert clothing_signature(data, box=box) is None


@pytest.mark.parametrize('data', [b'', b'garbage', b'x'*(MAX_IMAGE_BYTES+1)])
def test_bad_snapshot_abstains(data):
    assert clothing_signature(data) is None


def test_unsupported_format_and_geometry_abstain():
    assert clothing_signature(encoded(shopper('red', 'blue'), 'PNG')) is None
    assert clothing_signature(encoded(Image.new('RGB', (16, 32)))) is None


@pytest.mark.parametrize('other', [None, [100, 0], [False]*22, [0]*22, [0]*21+[101]])
def test_bad_stored_signature_cannot_match(other):
    sig = clothing_signature(encoded(shopper('red', 'blue')))
    assert compare_clothing(sig, other) == 'unknown'


def test_intermediate_similarity_has_its_own_abstention():
    first = [100]+[0]*10 + [0]*8+[100,0,0]
    second = [55,45]+[0]*9 + [0]*8+[100,0,0]
    assert compare_clothing(first, second) == 'unknown'


def test_live_snapshot_clamped_right_hand_crop_targets_the_garments():
    # Frigate crops [340:640, 0:300] for this off-centre person on a 640x360 camera.
    # Much of the requested square is background; its centre is outside the person.
    scene = Image.new('RGB', (640, 360), '#aca79d')
    draw = ImageDraw.Draw(scene)
    draw.rectangle((573, 112, 608, 140), fill='red')
    draw.rectangle((573, 141, 608, 178), fill='blue')
    box = [573/640, 90/360, 36/640, 96/360]
    local_box = live_crop_box(box, 640, 360)
    assert local_box == pytest.approx([233/300, 90/300, 36/300, 96/300])
    jpeg = encoded(scene.crop((340, 0, 640, 300)).resize((320, 320), Image.Resampling.BOX))
    assert clothing_signature(jpeg) is None
    live = clothing_signature(jpeg, box=local_box)
    complete = clothing_signature(encoded(scene, 'WEBP'), box=box)
    assert compare_clothing(live, complete) == 'compatible'


def test_live_snapshot_uses_expanded_scale_and_clips_region_to_small_frame():
    box = [50/1280, 100/720, 150/1280, 450/720]
    # Long side is 450; Frigate rounds 1.1 * 450 down to a multiple of 4 (492).
    assert live_crop_box(box, 1280, 720) == pytest.approx([50/492, 21/492, 150/492, 450/492])
    small = [80/240, 20/180, 50/240, 130/180]
    assert live_crop_box(small, 240, 180) == pytest.approx(small)


@pytest.mark.parametrize(('box', 'w', 'h'), [
    ([.1, .1, .04, .08], 640, 360),  # A 28px-high person stays unusable after resizing to 320px.
    ([.1, .1, .2, .5], None, 360), ([.1, .1, .2, .5], 640, 0),
    ([.1, .1, .2, .5], True, True), (None, 640, 360),
])
def test_live_snapshot_without_usable_source_geometry_abstains(box, w, h):
    assert live_crop_box(box, w, h) is None
