# Cloudflare logo index deployment

This publishes the repository's generated `logos-map.json` as a separate public
Cloudflare Worker static asset at `/logos-map.json`. It does not read or proxy
an M3U, and it does not modify any existing Worker.

## One-time setup

1. In Cloudflare, create an API token with permission to edit Workers Scripts
   for the account that owns the `workers.dev` hostname.
2. In GitHub, open **Settings → Secrets and variables → Actions** and add:
   - `CLOUDFLARE_API_TOKEN`: the token value.
   - `CLOUDFLARE_ACCOUNT_ID`: the Cloudflare account ID.
3. Open **Actions → Deploy EPG logo index to Cloudflare → Run workflow**.

The first successful run creates the new Worker named `aptv-logo-index`.
Its public JSON endpoint is
`https://aptv-logo-index.<your-workers-subdomain>.workers.dev/logos-map.json`.
Future changes to `logos-map.json` on `main` deploy automatically.

Do not put the API token, M3U URL, or stream credentials in repository files,
workflow logs, or public issues. If the secrets are not set, the deploy job is
skipped.
