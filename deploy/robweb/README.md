# robweb deployment

Hardened, loopback-only Docker build of hermes-desktop-web used on robweb
(behind Cloudflare Access at desktop-web.robweb.dev).

Build from the repo root:

    docker build -f deploy/robweb/Dockerfile -t hermes-desktop-web:local .
    docker compose -f deploy/robweb/compose.yaml up -d

Branch layout: `main` tracks upstream; `fix/*` are upstream-proposed fixes;
`robweb` = main + fixes + this folder. Upgrade: `git fetch upstream && git merge upstream/main` on `robweb`, rebuild.
No secrets live here; auth is the Hermes dashboard's own login plus Cloudflare Access.
