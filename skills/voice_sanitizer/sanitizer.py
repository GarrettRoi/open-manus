#!/usr/bin/env python3
"""
Voice Sanitizer — Clean up text before passing it to TTS.
Removes code blocks, long URLs, and technical tool-call jargon.
"""
import re
import argparse

def sanitize_for_voice(text):
    """
    Clean text for natural speech.
    - Removes Markdown code blocks.
    - Replaces long URLs with a simple "link".
    - Removes protocol tags like [REQUEST], [END], etc.
    - Cleans up common technical jargon.
    """
    if not text:
        return ""

    # 1. Remove Markdown code blocks (triple backticks)
    text = re.sub(r'```.*?```', '[code omitted]', text, flags=re.DOTALL)
    
    # 2. Remove inline code (single backticks)
    text = re.sub(r'`.*?`', '', text)
    
    # 3. Remove protocol tags
    text = re.sub(r'\[(NOTIFY|REQUEST|END|REPORT|GROUP CONVERSATION CONTEXT|LATEST MESSAGE FROM .*?)\]', '', text, flags=re.IGNORECASE)
    
    # 4. Simplify URLs
    # Matches http/https URLs and replaces them with "a link" or similar
    text = re.sub(r'https?://\S+', 'a link', text)
    
    # 5. Clean up extra whitespace and newlines
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    
    # 6. Remove common "tool call" or "thinking" artifacts if they leaked
    text = re.sub(r'Tool call:.*?\.', '', text, flags=re.IGNORECASE)
    text = re.sub(r'I will now.*?(\.|$)', '', text, flags=re.IGNORECASE)
    
    return text

def main():
    parser = argparse.ArgumentParser(description="Sanitize text for natural speech.")
    parser.add_argument("text", help="The text to sanitize.")
    args = parser.parse_args()

    print(sanitize_for_voice(args.text))

if __name__ == "__main__":
    main()
