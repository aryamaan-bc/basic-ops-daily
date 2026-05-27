#!/usr/bin/env python3
"""OPS Daily Summary script — self-contained.

Reads LINEAR_API_KEY and INTERCOM_API_KEY from environment, queries Linear and
Intercom directly, renders the v4-style daily summary to /tmp/ops_message.txt
and a summary JSON to /tmp/ops_summary.json. No Slack send here — the caller
posts /tmp/ops_message.txt to the desired Slack destination.

Filters:
- Team: OPS
- Master tickets only (no sub-issues)
- Active statuses (Todo, In Progress, Waiting for {customer,vendor,settlement}, Blocked)
- Excludes long-term projects (Bigger projects, Outstanding customer issues,
  Rebalance Operations, Corrective contributions, New features to be added)

Classifies tickets as "OPS" (taylor/will/aryamaan/daniel) vs "Engineering"
(everyone else) using the assignee, falling back to creator when unassigned.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except ImportError:
    ET = timezone(timedelta(hours=-4))

LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY")
INTERCOM_API_KEY = os.environ.get("INTERCOM_API_KEY")

OPS_TEAM = {"taylor", "will", "aryamaan", "daniel"}
EP = {
    "Bigger projects",
    "Outstanding customer issues",
    "Rebalance Operations",
    "Corrective contributions",
    "New features to be added",
}
AS = ["Todo", "In Progress", "Waiting for customer",
      "Waiting for vendor", "Waiting for settlement", "Blocked"]
LIST_BALL = ["KYC Failure", "Transfer Out Requests", "Onboarding Tasks",
             "Annual Notices & SPDs", "Inbound Rollovers", "MBDR Enablement",
             "IRA Contributions", "Trading Operations"]
COUNT_ONLY = ["Customer Money Issues", "Plan Conversion / Migration Operations",
              "Pooled Fund Migration Operational Tasks", "SP SIO Migration Ops",
              "Abandoned Onboardings"]
HYGIENE = ["customer service requests", "Daily Status Report Issues"]
RULE = "──────────────────────────────────────────────────"


# ---------- HTTP helpers ----------

def linear(query, variables=None):
    if not LINEAR_API_KEY:
        raise RuntimeError("LINEAR_API_KEY missing in env")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request("https://api.linear.app/graphql",
                                  data=body, method="POST")
    req.add_header("Authorization", LINEAR_API_KEY)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    if payload.get("errors"):
        raise RuntimeError(f"Linear errors: {payload['errors']}")
    return payload["data"]


def intercom(method, path, body=None):
    if not INTERCOM_API_KEY:
        return None
    url = f"https://api.intercom.io{path}"
    h = {"Authorization": f"Bearer {INTERCOM_API_KEY}",
         "Content-Type": "application/json", "Accept": "application/json"}
    data = json.dumps(body).encode() if body else None
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=h, method=method),
            timeout=30,
        ) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  intercom {type(e).__name__}: {e}", file=sys.stderr)
        return None


# ---------- Field accessors ----------

def parse_iso(s):
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def first_lower(name):
    if not name:
        return ""
    return name.split("@")[0].split()[0].lower()


def asg_lower(i):
    a = i.get("assignee") or {}
    return first_lower(a.get("displayName") or a.get("name") or "")


def asg_cap(i):
    f = asg_lower(i)
    if not f:
        return "Unassigned"
    return f[:1].upper() + f[1:]


def creator_lower(i):
    c = i.get("creator") or {}
    return first_lower(c.get("name") or c.get("email") or "")


def bucket(i):
    a = asg_lower(i)
    if a:
        return "ops" if a in OPS_TEAM else "platform"
    c = creator_lower(i)
    if c and c not in OPS_TEAM:
        return "platform"
    return "ops"


def eng_label(i):
    a = asg_lower(i)
    if a and a not in OPS_TEAM:
        return a[:1].upper() + a[1:]
    c = creator_lower(i)
    if c and c not in OPS_TEAM:
        return c[:1].upper() + c[1:] + " [created]"
    return "Unassigned"


def proj(i):
    return (i.get("project") or {}).get("name")


def state_name(i):
    return (i.get("state") or {}).get("name") or ""


def age_days(i, now):
    d = parse_iso(i.get("createdAt") or "")
    return (now - d).days if d else 0


def link(i):
    url = i.get("url") or ""
    ident = i.get("identifier") or ""
    return f"<{url}|{ident}>" if url else ident


# ---------- Linear queries ----------

ISSUES_QUERY = """
query($filter: IssueFilter!, $first: Int!, $after: String) {
  issues(filter: $filter, first: $first, after: $after) {
    nodes {
      identifier title url createdAt updatedAt
      priority priorityLabel
      state { name type }
      assignee { displayName name }
      creator { name email }
      project { name }
      parent { id }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def fetch_issues(filt):
    out = []
    after = None
    while True:
        d = linear(ISSUES_QUERY, {"filter": filt, "first": 100, "after": after})
        page = d["issues"]
        out.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return out


# ---------- Intercom needs-response ----------

ACK_PHRASES = [
    "thanks", "thank you", "sounds good", "got it", "perfect", "great", "awesome",
    "ok thanks", "appreciate it", "will do", "no worries", "all good",
    "have a great day", "have a good day", "apologies for the confusion",
    "sorry for the confusion", "understood", "makes sense", "that works",
    "no problem", "noted", "all set", "good to know", "that helps",
    "no further questions", "nothing else", "that's all", "cheers",
    "much appreciated", "glad to hear",
]


def intercom_needs():
    if not INTERCOM_API_KEY:
        return []
    convos, cursor = [], None
    seven = int(time.time()) - 7 * 24 * 3600
    while True:
        body = {"query": {"operator": "AND", "value": [
            {"field": "state", "operator": "=", "value": "open"},
            {"field": "updated_at", "operator": ">", "value": seven},
        ]}, "pagination": {"per_page": 25}}
        if cursor:
            body["pagination"]["starting_after"] = cursor
        r = intercom("POST", "/conversations/search", body)
        if not r:
            break
        convos.extend(r.get("conversations", []))
        nxt = r.get("pages", {}).get("next")
        if nxt and nxt.get("starting_after"):
            cursor = nxt["starting_after"]
        else:
            break
    waiting = []
    for c in convos:
        ws = c.get("waiting_since")
        a = c.get("source", {}).get("author", {})
        if (a.get("email") or "").lower().endswith("@basiccapital.com"):
            continue
        if ws and a.get("type") == "user":
            waiting.append(c)
    needs = []
    for c in waiting:
        cid = c.get("id")
        ws = c.get("waiting_since")
        wh = round((time.time() - ws) / 3600, 0)
        name = c.get("source", {}).get("author", {}).get("name", "Unknown")
        detail = intercom("GET", f"/conversations/{cid}")
        if not detail:
            continue
        parts = detail.get("conversation_parts", {}).get("conversation_parts", [])
        um = [p for p in parts if p.get("author", {}).get("type") == "user"]
        if not um:
            msg = re.sub(r"<[^>]+>", " ",
                         c.get("source", {}).get("body", "") or "").strip()[:150]
        else:
            msg = re.sub(r"<[^>]+>", " ", (um[-1].get("body") or "")).strip()[:150]
        if not msg or any(p in msg.lower() for p in ACK_PHRASES) or wh >= 168:
            continue
        needs.append({
            "customer_name": name,
            "wait_hours": int(wh),
            "last_message": msg,
            "conversation_id": cid,
            "intercom_url": f"https://app.intercom.com/a/inbox/k7w32l2g/inbox/shared/all/conversation/{cid}",
        })
    needs.sort(key=lambda x: -x["wait_hours"])
    return needs


# ---------- Rendering ----------

def render_todos(issues, now):
    todos = [i for i in issues if state_name(i) == "Todo"]
    if not todos:
        return []
    ops_t = [i for i in todos if bucket(i) == "ops"]
    plat_t = [i for i in todos if bucket(i) == "platform"]
    lines = []
    if ops_t:
        lines.append("  *Todos:*")
        for t in sorted(ops_t, key=lambda x: -age_days(x, now)):
            lines.append(f"    • {link(t)} ({age_days(t, now)}d, {asg_cap(t)}): {t['title']}")
    if plat_t:
        plat_by = Counter(eng_label(i) for i in plat_t)
        breakdown = ", ".join(f"{c} {who}" for who, c in plat_by.most_common())
        lines.append(f"  *Engineering Todos ({len(plat_t)}):* {breakdown}")
    return lines


def render_inprogress(issues, now):
    if not issues:
        return []
    ops_ip = [i for i in issues if bucket(i) == "ops"]
    plat_ip = [i for i in issues if bucket(i) == "platform"]
    lines = [f"  *In Progress ({len(issues)}):*"]
    by_a = defaultdict(list)
    for i in ops_ip:
        by_a[asg_cap(i)].append(i)
    for who in sorted(by_a, key=lambda k: -len(by_a[k])):
        items = sorted(by_a[who], key=lambda x: -age_days(x, now))
        top = items[:3]
        rest = items[3:]
        top_str = ", ".join(f"{link(i)} ({age_days(i, now)}d)" for i in top)
        if rest:
            top_str += f" + {len(rest)} more (oldest {max(age_days(i, now) for i in rest)}d)"
        lines.append(f"    • {who} ({len(items)}): {top_str}")
    if plat_ip:
        plat_by = Counter(eng_label(i) for i in plat_ip)
        breakdown = ", ".join(f"{c} {who}" for who, c in plat_by.most_common())
        lines.append(f"    • Engineering ({len(plat_ip)}): {breakdown}")
    return lines


def render_project(name, issues, now, mode):
    by_state = Counter(state_name(i) for i in issues)
    todos = [i for i in issues if state_name(i) == "Todo"]
    ops_t = sum(1 for t in todos if bucket(t) == "ops")
    eng_t = sum(1 for t in todos if bucket(t) == "platform")
    parts = []
    if by_state.get("Todo"):
        parts.append(f"{by_state['Todo']} Todo ({ops_t} OPS, {eng_t} Eng)")
    short = {"In Progress": "IP", "Waiting for customer": "WfC",
             "Waiting for vendor": "WfV", "Waiting for settlement": "WfS",
             "Blocked": "Blocked"}
    for s in ("In Progress", "Waiting for customer", "Waiting for vendor",
              "Waiting for settlement", "Blocked"):
        if by_state.get(s):
            parts.append(f"{by_state[s]} {short[s] if mode == 'list_ball' else s}")
    lines = [RULE, f"*{name}* ({len(issues)} active): {', '.join(parts)}"]
    # Transfer Out Requests gets per-ticket enumeration grouped by state,
    # showing every ticket (incl. engineering) with age + assignee + title.
    if name == "Transfer Out Requests":
        state_order = ("Todo", "In Progress", "Waiting for customer",
                       "Waiting for vendor", "Waiting for settlement", "Blocked")
        for state in state_order:
            in_state = sorted(
                [i for i in issues if state_name(i) == state],
                key=lambda x: -age_days(x, now),
            )
            if not in_state:
                continue
            lines.append(f"  *{state} ({len(in_state)}):*")
            for t in in_state:
                lines.append(f"    • {link(t)} ({age_days(t, now)}d, {asg_cap(t)}): {t['title']}")
        return lines
    lines.extend(render_todos(issues, now))
    if mode == "list_ball":
        ip = [i for i in issues if state_name(i) == "In Progress"]
        lines.extend(render_inprogress(ip, now))
        for status in ("Waiting for customer", "Waiting for vendor",
                       "Waiting for settlement", "Blocked"):
            n = by_state.get(status, 0)
            if n:
                lines.append(f"  *{status}:* {n}")
    return lines


# ---------- Main ----------

def main():
    now = datetime.now(timezone.utc)
    today = now.astimezone(ET).strftime("%Y-%m-%d")

    active = fetch_issues({
        "team": {"key": {"eq": "OPS"}},
        "state": {"name": {"in": AS}},
    })
    kept = [i for i in active if proj(i) not in EP and not i.get("parent")]
    print(f"Pulled {len(active)} active, kept {len(kept)} master non-excluded",
          file=sys.stderr)

    by_status = Counter(state_name(i) for i in kept)
    by_proj = defaultdict(list)
    for i in kept:
        by_proj[proj(i)].append(i)
    todo_ops = sum(1 for t in kept if state_name(t) == "Todo" and bucket(t) == "ops")
    todo_plat = sum(1 for t in kept if state_name(t) == "Todo" and bucket(t) == "platform")

    blocked_all = fetch_issues({
        "team": {"key": {"eq": "OPS"}},
        "state": {"name": {"eq": "Blocked"}},
    })
    bex = sum(1 for b in blocked_all if proj(b) in EP and not b.get("parent"))

    urgent = fetch_issues({
        "team": {"key": {"eq": "OPS"}},
        "priority": {"eq": 1},
        "state": {"type": {"in": ["unstarted", "started", "backlog", "triage"]}},
    })
    urgent = [i for i in urgent if proj(i) not in EP and not i.get("parent")]

    needs = intercom_needs()
    longest = f"{needs[0]['customer_name']}, {needs[0]['wait_hours']}h" if needs else "none"

    top_str = ", ".join(p for p, _ in Counter(
        proj(i) or "(no project)" for i in kept
    ).most_common(3))
    narrative = (
        f"The active master queue is at {len(kept)} issues "
        f"({by_status.get('Todo', 0)} Todo, {by_status.get('In Progress', 0)} In Progress, "
        f"{by_status.get('Waiting for customer', 0)} waiting on customer). "
        f"OPS owns {todo_ops} Todos; {todo_plat} sit with Engineering. "
        f"Top by volume: {top_str}. "
        f"{len(urgent)} urgent items; {len(needs)} customers waiting in Intercom "
        f"(longest: {longest})."
    )

    lines = [f"*OPS Daily Summary, {today}*", "", narrative, "", "*Status Breakdown*",
             f"• Todo: {by_status.get('Todo', 0)} ({todo_ops} OPS, {todo_plat} Engineering)"]
    for s in ("Waiting for customer", "In Progress", "Waiting for vendor",
              "Waiting for settlement", "Blocked"):
        n = by_status.get(s, 0)
        suffix = f" (+{bex} in excluded projects)" if s == "Blocked" else ""
        lines.append(f"• {s}: {n}{suffix}")
    lines.append("")

    if needs:
        lines.append(f"*Customers Waiting on Us ({len(needs)})*")
        for n in needs:
            msg = n["last_message"][:140].replace("\n", " ")
            cid = n.get("conversation_id")
            url = n.get("intercom_url") or (
                f"https://app.intercom.com/a/inbox/k7w32l2g/inbox/shared/all/conversation/{cid}"
                if cid else None
            )
            name_link = f"<{url}|{n['customer_name']}>" if url else n["customer_name"]
            lines.append(f"• *{name_link}*, {n['wait_hours']}h: \"{msg}\"")
        lines.append("")

    if urgent:
        lines.append(f"*Urgent ({len(urgent)})*")
        for u in sorted(urgent, key=lambda x: -age_days(x, now)):
            p = proj(u) or "(no project)"
            lines.append(f"• {link(u)} ({age_days(u, now)}d, {asg_cap(u)}) [{p}]: {u.get('title','')}")
        lines.append("")

    lines.append("*By Project*")
    lines.append("")
    for p in LIST_BALL:
        items = by_proj.get(p, [])
        if items:
            lines.extend(render_project(p, items, now, "list_ball"))
            lines.append("")
    for p in COUNT_ONLY:
        items = by_proj.get(p, [])
        if items:
            lines.extend(render_project(p, items, now, "count_only"))
            lines.append("")

    hygiene = defaultdict(list)
    for name in HYGIENE:
        for i in by_proj.get(name, []):
            hygiene[("recat", name)].append(i)
    for i in by_proj.get(None, []):
        hygiene[("noproj", "(no project)")].append(i)
    if hygiene:
        lines.append("*Hygiene Flags*")
        for (kind, p), items in hygiene.items():
            note = ("move to Outstanding Customer Issues" if p == "customer service requests"
                    else "every ticket needs a project" if kind == "noproj"
                    else "shouldn't exist, re-categorize")
            ids = ", ".join(link(i) for i in items)
            lines.append(f"• *{p}* ({len(items)}): {note}. {ids}")
        lines.append("")

    # Unmapped safety net — surface any project with active tickets that we don't render.
    rendered = set(LIST_BALL + COUNT_ONLY + HYGIENE)
    leftovers = {p: v for p, v in by_proj.items()
                 if p and p not in rendered and p not in EP}
    if leftovers:
        lines.append("*Unmapped (no rule yet)*")
        for p, v in leftovers.items():
            lines.append(f"• {p}: {len(v)}")
        lines.append("")

    lines.append(RULE)
    lines.append("_Excluded from this summary:_")
    for p in sorted(EP):
        lines.append(f"  • {p}")
    lines.append("  • All sub-issues (only master tickets shown)")

    text = "\n".join(lines)
    with open("/tmp/ops_message.txt", "w") as f:
        f.write(text)
    summary = {
        "date": today,
        "total_filtered": len(kept),
        "status_breakdown": {s: by_status.get(s, 0) for s in AS},
        "urgent": len(urgent),
        "needs_response": len(needs),
        "blocked_excluded": bex,
    }
    with open("/tmp/ops_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Wrote {len(text)} chars to /tmp/ops_message.txt; summary: {summary}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
