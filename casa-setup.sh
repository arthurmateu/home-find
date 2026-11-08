#!/usr/bin/env bash
# Sets up casa.admou.se behind nginx: the family page, which homefind uploads as plain
# files (see homefind/casa.py). Nothing runs on the server but nginx.
# Run on the server as root:  sudo bash casa-setup.sh
# Safe to re-run.
set -euo pipefail

DOMAIN=casa.admou.se
ROOT=/var/www/${DOMAIN}
UPLOADER=deploy  # the ssh user homefind uploads as

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

echo "==> Creating ${ROOT}"
install -d -o "$UPLOADER" -g www-data -m 755 "$ROOT"

echo "==> Configuring nginx"
# Not rewritten on a re-run: certbot adds its HTTPS settings to this file.
if [ ! -e /etc/nginx/sites-available/casa ]; then
cat > /etc/nginx/sites-available/casa <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    root ${ROOT};
    index index.html;
    access_log off;  # the private link is in every request

    add_header X-Robots-Tag "noindex, nofollow" always;
    add_header Referrer-Policy "strict-origin" always;  # other sites never see the private link
    add_header X-Content-Type-Options "nosniff" always;
    add_header Content-Security-Policy "default-src 'none'; img-src 'self' https://tile.openstreetmap.org; style-src 'unsafe-inline'; script-src 'unsafe-inline'; manifest-src 'self'; base-uri 'none'; form-action 'none'" always;

    location = / { return 404; }  # the page is only under its private link
    location = /robots.txt { default_type text/plain; return 200 "User-agent: *\nDisallow: /\n"; }
    location ~ \.webmanifest\$ { default_type application/manifest+json; expires -1; }
    location ~ /photos/ { expires 1y; }  # named after their content
    location / { expires -1; }           # always ask whether the page changed
}
EOF
fi
ln -sf /etc/nginx/sites-available/casa /etc/nginx/sites-enabled/casa
nginx -t
systemctl reload nginx

echo "==> Getting TLS certificate"
certbot --nginx -d "$DOMAIN" --redirect --keep-until-expiring

echo
echo "Done. https://${DOMAIN}/ answers 404 by design; the page is under its private link."
