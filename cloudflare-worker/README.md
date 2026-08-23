# One-click report trigger

This Cloudflare Worker is the credential boundary between the public GitHub
Pages dashboard and the repository's `deepseek-daily-report.yml` workflow.

It exposes only two CORS-restricted endpoints:

- `GET /status` returns sanitized archive/workflow freshness.
- `POST /trigger` dispatches the workflow only when the current Beijing-day
  DeepSeek report is missing and no run is active.

`GITHUB_TOKEN` must be stored as a Cloudflare Worker secret. Never put it in
`wrangler.jsonc`, the dashboard JavaScript, or GitHub Pages data files.

Deployment:

```powershell
pnpm install
pnpm exec wrangler secret put GITHUB_TOKEN
pnpm exec wrangler deploy
```

After deployment, set the public Worker URL in the `update-service-url` meta
tag in `docs/index.html`.
