# Talk to Z Zuno

A little web toy: you talk to Z Zuno, a 3D alien, using only picture symbols (faces, hands, animals, food, numbers, arrows...). He works out what the whole message means and answers in words, showing how he feels with his face screen, head, antennae, arms and hands.

Built with plain JavaScript and [three.js](https://threejs.org/) (r128). No build step.

## What it does

- **265 symbols in four tabs** you can open and close: Expressions, World, Life, Signs and numbers.
- **The alien reads the whole message** and answers with what he feels and thinks, not by repeating your symbols.
- **A rigged 3D model**: the single mesh is cut at load time into head, antennae, body, arms, elbows and hands, each on its own pivot with spring-smoothed animation. Moods change his posture, his antennae and the pixel-art mouth on his LED mask.
- **Two brains**:
  - An AI brain through [OpenRouter](https://openrouter.ai/) (default model `anthropic/claude-haiku-5.5` with light thinking, and a fast automatic fallback).
  - An offline brain made of written lines, used when no key is set or the AI is unavailable.

## Run it

You need Python 3 and a browser. No packages to install.

```bash
cp .env.example .env      # then put your OpenRouter key in .env
python server.py
```

Open <http://127.0.0.1:8773/>. The top-right "Brain" button shows which brain is active.

Without a key everything still works, using the offline brain.

### Your key stays private

The page never sees your API key. `server.py` reads it from `.env`, adds it to requests it sends to OpenRouter, and never returns it. The server:

- listens only on `127.0.0.1` and refuses requests from other websites,
- serves only `index.html` and `alien.glb` (never `.env`),
- fixes the model and reply length itself, and limits messages per minute.

`.env` is in `.gitignore`. Never commit it. If a key ever leaks, delete it at <https://openrouter.ai/keys> and make a new one.

The Brain button can also take a key you paste in (an OpenRouter `sk-or-...` or Anthropic `sk-ant-...` key). That key is kept in the browser's local storage only.

### Settings in `.env`

| Name | Meaning | Default |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | your key | none (offline brain) |
| `OPENROUTER_MODEL` | main model | `anthropic/claude-haiku-5.5` |
| `OPENROUTER_FALLBACK_MODEL` | used if the main one is busy | `google/gemini-2.5-flash-lite` |
| `OPENROUTER_REASONING_EFFORT` | how long a thinking model may think first (`low` is fast) | `low` |

## The 3D model is not included

`alien.glb` is not in this repository, because I do not have the right to redistribute that model. Without it the page shows a big 👽 instead, and everything else still works.

To use a model, put a glTF binary at `alien.glb` next to `index.html`. The cut lines for the head, arms, elbows, wrists and antennae (`MODEL_CUT` near the top of `index.html`) and the LED mouth screen (`MASK`, which should sit over the model's mouth) are tuned to the current model, a textured figure of about 65,000 triangles, so a different model needs those numbers adjusted.

## Files

- `index.html`: the whole page (symbols, brains, 3D scene, animation).
- `server.py`: the small local server that holds the key.
- `.env.example`: copy to `.env` and fill in.

## Licence

Code: MIT, see `LICENSE`. The 3D model is not covered and not included.
