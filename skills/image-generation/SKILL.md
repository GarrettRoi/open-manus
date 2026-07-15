# Image Generation Skill (Cora)

## Overview
Cora uses OpenRouter to access AI image generation models. This skill provides access to multiple image generation models through a single unified API.

## API Configuration
- **Provider**: OpenRouter (`https://openrouter.ai/api/v1`)
- **API Key**: Set via `OPENROUTER_API_KEY` environment variable
- **Primary Model**: `black-forest-labs/flux-1.1-pro` (best quality)
- **Fast Model**: `black-forest-labs/flux-schnell` (faster, cheaper)
- **Alternative**: `stabilityai/stable-diffusion-3-5-large`

## Usage

### Generate an Image
```python
from skills.image_generation.image_gen import generate_image

# Basic usage
result = generate_image(
    prompt="A beautiful wedding venue with soft golden hour lighting, elegant floral arrangements",
    model="black-forest-labs/flux-1.1-pro",
    width=1024,
    height=1024
)
# Returns: {"url": "...", "local_path": "/workspace/images/..."}
```

### Brand-Specific Prompts
When generating for specific brands, use these style guides:

**Vows & Vinyl DJ Co.**
- Style: Warm, romantic, candid wedding photography aesthetic
- Colors: Gold, ivory, deep burgundy
- Elements: DJ equipment, dance floors, happy couples, string lights
- Prompt suffix: `"romantic wedding atmosphere, warm golden lighting, elegant and joyful"`

**Cana Collective**
- Style: Clean, professional, Catholic wedding aesthetic  
- Colors: White, gold, sage green
- Elements: Church venues, vendor showcases, wedding planning
- Prompt suffix: `"Catholic wedding aesthetic, clean professional style, warm and welcoming"`

**McGarry Homes**
- Style: Professional real estate photography
- Colors: Navy blue, white, warm neutrals
- Elements: Home exteriors, interiors, Oklahoma City neighborhoods
- Prompt suffix: `"professional real estate photography, bright and inviting, Oklahoma City"`

## Environment Variables Required
```
OPENROUTER_API_KEY=<from secrets>
IMAGE_OUTPUT_DIR=/workspace/images
```

## Notes
- Images are saved to `/workspace/images/` on the agent's volume
- Always include brand context in prompts for consistency
- For social media: 1:1 ratio (1024x1024) for Instagram, 16:9 (1792x1024) for Facebook/Twitter
- For website banners: 16:9 or 21:9 ratio
