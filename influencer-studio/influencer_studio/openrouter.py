"""Minimal OpenRouter client for creative direction, images, and fan chat."""

import base64
import json
import os
import re
import urllib.error
import urllib.request


class ModelError(RuntimeError):
    pass


def _json_from_text(text):
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ModelError("Creative director returned invalid JSON")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ModelError("Creative director returned invalid JSON") from exc


class OpenRouter:
    def __init__(self, media_dir):
        self.key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
        self.chat_model = os.environ.get("INFLUENCER_CHAT_MODEL", "openrouter/free")
        self.image_model = os.environ.get("INFLUENCER_IMAGE_MODEL", "openai/gpt-image-1-mini")
        self.mock = os.environ.get("INFLUENCER_MOCK", "").lower() in {"1", "true", "yes"} or not self.key
        self.media_dir = media_dir
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def _post(self, endpoint, payload, timeout=180):
        req = urllib.request.Request(
            f"{self.base}/{endpoint.lstrip('/')}",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                     "HTTP-Referer": "http://localhost:8787", "X-Title": "Influencer Studio"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:600]
            raise ModelError(f"OpenRouter returned {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModelError(f"Could not reach OpenRouter: {exc}") from exc

    def _chat(self, messages, *, temperature=0.8, max_tokens=500):
        data = self._post("chat/completions", {
            "model": self.chat_model, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens,
        })
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("OpenRouter chat response contained no answer") from exc

    def create_post_brief(self, persona, concept):
        if self.mock:
            return {
                "caption": f"A little {concept.lower()} energy for your feed. What would you try first? ✨\n\n#AICreator #{persona['niche'].replace(' ', '')}",
                "image_prompt": self._image_prompt(persona, concept),
            }
        system = (
            "You are the creative director for an explicitly fictional, AI-generated social media creator. "
            "Return only JSON with string fields caption and image_prompt. The caption must sound authentic, "
            "include a light engagement question and never claim a real lived event. The image prompt must depict "
            "one original adult aged 25+, never a celebrity or real person, and must not include logos or text."
        )
        user = f"PERSONA\n{json.dumps(persona, ensure_ascii=False)}\n\nPOST CONCEPT\n{concept}"
        result = _json_from_text(self._chat([{"role": "system", "content": system},
                                             {"role": "user", "content": user}], temperature=0.9))
        if not isinstance(result.get("caption"), str) or not isinstance(result.get("image_prompt"), str):
            raise ModelError("Creative director omitted caption or image_prompt")
        return result

    @staticmethod
    def _image_prompt(persona, concept):
        return (f"Editorial social media portrait of an original fictional adult creator, age 25+, "
                f"{persona['appearance']}. Theme: {concept}. Niche: {persona['niche']}. "
                "Natural skin texture, candid composition, tasteful contemporary styling, soft daylight, "
                "high detail, no text, no watermark, no brand logos, not a real person or celebrity.")

    def generate_image(self, prompt, post_id, aspect_ratio="4:5"):
        if self.mock:
            safe = (prompt[:120].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="1000">'
                   '<defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="#f4b8a4"/>'
                   '<stop offset=".5" stop-color="#b8a4f4"/><stop offset="1" stop-color="#77d9c7"/></linearGradient></defs>'
                   '<rect width="100%" height="100%" fill="url(#g)"/><circle cx="400" cy="390" r="190" fill="#fff" opacity=".2"/>'
                   '<text x="50%" y="78%" text-anchor="middle" fill="white" font-family="system-ui" font-size="34">OFFLINE PREVIEW</text>'
                   f'<text x="50%" y="84%" text-anchor="middle" fill="white" font-family="system-ui" font-size="18">{safe}</text></svg>')
            name = f"{post_id}.svg"
            (self.media_dir / name).write_text(svg)
            return f"/media/{name}", 0.0
        data = self._post("images", {"model": self.image_model, "prompt": prompt,
                                      "n": 1, "aspect_ratio": aspect_ratio,
                                      "quality": "low", "output_format": "webp"}, timeout=300)
        try:
            image = data["data"][0]
            raw = base64.b64decode(image["b64_json"], validate=True)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError("OpenRouter image response contained no valid image") from exc
        media_type = image.get("media_type", "image/webp")
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/svg+xml": "svg"}.get(media_type, "webp")
        name = f"{post_id}.{ext}"
        (self.media_dir / name).write_bytes(raw)
        return f"/media/{name}", (data.get("usage") or {}).get("cost")

    def fan_reply(self, persona, history):
        latest = history[-1]["content"]
        if self.mock:
            return f"I love that you brought up “{latest[:60]}”! 💫 What got you interested in it?"
        system = f"""You are @{persona['handle']}, an original fictional AI influencer.
You must never claim to be human. If asked, say clearly that you are an AI-created character.
Name: {persona['name']}. Niche: {persona['niche']}. Bio: {persona['bio']}.
Voice: {persona['voice']}. Values: {', '.join(persona['values'])}.
Boundaries: {persona['boundaries']}.
Be warm and specific in 1–3 short sentences. Do not form romantic dependency, solicit money,
move the fan off-platform, give professional medical/legal/financial advice, or promise secrecy."""
        messages = [{"role": "system", "content": system}]
        messages.extend({"role": "assistant" if m["role"] == "influencer" else "user",
                         "content": m["content"]} for m in history[-16:])
        return self._chat(messages, temperature=0.85, max_tokens=180).strip()
