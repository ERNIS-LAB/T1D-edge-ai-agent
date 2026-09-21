# DiabetesAgent web UI

React front-end for the DiabetesAgent HTTP API: chat with streamed tool calls and
per-turn metrics, plus report and session browsers.

This is only the front-end. **The Python API must be running** — see the
[main README](../README.md#running-the-web-ui).

## Development

```bash
# terminal 1 — from the repository root
source venv/bin/activate
python server.py                 # binds 127.0.0.1:8000

# terminal 2 — from this directory
bun install
bun dev                          # http://localhost:3001
```

Set `PORT` to serve on a different port.

## Production build

```bash
bun run build                    # outputs to dist/
bun start
```

## Configuration

The API base URL is `API_BASE` at the top of [`src/App.tsx`](src/App.tsx), set to
`http://localhost:8000/api`. If you change it, start the API with a matching CORS
origin:

```bash
python server.py --cors-origin http://localhost:3001
```
