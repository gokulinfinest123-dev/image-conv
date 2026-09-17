"""Prompt library for image edits and copy rewrites."""

# Prompts are ordered by safety for e-commerce use. Index 0 (front-facing,
# clean white background) is the default when only one image is generated —
# it's the shot customers expect on a product listing and the least likely
# to be misread by the model. Later prompts add angle variety when N > 1.
VARIATION_PROMPTS = [
    (
        "Produce a professional e-commerce photograph of the SAME product "
        "shown. Do not alter brand, logo, colors, shape, or any printed text. "
        "Change ONLY: camera angle (front-facing straight on), lighting "
        "(bright even softbox), background (pure white infinity cove), and "
        "eliminate hard shadows. Must be indistinguishable from a real "
        "catalog photograph of this exact SKU. No watermark, no logo, no "
        "text overlay, no border, no signature — no elements not present "
        "on the physical product itself."
    ),
    (
        "This is a product catalog image. Create another authentic professional "
        "studio photograph of the EXACT SAME product. Preserve the product's "
        "identity, branding, logo, colors, shape, material, packaging, and "
        "labels with absolute fidelity. Change ONLY: camera angle (three-"
        "quarter view), lighting (softer key light from the left), shadows, "
        "and a clean neutral seamless background. Output must look like a "
        "real studio shot of the same physical product — no drawing, no "
        "illustration, no stylization, no added text or watermarks."
    ),
    (
        "Reshoot this exact product from a different perspective. Keep every "
        "visible detail — brand, logo, model, colors, materials, dimensions, "
        "packaging, labels — pixel-faithful to the original. Change ONLY: "
        "perspective (slight top-down), lighting (warm diffused overhead), "
        "surface reflections, depth of field (shallow focus on the label), "
        "and a subtle contextual surface (light wood or matte white). "
        "Photorealistic studio quality only, no added text or watermarks."
    ),
    (
        "Create another realistic product photo of this exact item. Preserve "
        "all identifying features — brand marks, colors, textures, form. "
        "Change ONLY: angle (side profile), a moody low-key lighting setup "
        "with a single rim light, dark gradient background, and glossy "
        "reflection on the surface below. Photograph must remain a "
        "faithful representation of the actual product, no added text "
        "or watermarks."
    ),
]


REWRITE_SYSTEM = (
    "You rewrite e-commerce product copy. You preserve every factual claim "
    "(brand, model, materials, dimensions, capacity, quantity, compatibility, "
    "certifications) with 100% fidelity. You never invent specs. You produce "
    "fresh wording — different sentence structure, different word choices, "
    "different order — so the result would not match the original under "
    "text-similarity checks, while conveying the same meaning to a shopper."
)


def rewrite_user_prompt(title: str, description: str) -> str:
    return (
        "Rewrite the product title and description below.\n\n"
        "Return valid JSON with exactly two keys: \"title\" and "
        "\"description\". No prose, no markdown, no code fences.\n\n"
        f"ORIGINAL TITLE:\n{title}\n\n"
        f"ORIGINAL DESCRIPTION:\n{description}\n"
    )


GENERATE_SYSTEM = (
    "You write e-commerce product titles and descriptions from a source title "
    "and brand name alone — no original description is given. You extract "
    "every factual detail already present in the source title (model numbers, "
    "features, specs, color, capacity, compatibility) and preserve them with "
    "100% fidelity. You never invent specs that are not implied by the given "
    "title or brand. You produce a polished catalog-ready title and a "
    "persuasive 2-4 sentence description, written in fresh wording that would "
    "not match the source title under text-similarity checks."
)


def generate_user_prompt(title: str, brand: str) -> str:
    return (
        "Write a new product title and a new product description for the "
        "listing below. No original description is available — write the "
        "description entirely from the facts stated in the title and brand.\n\n"
        "Return valid JSON with exactly two keys: \"title\" and "
        "\"description\". No prose, no markdown, no code fences.\n\n"
        f"SOURCE TITLE:\n{title}\n\n"
        f"BRAND:\n{brand or 'unknown'}\n"
    )
