#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"
SESSION="fsub"

# Kalau sudah ada session, kasih info cara attach
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "✅ Session '$SESSION' sudah jalan."
  echo "Attach: tmux attach -t $SESSION"
  echo "Stop  : tmux kill-session -t $SESSION"
  exit 0
fi

# Mulai session baru dan jalankan bot
tmux new-session -d -s "$SESSION" "bash -lc '
source .venv/bin/activate
export PYTHONUNBUFFERED=1
python main.py 2>&1 | tee -a bot.log
'"

echo "🚀 Bot started in tmux session: $SESSION"
echo "➤ Attach: tmux attach -t $SESSION"
echo "➤ Stop  : tmux kill-session -t $SESSION"
