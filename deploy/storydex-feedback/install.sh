#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: install.sh <feedback-root> <release-id> <package-dir>" >&2
  exit 2
fi

feedback_root="${1%/}"
release_id="$2"
package_dir="${3%/}"
[[ "$feedback_root" == /www/wwwroot/updates.septemc.com/storydex/feedback ]]
[[ "$release_id" =~ ^[0-9A-Za-z._-]+$ ]]
[[ -f "$package_dir/server.py" && -f "$package_dir/admin.html" ]]

release_dir="$feedback_root/releases/$release_id"
current_link="$feedback_root/current"
previous_target=""
if [[ -L "$current_link" ]]; then
  previous_target="$(readlink "$current_link")"
fi

nginx_bin="$(command -v nginx || true)"
if [[ -z "$nginx_bin" && -x /www/server/nginx/sbin/nginx ]]; then
  nginx_bin=/www/server/nginx/sbin/nginx
fi
[[ -n "$nginx_bin" ]] || { echo "nginx executable not found" >&2; exit 1; }

site_config=""
for candidate in \
  /www/server/panel/vhost/nginx/updates.septemc.com.conf \
  /etc/nginx/conf.d/updates.septemc.com.conf \
  /etc/nginx/sites-enabled/updates.septemc.com; do
  if [[ -f "$candidate" ]] && grep -Eq 'server_name[^;]*updates\.septemc\.com' "$candidate"; then
    site_config="$candidate"
    break
  fi
done
if [[ -z "$site_config" ]]; then
  while IFS= read -r candidate; do
    if grep -Eq 'server_name[^;]*updates\.septemc\.com' "$candidate"; then
      site_config="$candidate"
      break
    fi
  done < <(find /www/server/panel/vhost/nginx /etc/nginx -maxdepth 3 -type f -name '*.conf' 2>/dev/null | sort -u)
fi
[[ -n "$site_config" ]] || { echo "updates.septemc.com nginx config not found" >&2; exit 1; }

nginx_args=()
panel_nginx=0
if [[ "$site_config" == /www/server/panel/vhost/nginx/* && -x /www/server/nginx/sbin/nginx ]]; then
  nginx_bin=/www/server/nginx/sbin/nginx
  nginx_args=(-p /www/server/nginx/ -c conf/nginx.conf)
  panel_nginx=1
fi

nginx_test() {
  "$nginx_bin" "${nginx_args[@]}" -t
}

nginx_reload() {
  if [[ $panel_nginx -eq 1 && -x /etc/init.d/nginx ]]; then
    /etc/init.d/nginx reload
  elif systemctl is-active --quiet nginx.service; then
    systemctl reload nginx.service
  else
    "$nginx_bin" "${nginx_args[@]}" -s reload
  fi
}

service_user=www
if ! id "$service_user" >/dev/null 2>&1; then
  service_user=www-data
fi
id "$service_user" >/dev/null 2>&1
service_group="$(id -gn "$service_user")"
python_bin=/usr/bin/python3
if [[ ! -x "$python_bin" ]]; then
  python_bin="$(command -v python3 || true)"
fi
[[ -n "$python_bin" && "$python_bin" == /* ]] || { echo "python3 executable not found" >&2; exit 1; }
"$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 6) else 1)' \
  || { echo "Storydex feedback requires Python 3.6 or newer: $python_bin" >&2; exit 1; }

mkdir -p "$feedback_root/releases" "$feedback_root/data/images"
[[ ! -e "$release_dir" ]]
mkdir -p "$release_dir"
cp -a "$package_dir/." "$release_dir/"
chown -R root:root "$release_dir"
chmod -R u=rwX,go=rX "$release_dir"
chown -R "$service_user:$service_group" "$feedback_root/data"
chmod 750 "$feedback_root/data" "$feedback_root/data/images"

"$python_bin" "$release_dir/server.py" --root "$feedback_root" --check
chown -R "$service_user:$service_group" "$feedback_root/data"

snippet_path="$feedback_root/nginx-location.conf"
snippet_backup=""
if [[ -f "$snippet_path" ]]; then
  snippet_backup="$(mktemp /tmp/storydex-feedback-snippet.XXXXXX)"
  cp -a "$snippet_path" "$snippet_backup"
fi
install -m 0644 "$release_dir/nginx-location.conf" "$snippet_path"
config_backup="$(mktemp /tmp/storydex-feedback-nginx.XXXXXX)"
cp -a "$site_config" "$config_backup"

rollback() {
  status=$?
  trap - EXIT
  if [[ $status -ne 0 ]]; then
    echo "deployment failed; restoring previous service and nginx configuration" >&2
    cp -a "$config_backup" "$site_config" || true
    if [[ -n "$snippet_backup" ]]; then
      cp -a "$snippet_backup" "$snippet_path" || true
    else
      rm -f "$snippet_path"
    fi
    if [[ -n "$previous_target" ]]; then
      ln -sfn "$previous_target" "$current_link.rollback"
      mv -Tf "$current_link.rollback" "$current_link"
      systemctl restart storydex-feedback.service || true
    else
      rm -f "$current_link"
      systemctl stop storydex-feedback.service || true
    fi
    nginx_test && nginx_reload || true
  fi
  rm -f "$config_backup" "$snippet_backup"
  exit "$status"
}
trap rollback EXIT

python3 "$release_dir/install_nginx_include.py" "$site_config" "$snippet_path"
nginx_test

ln -sfn "$release_dir" "$current_link.next"
mv -Tf "$current_link.next" "$current_link"

sed \
  -e "s|__CURRENT__|$current_link|g" \
  -e "s|__ROOT__|$feedback_root|g" \
  -e "s|__PYTHON__|$python_bin|g" \
  -e "s|__SERVICE_USER__|$service_user|g" \
  -e "s|__SERVICE_GROUP__|$service_group|g" \
  "$release_dir/storydex-feedback.service" > /etc/systemd/system/storydex-feedback.service
systemctl daemon-reload
systemctl enable storydex-feedback.service
systemctl restart storydex-feedback.service

healthy=0
for _ in $(seq 1 20); do
  if curl --fail --silent --show-error "http://127.0.0.1:18766/storydex/feedback/health" >/dev/null; then
    healthy=1
    break
  fi
  sleep 0.5
done
[[ $healthy -eq 1 ]] || { journalctl -u storydex-feedback.service -n 80 --no-pager >&2; exit 1; }

nginx_reload
find "$feedback_root/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
  | sort -nr | tail -n +4 | cut -d' ' -f2- | xargs -r rm -rf

echo "Storydex feedback release $release_id is active"
