#!/bin/bash
# Launcher for pm2: joins the dedicated cgroup used by the egress firewall
# (see /usr/local/sbin/mcp-egress-rules.sh + mcp-egress-block.service on the
# host) and then execs the MCP server. Chromium children inherit the cgroup,
# so the screenshot tool can never reach internal network ranges even via
# redirects or subresources.
#
# pm2 usage:
#   pm2 start run-mcp.sh --name dreamagent-mcp
CG=/sys/fs/cgroup/dreamagent-mcp-egress
if ! systemctl is-active --quiet mcp-egress-block.service; then
  echo "ERROR: egress firewall service mcp-egress-block.service is not active; refusing to start" >&2
  exit 1
fi
if [[ ! -d "$CG" || ! -w "$CG/cgroup.procs" ]]; then
  echo "ERROR: required egress cgroup $CG is missing or not writable; refusing to start" >&2
  exit 1
fi
if ! printf '%s\n' "$$" > "$CG/cgroup.procs" 2>/dev/null; then
  echo "ERROR: could not join required egress cgroup $CG; refusing to start" >&2
  exit 1
fi
cd "$(dirname "$0")"
exec ./.venv/bin/python server.py --http --host 127.0.0.1 --port 8800
