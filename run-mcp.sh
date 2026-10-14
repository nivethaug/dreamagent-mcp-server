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
mkdir -p "$CG" 2>/dev/null || true
if ! echo $$ > "$CG/cgroup.procs" 2>/dev/null; then
  echo "WARN: could not join cgroup $CG — egress rules inactive for this process" >&2
fi
cd "$(dirname "$0")"
exec ./.venv/bin/python server.py --http --host 127.0.0.1 --port 8800
