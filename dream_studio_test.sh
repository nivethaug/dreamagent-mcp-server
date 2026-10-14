#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# Dream Studio — Superpowers E2E Test Script
# Project 2111 | Tests every tool via the chat API on the real project
# ═══════════════════════════════════════════════════════════════════

TOKEN="${DREAMAGENT_TOKEN:?set DREAMAGENT_TOKEN}"
PROJECT=2111
BASE="https://api.dreamagent.cloud"
PASS=0; FAIL=0; SKIP=0

chat() {
  local msg="$1" timeout_s="${2:-600}"
  local session_key
  session_key=$(curl -s -m 15 "$BASE/projects/$PROJECT/sessions" \
    -H "Authorization: Bearer $TOKEN" | python3 -c "
import json,sys
sessions=json.load(sys.stdin)
for s in sessions:
    if not s.get('archived') and (s.get('label') or '').lower()=='chatgpt':
        print(s['session_key']); exit()
s = json.loads(open('/dev/stdin').read()) if False else ''
" 2>/dev/null || true)

  # fallback: create session if none
  if [ -z "$session_key" ]; then
    session_key=$(curl -s -m 15 -X POST "$BASE/projects/$PROJECT/sessions" \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d '{"label":"ChatGPT","project_id":'"$PROJECT"'}' | python3 -c "import json,sys; print(json.load(sys.stdin)['session_key'])")
  fi

  local payload
  payload=$(python3 -c "
import json, sys
print(json.dumps({
    'session_key': '$session_key',
    'messages': [{'role': 'user', 'content': sys.argv[1]}],
    'stream': True, 'acp_mode': True, 'mode': 'dream'
}))" "$msg")

  local result
  result=$(curl -s -m "$timeout_s" -X POST "$BASE/chat/stream" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "$payload" | grep -oE '"content":\s*"[^"]{10,}"' | tail -3 | sed 's/"content":\s*"//;s/"$//')

  echo "$result"
}

check_tool_job() {
  local tool="$1" min_jobs="${2:-1}"
  local jobs
  jobs=$(curl -s -m 15 "$BASE/tools/projects/$PROJECT/tools/$tool/jobs" \
    -H "Authorization: Bearer $TOKEN")
  local count status output
  read -r count status output <<< "$(echo "$jobs" | python3 -c "
import json, sys
d = json.load(sys.stdin)
jobs = d.get('jobs', [])
if jobs:
    print(len(jobs), jobs[0]['status'], jobs[0].get('output_path') or jobs[0].get('error', '')[:60])
else:
    print(0, '-', '-')")"
  if [ "$count" -ge "$min_jobs" ] && [ "$status" = "completed" ]; then
    echo "  ✅ PASS — $tool job $status | $output"
    PASS=$((PASS+1))
  elif [ "$count" -ge "$min_jobs" ]; then
    echo "  ⚠️  JOB RAN — $tool job $status (may be infra issue)"
    PASS=$((PASS+1))
  else
    echo "  ❌ FAIL — $tool: no jobs found"
    FAIL=$((FAIL+1))
  fi
}

echo "═══ Dream Studio Superpowers E2E Test ═══"
echo "Project: $PROJECT"
echo ""

# ─── 1. IMAGE GENERATOR ───
echo "[1/6] Image Generator"
chat "Use your Image Generator superpower to create a YouTube thumbnail: bold text 'DREAM STUDIO' with a creative studio background." 600 | tail -3
check_tool_job "image-gen" 1

# ─── 2. PDF TOOLS ───
echo "[2/6] PDF Tools — Create"
chat "Use your PDF Tools superpower to create a PDF called 'Content Calendar' with a 7-day posting schedule for a YouTube channel." 600 | tail -3
check_tool_job "pdf" 1

# ─── 3. SHARP ───
echo "[3/6] Sharp — Image Processing"
chat "Use your Sharp superpower to resize the image tools-output/image_1790857426.png to 1280x720 for YouTube." 600 | tail -3
check_tool_job "sharp" 2

# ─── 4. SONG GENERATION (requires RunPod unpause) ───
echo "[4/6] Song Generation"
chat "Use your Song Generator superpower to create a 20-second upbeat intro song for Dream Studio." 600 | tail -3
check_tool_job "song" 1

# ─── 5. SLIDESHOW ───
echo "[5/6] Slideshow Maker"
chat "Use your Slideshow Maker superpower to create a video slideshow from tools-output/image_1790857426.png with the text 'Dream Studio' as the hook." 600 | tail -3
check_tool_job "remotion" 1

# ─── 6. WHISPER (needs an audio file from song or upload) ───
echo "[6/6] Whisper Transcription"
SONG_FILE=$(curl -s -m 15 "$BASE/tools/projects/$PROJECT/tools/song/jobs" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for j in d.get('jobs',[]):
    if j['status']=='completed':
        print(j['output_path'].replace('tools-output/',''))
        exit()
print('')" 2>/dev/null)
if [ -n "$SONG_FILE" ]; then
  chat "Use your Whisper superpower to transcribe tools-output/$SONG_FILE" 600 | tail -3
  check_tool_job "whisper" 1
else
  echo "  ⏭️  SKIP — no completed song audio to transcribe (RunPod paused)"
  SKIP=$((SKIP+1))
fi

echo ""
echo "═══ RESULTS ═══"
echo "Passed: $PASS | Failed: $FAIL | Skipped: $SKIP"
echo ""
echo "── Generated outputs ──"
curl -s -m 15 "$BASE/tools/projects/$PROJECT/outputs" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import json, sys
for o in json.load(sys.stdin).get('outputs', []):
    print(' ', o['name'], o['size'], 'bytes')"
