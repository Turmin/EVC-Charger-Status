# Docker deployment

On pull requests, GitHub Actions runs the unit tests. On pushes to `main`, it also builds an amd64/arm64 Docker image, publishes the commit-tagged image to GHCR, and deploys it to the VPS. The VPS needs Docker Engine and the Compose plugin. The SSH deploy user must be able to run `docker`.

## VPS setup

Create a private deployment directory, for example `/opt/evc-charger-status`, owned by the deploy user. Put `.env` with `EVC_API_KEY=...` there. The workflow uploads the tracked `config.json` with the charger list on each deployment. Optional `.env` entries are `EVC_DEVICE_ID`, `EVC_BASE_URL`, and `EVC_PORT` (default 8000). The private `.env` is ignored by Git and stays on the VPS. The workflow also uploads `docker-compose.yml`.

Before the first deploy, create the private environment file on the VPS:

```bash
cd /opt/evc-charger-status
install -m 600 /dev/null .env
# Edit .env and add EVC_API_KEY=your_actual_key
```

Use your actual `DEPLOY_PATH` instead of the example path. The workflow stops with a clear error if this file or the deployment directory is missing.

The API binds to `127.0.0.1:EVC_PORT` on the VPS, for use behind an existing reverse proxy. SQLite history is stored in the named Docker volume `evc-status-data` and survives container replacement. Keep one deployment of this app per VPS unless you give each stack a separate Compose project name and port.

## GitHub Actions secrets

Set `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH` (absolute directory path), `DEPLOY_SSH_KEY` (private SSH key), and `DEPLOY_KNOWN_HOSTS` (verified SSH host key line) in repository secrets. `DEPLOY_PORT` is optional and defaults to 22. Keep host key verification enabled. The workflow uses the `production` GitHub environment; configure reviewers there if wanted.

For a private GHCR package, also set `GHCR_USERNAME` and `GHCR_READ_TOKEN` to a GitHub account and token with `read:packages` access. Public packages need neither. The workflow's `GITHUB_TOKEN` publishes the image. The EVC API key is only needed on the VPS.

After the first deployment, check from the deployment directory:

```bash
docker compose ps
docker compose logs --tail=100
curl -fsS http://127.0.0.1:8000/health
```

Use your configured `EVC_PORT` if different. `/health` confirms the API runs; it does not contact EVC-net.
