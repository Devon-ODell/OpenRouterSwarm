# Influencer Studio

A standalone fork of the Flint swarm idea for building and operating **fictional,
clearly disclosed AI social media creators**. It turns one persona into a consistent
content pipeline and a persona-grounded fan inbox:

1. The creative director turns a post concept into a caption and visual brief.
2. The visual creator sends that brief to OpenRouter's Images API.
3. The community manager replies to fans in the persona's voice, with thread memory.

Every profile and post is marked AI-generated. Prompts require an original adult
character, and the service refuses minor-coded, explicit, or real-person-cloning
requests. It does not publish to social networks; a human remains the final publisher.

## Run it

No package installation is needed (Python 3.10+):

```sh
cd influencer-studio
python3 server.py
```

Open <http://127.0.0.1:8787>. With no API key it automatically runs in offline demo
mode, generating local preview art and deterministic fan replies.

For live output:

```sh
cp .env.example .env
# export the values in .env, or load it with your shell
export OPENROUTER_API_KEY='sk-or-v1-...'
python3 server.py
```

The default visual model is `openai/gpt-image-1-mini`; model availability and pricing
change, so set `INFLUENCER_IMAGE_MODEL` to another Images API model when needed.
`INFLUENCER_CHAT_MODEL=openrouter/free` keeps creative direction and fan chat light.

Data is local in `data/studio.db`; generated media is in `data/media/`. The API key is
read from the process environment and is never sent to the browser or stored in SQLite.

## Test

```sh
python3 -m unittest discover -s tests -v
```

The tests force offline mode and cover persona safety, post generation, persistence,
fan-thread memory, and core HTTP routes.

## HTTP API

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/health` | Mode and model configuration |
| `GET/POST` | `/api/influencers` | List or create personas |
| `GET` | `/api/influencers/:id/posts` | Persona content grid |
| `POST` | `/api/influencers/:id/posts/generate` | Generate and save one post |
| `POST` | `/api/influencers/:id/chat` | Receive a fan message and draft a reply |
| `GET` | `/api/influencers/:id/conversations` | Fan inbox summary |

This is an operator studio, not an autonomous engagement bot. Before production use,
add authentication, moderation at the model boundary, consent/audit workflows, rate
limits, backups, and each social platform's approved publishing/messaging API.
