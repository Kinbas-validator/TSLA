#!/usr/bin/env bash
# TSLA spread monitor — установка / удаление одной командой (Ubuntu 22.04/24.04).
#
#   Установка:  curl -fsSL https://raw.githubusercontent.com/Kinbas-validator/TSLA/main/install.sh | sudo bash
#   Удаление:   sudo tsla-spread uninstall
#               (или: curl -fsSL https://raw.githubusercontent.com/Kinbas-validator/TSLA/main/install.sh | sudo bash -s uninstall)
#   Прочее:     sudo tsla-spread status | logs | url | config | password | update
#
# Переменные (необязательно): WEB_PASSWORD, TG_BOT_TOKEN, DOMAIN (свой домен вместо бесплатного *.sslip.io),
#   MODE=auto|caddy|nginx|tunnel (auto — определить по серверу), ALERT_SPREAD_USD, ALERT_SIZE.
#
# Как встраивается, ничего не ломая:
#   * приложение слушает только 127.0.0.1 на свободном порту (18090+), наружу ничего не открывает;
#   * если на 80/443 уже работает Caddy (systemd + Caddyfile) — в конец Caddyfile дописывается ОДИН блок
#     для нашего домена (между маркерами), с бэкапом, `caddy validate` и откатом при ошибке;
#   * если там nginx — отдельный server-блок только для нашего домена + сертификат certbot;
#   * если 80/443 свободны — ставится Caddy;
#   * если там что-то иное (docker, apache, traefik…) или HTTPS не получился — Cloudflare Quick Tunnel
#     (https://*.trycloudflare.com, порты не нужны; адрес приходит в Telegram).
#   Все изменения записываются в /etc/tsla-spread/install.state, uninstall откатывает ровно их.
set -euo pipefail

APP=tsla-spread
REPO=Kinbas-validator/TSLA
BRANCH=${BRANCH:-main}
DIR=/opt/$APP
DATA=/var/lib/$APP
ETC=/etc/$APP
CONF=$ETC/config.env
STATE=$ETC/install.state
BIN=/usr/local/bin/$APP
UNIT=/etc/systemd/system/$APP.service
TUNIT=/etc/systemd/system/$APP-tunnel.service
MARK_B="# >>> $APP block (added by $APP installer; removed by '$APP uninstall') >>>"
MARK_E="# <<< $APP block <<<"

c_g=$'\e[32m'; c_y=$'\e[33m'; c_r=$'\e[31m'; c_b=$'\e[1m'; c_0=$'\e[0m'
say()  { echo "${c_g}==>${c_0} $*"; }
warn() { echo "${c_y}!!${c_0} $*"; }
die()  { echo "${c_r}ошибка:${c_0} $*" >&2; exit 1; }
need_root() { [[ $EUID -eq 0 ]] || die "нужен root: добавьте sudo"; }

ask() { # ask VAR "prompt" default [secret]
  local __v="$1" __p="$2" __d="${3:-}" __s="${4:-}" __a=""
  if [[ -n "${!__v:-}" ]]; then return 0; fi
  if { true </dev/tty; } 2>/dev/null; then
    if [[ -n "$__s" ]]; then read -r -s -p "$__p" __a </dev/tty || true; echo >/dev/tty
    else read -r -p "$__p" __a </dev/tty || true; fi
  fi
  printf -v "$__v" '%s' "${__a:-$__d}"
}
st_get() { grep -E "^$1=" "$STATE" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
st_set() { mkdir -p "$ETC"; touch "$STATE"; sed -i "/^$1=/d" "$STATE"; echo "$1=$2" >> "$STATE"; }
conf_get() { grep -E "^$1=" "$CONF" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
conf_set() { sed -i "/^$1=/d" "$CONF"; echo "$1=$2" >> "$CONF"; }
rand() { python3 -c "import secrets;print(secrets.token_${1:-hex}(${2:-32}))"; }
public_ip() {
  local ip
  for u in https://api.ipify.org https://ifconfig.me/ip https://ipv4.icanhazip.com; do
    ip=$(curl -fsS -4 --max-time 6 "$u" 2>/dev/null | tr -d '[:space:]') || true
    [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && { echo "$ip"; return 0; }
  done
  return 1
}
port_busy() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }
free_port() { local p; for p in $(seq "$1" $(( $1 + 200 ))); do port_busy "$p" || { echo "$p"; return 0; }; done; die "нет свободного порта"; }
proc_on() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -oP 'users:\(\("\K[^"]+' | head -1 || true; }
apt_install() { DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null; }
apt_updated=0
apt_update() { [[ $apt_updated == 1 ]] || { apt-get update -qq >/dev/null; apt_updated=1; }; }
pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }

# ------------------------------------------------------------------ source files
fetch_source() {
  local self="${BASH_SOURCE[0]:-}"
  if [[ -n "$self" && -f "$self" && -f "$(dirname "$self")/app/app.py" ]]; then
    SRC=$(cd "$(dirname "$self")" && pwd)
    return 0
  fi
  local tmp; tmp=$(mktemp -d)
  say "Скачиваю $REPO@$BRANCH"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" | tar -xz -C "$tmp"
  SRC=$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -1)
  [[ -f "$SRC/app/app.py" ]] || die "в архиве нет app/app.py"
}

install_files() {
  mkdir -p "$DIR" "$DATA" "$ETC"
  install -m 644 "$SRC"/app/app.py "$SRC"/app/index.html "$SRC"/app/login.html "$DIR"/
  install -m 755 "$SRC/install.sh" "$BIN"
  chown $APP:$APP "$DATA"; chmod 750 "$DATA"
}

# ------------------------------------------------------------------ health
wait_https() { # wait_https URL seconds
  local url="$1" t="${2:-120}" host; host=$(echo "$url" | sed -E 's#https?://([^/]+).*#\1#')
  for _ in $(seq 1 $(( t / 5 ))); do
    curl -fsS --max-time 8 "$url/health" >/dev/null 2>&1 && return 0
    curl -fsS --max-time 8 --resolve "$host:443:127.0.0.1" "$url/health" >/dev/null 2>&1 && return 0
    sleep 5
  done
  return 1
}

# ------------------------------------------------------------------ Caddy
caddyfile_path() { # Caddyfile used by the systemd caddy service, empty if not a Caddyfile setup
  local exec cfg
  exec=$(systemctl show caddy -p ExecStart --value 2>/dev/null || true)
  cfg=$(echo "$exec" | grep -oP -- '--config[ =]\K\S+' | head -1 || true)
  [[ -z "$cfg" && -f /etc/caddy/Caddyfile ]] && cfg=/etc/caddy/Caddyfile
  [[ -n "$cfg" && -f "$cfg" ]] || return 0
  if [[ "$cfg" == *.json ]] && ! echo "$exec" | grep -q -- '--adapter caddyfile'; then return 0; fi
  echo "$cfg"
}
strip_block() { # remove our marked block from file
  local f="$1"
  [[ -f "$f" ]] || return 0
  awk -v b="$MARK_B" -v e="$MARK_E" '$0==b{skip=1;next} $0==e{skip=0;next} !skip' "$f" > "$f.tmp.$$" && cat "$f.tmp.$$" > "$f" && rm -f "$f.tmp.$$"
}
caddy_add_site() { # file domain port ; returns 1 and restores backup on failure
  local f="$1" domain="$2" port="$3" bak; bak="$f.bak-$APP-$(date +%Y%m%d%H%M%S)"
  cp -a "$f" "$bak"; st_set CADDY_BACKUP "$bak"
  strip_block "$f"
  cat >> "$f" <<EOF

$MARK_B
$domain {
	encode gzip
	reverse_proxy 127.0.0.1:$port
}
$MARK_E
EOF
  if ! caddy validate --config "$f" --adapter caddyfile >/dev/null 2>&1; then
    warn "caddy validate не прошёл — возвращаю Caddyfile как был"
    cat "$bak" > "$f"; return 1
  fi
  if ! systemctl reload caddy; then
    warn "caddy reload не прошёл — возвращаю Caddyfile как был"
    cat "$bak" > "$f"; systemctl reload caddy || true; return 1
  fi
  return 0
}
setup_caddy_existing() {
  local f; f=$(caddyfile_path)
  [[ -n "$f" ]] || { warn "Caddy запущен не из Caddyfile (JSON/API) — не трогаю его"; return 1; }
  say "Найден работающий Caddy ($f) — добавляю только блок для $DOMAIN, остальное не трогаю"
  caddy_add_site "$f" "$DOMAIN" "$PORT" || return 1
  st_set PROXY caddy-existing; st_set CADDYFILE "$f"
}
setup_caddy_new() {
  say "Порты 80/443 свободны — ставлю Caddy"
  apt_update; apt_install caddy
  st_set CADDY_OURS 1
  local f=/etc/caddy/Caddyfile
  printf '# Caddyfile (installed by %s)\n' "$APP" > "$f"
  cat >> "$f" <<EOF
$MARK_B
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
$MARK_E
EOF
  caddy validate --config "$f" --adapter caddyfile >/dev/null 2>&1 || return 1
  systemctl enable caddy >/dev/null 2>&1; systemctl restart caddy
  st_set PROXY caddy-ours; st_set CADDYFILE "$f"
  open_ufw 80; open_ufw 443
}

# ------------------------------------------------------------------ nginx
setup_nginx() {
  say "Найден nginx — добавляю отдельный server-блок только для $DOMAIN"
  local site
  if [[ -d /etc/nginx/sites-available ]]; then site=/etc/nginx/sites-available/$APP.conf; else site=/etc/nginx/conf.d/$APP.conf; fi
  cat > "$site" <<EOF
# added by $APP installer; removed by '$APP uninstall'
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  [[ $site == /etc/nginx/sites-available/* ]] && ln -sf "$site" /etc/nginx/sites-enabled/$APP.conf
  st_set NGINX_SITE "$site"; st_set PROXY nginx
  if ! nginx -t >/dev/null 2>&1; then warn "nginx -t не прошёл — убираю свой блок"; nginx_remove; return 1; fi
  systemctl reload nginx
  if ! command -v certbot >/dev/null || ! dpkg -l python3-certbot-nginx >/dev/null 2>&1; then
    pkg_installed certbot || st_set CERTBOT_OURS 1
    apt_update; apt_install certbot python3-certbot-nginx
  fi
  if ! certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect >/dev/null 2>&1; then
    warn "certbot не смог получить сертификат"; nginx_remove; return 1
  fi
  st_set CERT_NAME "$DOMAIN"
}
nginx_remove() {
  local site; site=$(st_get NGINX_SITE)
  [[ -n "$(st_get CERT_NAME)" ]] && certbot delete --cert-name "$(st_get CERT_NAME)" --non-interactive >/dev/null 2>&1 || true
  [[ -n "$site" ]] && rm -f "$site" "/etc/nginx/sites-enabled/$APP.conf"
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
  if [[ "$(st_get CERTBOT_OURS)" == 1 ]] && ! ls /etc/letsencrypt/renewal/*.conf >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq certbot python3-certbot-nginx >/dev/null 2>&1 || true
  fi
  sed -i '/^NGINX_SITE=/d;/^CERT_NAME=/d;/^PROXY=/d' "$STATE" 2>/dev/null || true
}

# ------------------------------------------------------------------ Cloudflare quick tunnel
setup_tunnel() {
  say "Использую Cloudflare Quick Tunnel (HTTPS без открытых портов и без влияния на другие сервисы)"
  local arch; arch=$(dpkg --print-architecture)
  case "$arch" in amd64|arm64|arm|386) ;; armhf) arch=arm ;; i386) arch=386 ;; *) die "архитектура $arch не поддерживается cloudflared" ;; esac
  curl -fsSL -o "$DIR/cloudflared" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
  chmod 755 "$DIR/cloudflared"
  local mport; mport=$(st_get TUNNEL_METRICS_PORT); [[ -n "$mport" ]] || mport=$(free_port 20241)
  st_set TUNNEL_METRICS_PORT "$mport"
  cat > "$TUNIT" <<EOF
[Unit]
Description=$APP Cloudflare quick tunnel
After=network-online.target $APP.service
Wants=network-online.target

[Service]
User=$APP
ExecStart=$DIR/cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:$mport --url http://127.0.0.1:$PORT
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA
Environment=HOME=$DATA

[Install]
WantedBy=multi-user.target
EOF
  conf_set TUNNEL_METRICS "127.0.0.1:$mport"
  conf_set PUBLIC_URL ""
  st_set PROXY tunnel
  systemctl daemon-reload; systemctl enable --now $APP-tunnel >/dev/null 2>&1
  systemctl restart $APP-tunnel
}
tunnel_url() {
  local mport; mport=$(st_get TUNNEL_METRICS_PORT)
  curl -fsS --max-time 4 "http://127.0.0.1:$mport/quicktunnel" 2>/dev/null | python3 -c 'import json,sys;h=json.load(sys.stdin).get("hostname");print("https://"+h if h else "")' 2>/dev/null || true
}

# ------------------------------------------------------------------ firewall
open_ufw() {
  command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active" || return 0
  ufw status | grep -qE "^$1(/tcp)?[[:space:]]+ALLOW" && return 0
  ufw allow "$1"/tcp comment "$APP" >/dev/null && st_set "UFW_$1" 1
}

# ------------------------------------------------------------------ app service
write_unit() {
  cat > "$UNIT" <<EOF
[Unit]
Description=TSLA spread monitor (Lighter vs Jupiter)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP
Group=$APP
EnvironmentFile=$CONF
ExecStart=/usr/bin/python3 -u $DIR/app.py
Restart=always
RestartSec=5
WorkingDirectory=$DATA
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA
MemoryMax=300M

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable $APP >/dev/null 2>&1
  systemctl restart $APP
  for _ in $(seq 1 15); do curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && return 0; sleep 1; done
  journalctl -u $APP -n 30 --no-pager; die "приложение не запустилось"
}

write_conf() {
  umask 077
  cat > "$CONF" <<EOF
# ---- TSLA spread monitor. После правки: sudo $APP restart
WEB_PASSWORD=$WEB_PASSWORD
SESSION_SECRET=$(rand hex 32)
BIND=127.0.0.1
WEB_PORT=$PORT
PUBLIC_URL=
DB_PATH=$DATA/data.db

# Telegram: токен от @BotFather. Чат привязывается сам — отправьте боту /start.
TG_BOT_TOKEN=$TG_BOT_TOKEN
TG_CHAT_ID=

# Начальные значения (дальше меняются в веб-интерфейсе или командами бота)
ALERT_SPREAD_USD=$ALERT_SPREAD_USD
ALERT_SIZE=$ALERT_SIZE
ALERT_COOLDOWN_MIN=15
ALERT_HYSTERESIS=0.10
POSITION_SHARES=24

POLL_SEC=10
SIZES=1,2,3,5,10,24
RETENTION_DAYS=60

# Lighter (Robinhood chain): TSLA perp = market 16
LIGHTER_API=https://api.rh.lighter.xyz
LIGHTER_MARKET_ID=16
LIGHTER_TAKER_FEE=0

# Jupiter: без ключа — lite-api.jup.ag; с ключом (portal.jup.ag) — api.jup.ag
JUP_API_KEY=
TSLAX_MINT=XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB
TSLAX_DECIMALS=8
EOF
  umask 022
  chown root:$APP "$CONF"; chmod 640 "$CONF"
}

# ------------------------------------------------------------------ install
do_install() {
  need_root
  command -v systemctl >/dev/null || die "нужен systemd"
  command -v apt-get >/dev/null || die "поддерживается только Ubuntu/Debian"
  for b in python3 curl ss awk; do command -v $b >/dev/null || { apt_update; apt_install python3 curl ca-certificates iproute2 gawk; break; }; done
  python3 -c 'import sqlite3, ssl' || die "python3 без sqlite3/ssl"

  fetch_source
  id -u $APP >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin $APP

  if [[ -f "$CONF" && -f "$STATE" ]]; then
    say "Уже установлено — обновляю приложение (настройки, история и домен сохраняются)"
    install_files
    PORT=$(conf_get WEB_PORT)
    systemctl restart $APP
    [[ "$(st_get PROXY)" == tunnel ]] && { setup_tunnel_bin_only; systemctl restart $APP-tunnel; }
    say "Обновлено. Адрес: $(cur_url)"
    return 0
  fi

  install_files
  echo; echo "${c_b}Настройка${c_0} (Enter — по умолчанию)"
  ask WEB_PASSWORD "Пароль для веб-интерфейса (Enter — сгенерировать): " "" secret
  [[ -n "${WEB_PASSWORD:-}" ]] || { WEB_PASSWORD=$(rand urlsafe 12); GEN_PASS=1; }
  ask TG_BOT_TOKEN "Токен Telegram-бота от @BotFather (Enter — позже): " ""
  ask ALERT_SPREAD_USD "Порог оповещения, \$ на акцию [0.30]: " "0.30"
  ask ALERT_SIZE "Размер порции для оповещений, шт [5]: " "5"

  PORT=$(free_port 18090)
  write_conf
  st_set PORT "$PORT"
  write_unit
  say "Приложение запущено на 127.0.0.1:$PORT (снаружи не доступно напрямую)"

  # ---- выбираем способ публикации по HTTPS
  local mode="${MODE:-auto}" ip p443 p80 who url=""
  if [[ $mode != tunnel ]]; then
    ip=$(public_ip) || { warn "не удалось узнать внешний IPv4"; mode=tunnel; }
  fi
  if [[ $mode != tunnel ]]; then
    if [[ -z "${DOMAIN:-}" ]]; then
      DOMAIN="tsla-${ip//./-}.sslip.io"
      say "Бесплатный домен: $DOMAIN  (sslip.io → $ip)"
    fi
    if ! getent ahostsv4 "$DOMAIN" | awk '{print $1}' | grep -qx "$ip"; then
      warn "$DOMAIN не указывает на $ip"; [[ $mode == auto ]] && mode=tunnel
    fi
  fi
  if [[ $mode == auto ]]; then
    p443=$(proc_on 443); p80=$(proc_on 80); who=${p443:-$p80}
    say "Порт 443: ${p443:-свободен}, порт 80: ${p80:-свободен}"
    case "$who" in
      "")    if command -v caddy >/dev/null && [[ -f /etc/caddy/Caddyfile ]] && ! pkg_installed nginx; then mode=caddy-existing-stopped; else mode=caddy-new; fi ;;
      caddy) if systemctl is-active --quiet caddy; then mode=caddy-existing; else mode=tunnel; fi ;;
      nginx) mode=nginx ;;
      *)     warn "80/443 заняты процессом '$who' (не Caddy/nginx) — не вмешиваюсь"; mode=tunnel ;;
    esac
  fi
  [[ $mode == caddy ]] && { systemctl is-active --quiet caddy && mode=caddy-existing || mode=caddy-new; }

  st_set DOMAIN "${DOMAIN:-}"
  case "$mode" in
    caddy-existing) setup_caddy_existing || mode=tunnel ;;
    caddy-existing-stopped) warn "Caddy установлен, но остановлен — не трогаю его"; mode=tunnel ;;
    caddy-new) setup_caddy_new || { warn "не удалось настроить Caddy"; remove_proxy; mode=tunnel; } ;;
    nginx) setup_nginx || mode=tunnel ;;
  esac

  if [[ $mode != tunnel ]]; then
    url="https://$DOMAIN"
    say "Жду выпуск сертификата и проверяю $url ..."
    if wait_https "$url" 150; then
      conf_set PUBLIC_URL "$url"; systemctl restart $APP
    else
      warn "HTTPS на $DOMAIN не заработал (порт 80/443 закрыт у провайдера или лимит Let's Encrypt) — откатываю и включаю туннель"
      remove_proxy; mode=tunnel
    fi
  fi
  if [[ $mode == tunnel ]]; then
    setup_tunnel
    systemctl restart $APP
    say "Жду адрес туннеля..."
    for _ in $(seq 1 30); do url=$(tunnel_url); [[ -n "$url" ]] && break; sleep 2; done
    [[ -n "$url" ]] && wait_https "$url" 60 || true
  fi

  echo
  echo "${c_b}Готово.${c_0}"
  echo "  Веб-интерфейс:  ${c_b}${url:-(адрес туннеля появится через минуту: sudo $APP url)}${c_0}"
  if [[ -n "${GEN_PASS:-}" ]]; then echo "  Пароль:         ${c_b}$WEB_PASSWORD${c_0}   ← сохраните"; fi
  if [[ -n "$TG_BOT_TOKEN" ]]; then echo "  Telegram:       откройте своего бота и отправьте /start — чат привяжется сам"
  else echo "  Telegram:       sudo $APP config  → вписать TG_BOT_TOKEN"; fi
  [[ $mode == tunnel ]] && echo "  Примечание:     адрес *.trycloudflare.com меняется при перезапуске — новый бот пришлёт в Telegram"
  echo "  Управление:     sudo $APP status | logs | url | config | password | update"
  echo "  Удалить всё:    ${c_b}sudo $APP uninstall${c_0}"
}
setup_tunnel_bin_only() {
  local arch; arch=$(dpkg --print-architecture); [[ $arch == armhf ]] && arch=arm; [[ $arch == i386 ]] && arch=386
  curl -fsSL -o "$DIR/cloudflared" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch" && chmod 755 "$DIR/cloudflared"
}
cur_url() {
  if [[ "$(st_get PROXY)" == tunnel ]]; then tunnel_url; else conf_get PUBLIC_URL; fi
}

# ------------------------------------------------------------------ uninstall
remove_proxy() {
  case "$(st_get PROXY)" in
    caddy-existing)
      local f; f=$(st_get CADDYFILE)
      if [[ -n "$f" && -f "$f" ]]; then
        strip_block "$f"
        # убрать пустые хвостовые строки, которые мы добавили
        sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$f"
        caddy validate --config "$f" --adapter caddyfile >/dev/null 2>&1 && systemctl reload caddy || warn "проверьте Caddy: $f"
      fi ;;
    caddy-ours)
      systemctl disable --now caddy >/dev/null 2>&1 || true
      DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq caddy >/dev/null 2>&1 || true
      rm -rf /etc/caddy /var/lib/caddy ;;
    nginx) nginx_remove ;;
    tunnel)
      systemctl disable --now $APP-tunnel >/dev/null 2>&1 || true
      rm -f "$TUNIT"; systemctl daemon-reload ;;
  esac
  if [[ "$(st_get CADDY_OURS)" == 1 && "$(st_get PROXY)" != caddy-ours ]]; then
    systemctl disable --now caddy >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq caddy >/dev/null 2>&1 || true
    rm -rf /etc/caddy /var/lib/caddy
  fi
  for p in 80 443; do
    if [[ "$(st_get UFW_$p)" == 1 ]]; then ufw delete allow "$p"/tcp >/dev/null 2>&1 || true; fi
  done
  sed -i '/^PROXY=/d;/^CADDY_OURS=/d;/^UFW_80=/d;/^UFW_443=/d' "$STATE" 2>/dev/null || true
}

do_uninstall() {
  need_root
  [[ -f "$STATE" || -f "$UNIT" || -d "$DIR" ]] || { say "$APP не установлен"; return 0; }
  say "Удаляю $APP"
  systemctl disable --now $APP >/dev/null 2>&1 || true
  remove_proxy
  systemctl disable --now $APP-tunnel >/dev/null 2>&1 || true
  rm -f "$UNIT" "$TUNIT"; systemctl daemon-reload
  rm -rf "$DIR" "$DATA" "$ETC"
  id -u $APP >/dev/null 2>&1 && { userdel $APP 2>/dev/null || true; }
  rm -f "$BIN"
  say "Удалено полностью: сервис, файлы, история, настройки, блок в прокси, правила ufw."
}

# ------------------------------------------------------------------ main
case "${1:-install}" in
  install)          do_install ;;
  update)           do_install ;;
  uninstall|remove) do_uninstall ;;
  status)  systemctl status $APP --no-pager -l | head -15 || true
           [[ "$(st_get PROXY)" == tunnel ]] && systemctl status $APP-tunnel --no-pager | head -5 || true
           echo "Прокси: $(st_get PROXY)   Адрес: $(cur_url)" ;;
  url)     cur_url ;;
  logs)    journalctl -u $APP -u $APP-tunnel -f -n 100 ;;
  restart) need_root; systemctl restart $APP; say "перезапущен" ;;
  config)  need_root; ${EDITOR:-nano} "$CONF"; systemctl restart $APP; say "перезапущен с новым конфигом" ;;
  password) need_root; NEWPW=""; ask NEWPW "Новый пароль: " "" secret; [[ -n "$NEWPW" ]] || die "пустой пароль"
           conf_set WEB_PASSWORD "$NEWPW"; conf_set SESSION_SECRET "$(rand hex 32)"
           systemctl restart $APP; say "пароль изменён" ;;
  *) echo "использование: sudo $APP {install|update|uninstall|status|url|logs|restart|config|password}"; exit 1 ;;
esac
