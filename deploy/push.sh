#!/usr/bin/env bash
# Copy this checkout to another host and restart instantclip there.
#
#   deploy/push.sh [user@]host [remote dir]        (remote dir: relative to the remote home, default instantclip)
#   deploy/push.sh --force ...                       restart even while that server records or encodes
#
# The remote keeps its own config.json, venv, logs and runtime state (state.json, sessions.json,
# probe_cache.json); everything else mirrors this checkout, .git included (the UI shows its commit time).
# Needs key-based ssh (ssh-copy-id [user@]host). The first push to a host starts from config.example.json:
# set paths etc. in <remote dir>/config.json there, then push again (or restart).
set -euo pipefail

force=0
if [ "${1:-}" = "--force" ]; then force=1; shift; fi
target=${1:?"usage: deploy/push.sh [--force] [user@]host [remote dir]"}
dir=${2:-instantclip}
cd "$(dirname "$0")/.."
if [ ! -d .git ]; then   # a git worktree has a .git file pointing elsewhere: the remote would lose its history
  echo "push from the main checkout (this one has no .git directory)" >&2
  exit 1
fi

ssh_=(ssh -o BatchMode=yes -o LogLevel=ERROR "$target")
remote() { "${ssh_[@]}" "cd ~ && $*"; }

# never mirror (with --delete) into a directory that is something else
state=$(remote "if [ ! -e '$dir' ]; then echo new; elif [ -f '$dir/run.sh' ] && [ -f '$dir/app/main.py' ]; then echo ok; else echo other; fi")
if [ "$state" = other ]; then
  echo "$target:~/$dir exists and is not an instantclip checkout: not touching it" >&2
  exit 1
fi
port=8090
if [ "$state" = ok ]; then
  port=$(remote "cd '$dir' && python3 -c 'import json; print(json.load(open(\"config.json\")).get(\"http\", {}).get(\"port\", 8090))' 2>/dev/null || echo 8090")
  # recording / encoding / proxy jobs would be cut off by the restart
  busy=$(remote "curl -s -m 5 http://127.0.0.1:$port/api/status" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit()
rec = [r["device"] for r in d.get("recorders", []) if r.get("mode") != "idle"]
jobs = [j["name"] for j in d.get("jobs", []) if j.get("state") in ("queued", "waiting", "running")]
out = (["recording: " + ", ".join(rec)] if rec else []) + (["subclip jobs: %d" % len(jobs)] if jobs else []) + \
      (["proxy jobs: %d" % len(d["proxies"])] if d.get("proxies") else [])
print("; ".join(out))')
  if [ -n "$busy" ] && [ $force = 0 ]; then
    echo "$target:$port is busy ($busy). Not restarting; use --force to do it anyway." >&2
    exit 1
  fi
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "note: uncommitted changes in this checkout are copied too" >&2
fi
reqs_before=$(remote "sha1sum '$dir/requirements.txt' 2>/dev/null | cut -c1-40" || true)

echo "copying $(git log -1 --format='%h %s') to $target:~/$dir"
rsync -az --delete \
  --exclude .venv --exclude logs --exclude __pycache__ --exclude config.json \
  --exclude state.json --exclude sessions.json --exclude probe_cache.json --exclude '*.json.tmp' \
  --exclude 'wasm/build' \
  -e "ssh -o BatchMode=yes -o LogLevel=ERROR" ./ "$target:$dir/"

if [ "$(sha1sum requirements.txt | cut -c1-40)" != "$reqs_before" ] || ! remote "test -x '$dir/.venv/bin/python'"; then
  echo "installing requirements"
  remote "cd '$dir' && { [ -x .venv/bin/python ] || python3 -m venv .venv; } && .venv/bin/pip install -q -r requirements.txt"
fi

echo "restarting instantclip on $target:$port"
if remote "systemctl is-active -q instantclip 2>/dev/null"; then
  remote "sudo -n systemctl restart instantclip" || {
    echo "instantclip runs as a systemd service there: restart it with 'sudo systemctl restart instantclip'" >&2
    exit 1
  }
else
  remote "cd '$dir' && mkdir -p logs
    pid=\$(ss -ltnp | grep ':$port ' | grep -o 'pid=[0-9]*' | cut -d= -f2 | head -1)
    if [ -n \"\$pid\" ]; then
      kill -INT \$pid
      for i in \$(seq 120); do kill -0 \$pid 2>/dev/null || break; sleep 0.5; done
    fi
    setsid nohup ./run.sh >> logs/run.out 2>&1 < /dev/null &"
fi

# up again, on the commit just copied
for _ in $(seq 60); do
  info=$(remote "curl -s -m 2 http://127.0.0.1:$port/api/config" 2>/dev/null || true)
  [ -n "$info" ] && break
  sleep 1
done
if [ -z "$info" ]; then
  echo "instantclip did not come up on $target:$port; see ~/$dir/logs/run.out there" >&2
  exit 1
fi
echo "$info" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("running on %s, commit %s" % (d.get("hostname", "?"), d.get("commit_time", "?")))'
host=${target#*@}
echo "http://$host:$port/"
