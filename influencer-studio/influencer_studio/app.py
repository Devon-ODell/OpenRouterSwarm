"""Application service: validation and orchestration, independent of HTTP."""

import uuid

from .safety import SafetyError, clean_text, safe_handle, validate_creative_text


DISCLOSURE = "Fictional AI-generated creator · Images and replies are generated with AI"


class App:
    def __init__(self, store, models):
        self.store = store
        self.models = models

    def create_influencer(self, body):
        values = body.get("values", [])
        if isinstance(values, str):
            values = [x.strip() for x in values.split(",") if x.strip()]
        if not isinstance(values, list) or not 1 <= len(values) <= 8:
            raise SafetyError("values must contain 1–8 items")
        item = {
            "name": clean_text(body.get("name", ""), "name", limit=60, required=True),
            "handle": safe_handle(body.get("handle", "")),
            "niche": clean_text(body.get("niche", ""), "niche", limit=80, required=True),
            "bio": clean_text(body.get("bio", ""), "bio", limit=300, required=True),
            "voice": clean_text(body.get("voice", ""), "voice", limit=300, required=True),
            "appearance": clean_text(body.get("appearance", ""), "appearance", limit=600, required=True),
            "values": [clean_text(str(v), "value", limit=50, required=True) for v in values],
            "boundaries": clean_text(body.get("boundaries", ""), "boundaries", limit=500, required=True),
            "disclosure": DISCLOSURE,
        }
        validate_creative_text(*[v for v in item.values() if isinstance(v, str)])
        return self.store.create_influencer(item)

    def generate_post(self, influencer_id, body):
        persona = self._persona(influencer_id)
        concept = clean_text(body.get("concept", ""), "concept", limit=600, required=True)
        validate_creative_text(concept)
        ratio = body.get("aspect_ratio", "4:5")
        if ratio not in {"1:1", "4:5", "9:16"}:
            raise SafetyError("aspect_ratio must be 1:1, 4:5, or 9:16")
        brief = self.models.create_post_brief(persona, concept)
        validate_creative_text(brief["caption"], brief["image_prompt"])
        post_id = f"asset_{uuid.uuid4().hex[:12]}"
        image_url, cost = self.models.generate_image(brief["image_prompt"], post_id, ratio)
        return self.store.create_post({
            "influencer_id": influencer_id, "concept": concept,
            "caption": clean_text(brief["caption"], "caption", limit=2200, required=True),
            "image_prompt": clean_text(brief["image_prompt"], "image_prompt", limit=4000, required=True),
            "image_url": image_url, "model": self.models.image_model, "cost": cost,
        })

    def chat(self, influencer_id, body):
        persona = self._persona(influencer_id)
        fan_id = clean_text(body.get("fan_id", ""), "fan_id", limit=80, required=True)
        fan_name = clean_text(body.get("fan_name", "Fan"), "fan_name", limit=60, required=True)
        message = clean_text(body.get("message", ""), "message", limit=1500, required=True)
        inbound = self.store.add_message(influencer_id, fan_id, fan_name, "fan", message)
        history = self.store.thread(influencer_id, fan_id)
        reply = clean_text(self.models.fan_reply(persona, history), "reply", limit=1500, required=True)
        outbound = self.store.add_message(influencer_id, fan_id, fan_name, "influencer", reply)
        return {"message": inbound, "reply": outbound, "disclosure": persona["disclosure"]}

    def _persona(self, influencer_id):
        persona = self.store.get_influencer(influencer_id)
        if not persona:
            raise KeyError("Influencer not found")
        return persona
