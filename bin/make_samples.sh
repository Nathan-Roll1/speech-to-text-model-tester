#!/usr/bin/env bash
# Generate synthetic dictation samples (numbers / tickers / code identifiers)
# with macOS `say` for QUALITATIVE formatting tests only.
# Never compute WER from these — see docs/PITFALLS.md §2.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p samples && cd samples

cat > trading.txt <<'EOF'
Trading update for Tuesday. We deployed nineteen new streams this morning, bringing total slot utilization to eighty four percent. Bitcoin leads the board at twelve point four percent over twenty four hours, with volume near one point eight billion dollars. Ethereum is the laggard, down three point one percent, and the stop loss on E T H U S D T was hit at four thousand two hundred dollars.
EOF

cat > coding.txt <<'EOF'
Okay, next task. Open speak brief dot t s and add a debounce of two hundred milliseconds to the fetch raw signal callback. Make sure the abort controller is passed into the planner call, and log the planner latency in milliseconds.
EOF

cat > dates.txt <<'EOF'
Quick check. The deadline is March third, twenty twenty six, at five p m sharp. Budget is forty seven thousand dollars.
EOF

for s in trading coding dates; do
  /usr/bin/say -v Samantha -o "$s.aiff" < "$s.txt"
  afconvert -f WAVE -d LEI16@16000 "$s.aiff" "$s.wav"
  echo "$s.wav: $(afinfo "$s.wav" | grep 'estimated duration' | grep -o '[0-9.]*') s"
done
echo "samples in $(pwd)"
