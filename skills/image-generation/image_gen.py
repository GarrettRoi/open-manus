#!/usr/bin/env python3
"""
AI Image Generation Client for Cora
Uses OpenRouter to access multiple image generation models.
"""
import os
import json
import base64
import requests
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Literal


OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1"
IMAGE_OUTPUT_DIR = os.environ.get("IMAGE_OUTPUT_DIR", "/workspace/images")

# Available models
MODELS = {
    "flux-pro": "black-forest-labs/flux-1.1-pro",
    "flux-schnell": "black-forest-labs/flux-schnell",
    "flux-dev": "black-forest-labs/flux-dev",
    "sd3": "stabilityai/stable-diffusion-3-5-large",
    "dalle3": "openai/dall-e-3",
}

# Brand style guides
BRAND_STYLES = {
    "vows_vinyl": {
        "name": "Vows & Vinyl DJ Co.",
        "style_suffix": "romantic wedding atmosphere, warm golden lighting, elegant and joyful, professional photography",
        "colors": "gold, ivory, deep burgundy",
        "aspect_ratio": "1:1"
    },
    "cana": {
        "name": "Cana Collective",
        "style_suffix": "Catholic wedding aesthetic, clean professional style, warm and welcoming, elegant",
        "colors": "white, gold, sage green",
        "aspect_ratio": "1:1"
    },
    "mcgarry": {
        "name": "McGarry Homes",
        "style_suffix": "professional real estate photography, bright and inviting, Oklahoma City, modern",
        "colors": "navy blue, white, warm neutrals",
        "aspect_ratio": "16:9"
    }
}


def generate_image(
    prompt: str,
    model: str = "flux-pro",
    width: int = 1024,
    height: int = 1024,
    brand: Optional[str] = None,
    output_filename: Optional[str] = None,
    save_local: bool = True
) -> Dict[str, Any]:
    """
    Generate an AI image using OpenRouter.
    
    Args:
        prompt: Text description of the image to generate
        model: Model key from MODELS dict or full model name
        width: Image width in pixels (default 1024)
        height: Image height in pixels (default 1024)
        brand: Optional brand key ('vows_vinyl', 'cana', 'mcgarry') for style injection
        output_filename: Optional filename for saved image (auto-generated if None)
        save_local: Whether to save the image locally
    
    Returns:
        Dict with 'url', 'local_path', 'prompt_used', 'model_used'
    """
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY environment variable not set")
    
    # Resolve model name
    model_name = MODELS.get(model, model)
    
    # Apply brand style if specified
    full_prompt = prompt
    if brand and brand in BRAND_STYLES:
        style = BRAND_STYLES[brand]
        full_prompt = f"{prompt}, {style['style_suffix']}"
    
    # Build the request
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://vowsok.com",
        "X-Title": "Cora Image Generator"
    }
    
    payload = {
        "model": model_name,
        "prompt": full_prompt,
        "width": width,
        "height": height,
        "n": 1
    }
    
    # OpenRouter image generation uses the images endpoint
    resp = requests.post(
        f"{OPENROUTER_URL}/images/generations",
        headers=headers,
        json=payload,
        timeout=120
    )
    resp.raise_for_status()
    data = resp.json()
    
    # Extract image URL or base64
    image_data = data.get("data", [{}])[0]
    image_url = image_data.get("url", "")
    image_b64 = image_data.get("b64_json", "")
    
    result = {
        "url": image_url,
        "local_path": None,
        "prompt_used": full_prompt,
        "model_used": model_name,
        "brand": brand,
        "dimensions": f"{width}x{height}"
    }
    
    # Save locally if requested
    if save_local and (image_url or image_b64):
        Path(IMAGE_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        
        if not output_filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
            output_filename = f"cora_{timestamp}_{prompt_hash}.png"
        
        local_path = os.path.join(IMAGE_OUTPUT_DIR, output_filename)
        
        if image_b64:
            with open(local_path, "wb") as f:
                f.write(base64.b64decode(image_b64))
        elif image_url:
            img_resp = requests.get(image_url, timeout=60)
            img_resp.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(img_resp.content)
        
        result["local_path"] = local_path
        print(f"Image saved to: {local_path}")
    
    return result


def generate_social_post_image(
    brand: str,
    topic: str,
    platform: Literal["instagram", "facebook", "twitter"] = "instagram"
) -> Dict[str, Any]:
    """
    Generate a social media post image for a specific brand and platform.
    
    Args:
        brand: Brand key ('vows_vinyl', 'cana', 'mcgarry')
        topic: What the image should be about
        platform: Target social media platform (affects dimensions)
    
    Returns:
        Generated image result dict
    """
    # Platform-specific dimensions
    dimensions = {
        "instagram": (1080, 1080),
        "facebook": (1200, 630),
        "twitter": (1200, 675)
    }
    
    width, height = dimensions.get(platform, (1080, 1080))
    brand_info = BRAND_STYLES.get(brand, {})
    brand_name = brand_info.get("name", brand)
    
    prompt = f"Professional marketing image for {brand_name}: {topic}"
    
    return generate_image(
        prompt=prompt,
        model="flux-pro",
        width=width,
        height=height,
        brand=brand,
        output_filename=f"{brand}_{platform}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    )


def generate_real_estate_image(
    property_type: str,
    features: str,
    location: str = "Oklahoma City"
) -> Dict[str, Any]:
    """Generate a real estate marketing image for McGarry Homes."""
    prompt = f"Professional real estate photo of {property_type} in {location}, {features}"
    return generate_image(
        prompt=prompt,
        model="flux-pro",
        width=1200,
        height=800,
        brand="mcgarry"
    )


if __name__ == "__main__":
    # Test the image generation
    print("Testing Cora image generation...")
    
    if not OPENROUTER_API_KEY:
        print("ERROR: OPENROUTER_API_KEY not set")
        exit(1)
    
    # Test with a simple prompt
    result = generate_image(
        prompt="A beautiful wedding reception with elegant table settings and soft candlelight",
        brand="vows_vinyl",
        save_local=False  # Don't save in test mode
    )
    print(f"Generated image URL: {result.get('url', 'N/A')}")
    print(f"Model used: {result.get('model_used')}")
    print("Image generation test successful!")
