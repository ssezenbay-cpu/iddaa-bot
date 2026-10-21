#!/usr/bin/env bash
# iddaa botu kurulum: sanal ortam, paketler, .env, systemd servisi.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
cd "$DIR"

echo "==> Sistem paketleri"
if command -v apt-get >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3 python3-venv python3-pip >/dev/null
fi
PYV=$(python3 -c 'import sys; print(sys.version_info >= (3,10))')
if [ "$PYV" != "True" ]; then echo "Python 3.10+ gerekli"; exit 1; fi

echo "==> Sanal ortam ve paketler"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  read -rp "Telegram bot token (BotFather): " TOK
  read -rp "Gemini API anahtarı (boş geçebilirsin): " GEM
  read -rp "Kasa tutarı TL [1000]: " BR
  sed -i "s|^TELEGRAM_TOKEN=.*|TELEGRAM_TOKEN=${TOK}|" .env
  sed -i "s|^GEMINI_API_KEY=.*|GEMINI_API_KEY=${GEM}|" .env
  sed -i "s|^BANKROLL=.*|BANKROLL=${BR:-1000}|" .env
  chmod 600 .env
fi

echo "==> Testler"
.venv/bin/pip install -q pytest >/dev/null
.venv/bin/python -m pytest -q tests/ || { echo "Testler başarısız, kurulum durdu"; exit 1; }

echo "==> systemd servisi"
sed -e "s|__USER__|${USER_NAME}|" -e "s|__DIR__|${DIR}|g" deploy/iddaa-bot.service | sudo tee /etc/systemd/system/iddaa-bot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now iddaa-bot
sleep 3
sudo systemctl --no-pager status iddaa-bot | head -5
echo
echo "Kurulum tamam. Telegram'da botuna /start yaz, sonra /teshis ile kaynakları kontrol et."
echo "Loglar: sudo journalctl -u iddaa-bot -f"
