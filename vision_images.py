"""Sizing images for a vision model's context.

There is no cap on how many images a tool may return. Instead each batch shares a pixel budget, so more
images means each is smaller (never below a readable width), and the budget follows the model: its tier,
and a small fraction of its context window so a short-context model is not flooded. Pixels are a fair
proxy for what a vision encoder charges (roughly 750-800 pixels per token for the common ones).
"""
import base64
import io
import math

from PIL import Image

PIXEL_BUDGET = {"small": 450_000, "standard": 900_000, "large": 1_500_000}   # per batch, by model tier
_PIXELS_PER_TOKEN = 780
_CONTEXT_SHARE = 0.04              # a batch may cost at most about this share of the context window
MIN_WIDTH = 480                    # below this, text in a screenshot stops being legible
MAX_WIDTH = 1400
MIN_IMAGE_PIXELS = 150_000         # many images in one batch stop shrinking here; the batch grows instead of going unreadable
JPEG_QUALITY = 70


def batch_budget(tier="standard", ctx_size=None):
    budget = PIXEL_BUDGET.get(tier, PIXEL_BUDGET["standard"])
    if ctx_size:
        budget = min(budget, int(ctx_size * _CONTEXT_SHARE * _PIXELS_PER_TOKEN))
    return max(budget, MIN_IMAGE_PIXELS)


def _fit(data, budget):
    image = Image.open(io.BytesIO(data)).convert("RGB")
    width, height = image.size
    scale = min(1.0, math.sqrt(budget / (width * height)), MAX_WIDTH / width)
    new_width = max(round(width * scale), min(width, MIN_WIDTH))       # never narrower than legible (or than the original)
    new_width = min(new_width, width)
    if new_width != width:
        image = image.resize((new_width, max(1, round(height * new_width / width))), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


def fit_images(images_b64, tier="standard", ctx_size=None):
    """base64 images (PNG or JPEG) -> base64 JPEGs sized to share the model's budget."""
    if not images_b64:
        return []
    per_image = max(batch_budget(tier, ctx_size) / len(images_b64), MIN_IMAGE_PIXELS)
    return [base64.b64encode(_fit(base64.b64decode(b), per_image)).decode() for b in images_b64]
