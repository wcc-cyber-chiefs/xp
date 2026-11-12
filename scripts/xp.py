#!/usr/bin/env python3
"""Cyber Chiefs XP engine.

The ledger (data/ledger.jsonl) is the single source of truth: one JSON line per XP event.
Totals, levels and badges are always recomputed from it, never stored by hand.

Commands
  build     compute everything, write the static site + badge SVGs, list announcements
  announce  post announcements to Discord (DISCORD_WEBHOOK_XP)
  add       append events to the ledger (used by the other commands and for manual fixes)
  award     handle an officer's "/award" issue comment
  join      handle a "Join XP" issue form
  dispatch  handle an xp-event sent from another repo (repository_dispatch)
  validate  check the ledger and members file for problems
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import shutil
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")

COLORS = {  # name: (mid, light)
    "red": ("#E24B4A", "#F09595"),
    "blue": ("#378ADD", "#85B7EB"),
    "amber": ("#D08A1E", "#F2B95A"),
    "green": ("#1D9E75", "#5DCAA5"),
    "purple": ("#7F77DD", "#AFA9EC"),
    "teal": ("#1BA3A3", "#7FD6D6"),
    "pink": ("#D4537E", "#ED93B1"),
    "gray": ("#5F5E5A", "#888780"),
}
METALS = [("bronze", "#C08457"), ("silver", "#C9C7BF"), ("gold", "#F2C14E")]
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
BG = "#0B0F14"


# ----------------------------------------------------------------------------- data

def load_config(root: Path) -> dict:
    with open(root / "xp-config.yml") as f:
        cfg = yaml.safe_load(f)
    for b in cfg["badges"].values():
        if "date" in b:
            b["date"] = str(b["date"])
    for s in cfg["semesters"]:
        s["start"], s["end"] = str(s["start"]), str(s["end"])
    return cfg


def load_members(data: Path) -> dict:
    p = data / "members.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text() or "{}").get("members", {})


def save_members(data: Path, members: dict) -> None:
    (data / "members.json").write_text(
        json.dumps({"members": dict(sorted(members.items()))}, indent=2) + "\n")


def load_ledger(data: Path) -> list[dict]:
    p = data / "ledger.jsonl"
    if not p.exists():
        return []
    out = []
    for n, line in enumerate(p.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            print(f"warning: ledger line {n} is not valid JSON, skipped", file=sys.stderr)
            continue
        e["_line"] = n
        out.append(e)
    return out


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ----------------------------------------------------------------------------- adding events

class XPError(Exception):
    pass


def add_events(root: Path, data: Path, users: list[str], source: str, xp=None, ref: str = "",
               by: str = "manual") -> list[str]:
    """Validate and append events. Returns human-readable result lines."""
    cfg = load_config(root)
    members = load_members(data)
    ledger = load_ledger(data)
    if source not in cfg["sources"]:
        raise XPError(f"unknown source `{source}`. Valid: {', '.join(cfg['sources'])}")
    spec = cfg["sources"][source]
    if xp is None:
        xp = spec["xp"]
    if xp is None:
        raise XPError(f"`{source}` needs an XP amount, e.g. `/award @user {source} 50 reason`")
    xp = int(xp)
    if xp < 0 and source != "correction":
        raise XPError("negative XP is only allowed with the `correction` source")
    if abs(xp) > 500:
        raise XPError("refusing a single award over 500 XP; split it or edit the config")
    if spec.get("unique") and not ref:
        raise XPError(f"`{source}` needs a ref (challenge id, session date, PR link...) so it can't be double-counted")
    seen = {(e["user"].lower(), e["source"], e.get("ref", "")) for e in ledger}
    results, new = [], []
    for u in users:
        u = u.lstrip("@")
        if not USER_RE.match(u):
            results.append(f"- `{u}`: not a valid GitHub username, skipped")
            continue
        key = next((m for m in members if m.lower() == u.lower()), None)
        if key is None:
            results.append(f"- `{u}`: hasn't joined the XP system yet (they need to open a Join XP issue), skipped")
            continue
        if spec.get("unique") and (key.lower(), source, ref) in seen:
            results.append(f"- `{key}`: already has `{source}` for `{ref}`, skipped")
            continue
        new.append({"ts": now_iso(), "user": key, "xp": xp, "source": source, "ref": ref, "by": by})
        seen.add((key.lower(), source, ref))
        results.append(f"- `{key}`: {xp:+d} XP ({source}{' · ' + ref if ref else ''})")
    if new:
        with open(data / "ledger.jsonl", "a") as f:
            for e in new:
                f.write(json.dumps(e) + "\n")
    return results


# ----------------------------------------------------------------------------- compute

def current_semester(cfg: dict, today: str) -> dict | None:
    sems = sorted(cfg["semesters"], key=lambda s: s["start"])
    cur = None
    for s in sems:
        if s["start"] <= today:
            cur = s
    return cur


def max_streak(user_dates: set[str], sessions: list[str]) -> int:
    best = run = 0
    for d in sessions:
        run = run + 1 if d in user_dates else 0
        best = max(best, run)
    return best


def compute(cfg: dict, members: dict, ledger: list[dict], today: str | None = None) -> dict:
    today = today or dt.date.today().isoformat()
    sem = current_semester(cfg, today)
    unique_sources = {k for k, v in cfg["sources"].items() if v.get("unique")}
    seen = set()
    per = defaultdict(lambda: {"xp": 0, "sem_xp": 0, "counts": defaultdict(int), "att": set(), "events": []})
    sessions = set()
    for e in ledger:
        u = e.get("user")
        if u not in members or e.get("source") not in cfg["sources"]:
            continue
        key = (u, e["source"], e.get("ref", ""))
        if e["source"] in unique_sources:
            if key in seen:
                continue
            seen.add(key)
        p = per[u]
        p["xp"] += int(e["xp"])
        day = e["ts"][:10]
        if sem and sem["start"] <= day <= sem["end"]:
            p["sem_xp"] += int(e["xp"])
        p["counts"][e["source"]] += 1
        p["events"].append(e)
        if e["source"] == "attendance" and e.get("ref"):
            p["att"].add(e["ref"])
            sessions.add(e["ref"])
    sessions = sorted(sessions)
    levels = cfg["levels"]
    users = {}
    for u, m in members.items():
        p = per[u]
        xp = max(0, p["xp"])
        li = max(i for i, l in enumerate(levels) if xp >= l["min"])
        badges = {}
        for bname, b in cfg["badges"].items():
            if b["metric"] == "count":
                val = sum(p["counts"][s] for s in b["sources"])
            elif b["metric"] == "streak":
                val = max_streak(p["att"], sessions)
            elif b["metric"] == "joined_before":
                val = 1 if m.get("joined", "9999")[:10] <= b["date"] else 0
            else:
                val = 0
            tier = sum(1 for t in b["tiers"] if val >= t)
            badges[bname] = {"tier": tier, "value": val}
        users[u] = {
            "xp": xp, "sem_xp": max(0, p["sem_xp"]), "level": li, "badges": badges,
            "events": sorted(p["events"], key=lambda e: e["ts"], reverse=True),
            "discord": m.get("discord", ""), "joined": m.get("joined", ""),
        }
    return {"users": users, "semester": sem, "sessions": sessions}


# ----------------------------------------------------------------------------- SVG: shield badges

SHIELD = "M-22 -32H22L28 -26V2C28 20 12 30 0 38C-12 30 -28 20 -28 2V-26Z"
SHIELD_IN = "M-17 -26H17L22 -21V1C22 16 9 24 0 31C-9 24 -22 16 -22 1V-21Z"


def _star(r1=13, r2=5.5):
    pts = []
    for i in range(10):
        r = r1 if i % 2 == 0 else r2
        a = math.radians(-90 + i * 36)
        pts.append(f"{r * math.cos(a):.1f},{r * math.sin(a) + 1:.1f}")
    return " ".join(pts)


def glyph(icon: str, L: str) -> str:
    s = f'stroke="{L}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"'
    if icon == "drop":
        return f'<path d="M0 -14C11 -1 11 13 0 13C-11 13 -11 -1 0 -14Z" fill="{L}" fill-opacity="0.25" {s}/>'
    if icon == "prompt":
        return (f'<polyline points="-10,-8 -3,-1 -10,6" fill="none" {s}/>'
                f'<line x1="0" y1="7" x2="10" y2="7" {s}>'
                '<animate attributeName="opacity" values="1;1;0;0" dur="1.1s" repeatCount="indefinite"/></line>')
    if icon == "flag":
        return (f'<line x1="-7" y1="-13" x2="-7" y2="13" {s}/>'
                f'<polygon points="-7,-12 10,-6 -7,1" fill="{L}" fill-opacity="0.25" {s}/>')
    if icon == "bars":
        return "".join(f'<rect x="{x}" y="{y}" width="4" height="{h}" fill="{L}"/>'
                       for x, y, h in [(-12, 2, 9), (-6, -2, 13), (0, -7, 18), (6, -13, 24)])
    if icon == "crosshair":
        return (f'<circle r="9" fill="none" {s}/>'
                f'<path d="M0 -15V-5M0 5V15M-15 0H-5M5 0H15" fill="none" {s}/>'
                f'<circle r="1.8" fill="{L}"/>')
    if icon == "nodes":
        return (f'<path d="M0 -9L-9 8M0 -9L9 8M-9 8H9" fill="none" {s} stroke-opacity="0.6"/>'
                f'<circle cy="-9" r="4" fill="{BG}" {s}/><circle cx="-9" cy="8" r="4" fill="{BG}" {s}/>'
                f'<circle cx="9" cy="8" r="4" fill="{BG}" {s}/>')
    if icon == "star":
        return f'<polygon points="{_star()}" fill="{L}" fill-opacity="0.25" {s}/>'
    if icon == "lock":
        return (f'<path d="M-7 -1V-6A7 7 0 0 1 7 -6V-1" fill="none" {s}/>'
                f'<rect x="-10" y="-1" width="20" height="14" rx="2" fill="none" {s}/>'
                f'<circle cy="6" r="1.6" fill="{L}"/>')
    return ""


def shield_svg(name: str, badge: dict, tier: int, title: str = "", compact: bool = False) -> str:
    """120x160 standalone shield badge (compact = 120x124, no label). tier 0 = locked."""
    locked = tier == 0
    M, L = COLORS["gray"] if locked else COLORS.get(badge["color"], COLORS["gray"])
    n_tiers = len(badge["tiers"])
    metal = METALS[min(tier, 3) - 1][1] if (tier and n_tiers > 1) else None
    dash = ' stroke-dasharray="5 4"' if locked else ""
    glow = "" if locked else ' filter="url(#glow)"'
    icon = "lock" if locked else badge["icon"]
    parts = [
        (f'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="124" viewBox="0 0 120 124" role="img">' if compact else
         f'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="160" viewBox="0 0 120 160" role="img">'),
        f'<title>{html.escape(title or name)}</title>',
        '<defs><filter id="glow" x="-30%" y="-30%" width="160%" height="160%">'
        '<feGaussianBlur stdDeviation="1.4" result="b"/>'
        '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>',
        '<g transform="translate(60,64) scale(1.5)">',
        f'<path d="M28 -10H33L36 -13" fill="none" stroke="{M}" stroke-width="1"/><circle cx="37" cy="-14" r="1.4" fill="none" stroke="{M}" stroke-width="0.9"/>',
    ]
    if not locked:
        parts.append(f'<path d="M-28 6H-33L-36 9" fill="none" stroke="{M}" stroke-width="1"/>'
                     f'<circle cx="-37" cy="10" r="1.4" fill="none" stroke="{M}" stroke-width="0.9"/>')
    if metal:
        parts.append(f'<path d="{SHIELD}" transform="translate(0,-0.6) scale(1.12)" fill="none" stroke="{metal}" stroke-width="1"/>')
    parts += [
        f'<path d="{SHIELD}" fill="{BG}" stroke="{M}" stroke-width="1.5"{dash}{glow}/>',
        f'<path d="{SHIELD_IN}" fill="none" stroke="{L}" stroke-opacity="0.4" stroke-width="0.8" stroke-dasharray="1.5 2"/>' if not locked else "",
        f'<g transform="translate(0,1)">{glyph(icon, L)}</g>',
        '</g>',
    ]
    if compact:
        return "".join(parts) + "</svg>"
    parts += [
        f'<text x="60" y="140" text-anchor="middle" font-family="{MONO}" font-size="12" fill="{M if not locked else L}">{html.escape(name)}</text>',
    ]
    if locked:
        parts.append(f'<text x="60" y="155" text-anchor="middle" font-family="{MONO}" font-size="10" fill="{COLORS["gray"][0]}">locked</text>')
    elif metal:
        for i in range(3):
            cx = 48 + i * 12
            fill = metal if i < tier else "none"
            parts.append(f'<polygon points="{cx},146 {cx + 4},150 {cx},154 {cx - 4},150" fill="{fill}" stroke="{metal}" stroke-width="1"/>')
    parts.append("</svg>")
    return "".join(parts)


# ----------------------------------------------------------------------------- SVG: level badge

def level_svg(cfg: dict, xp: int, li: int) -> str:
    levels = cfg["levels"]
    lvl = levels[li]
    nxt = levels[li + 1] if li + 1 < len(levels) else None
    M, L = COLORS.get(lvl["color"], COLORS["gray"])
    handle = cfg["club"]["handle"]
    left = f"&gt; {html.escape(handle)}"
    cursor_x = 16 + 7.23 * (len(handle) + 2) + 2
    right = f"lvl.{li + 1:02d} {html.escape(lvl['name'])}"
    frac = 1.0 if not nxt else min(1.0, xp / nxt["min"])
    filled = round(frac * 20)
    segs = "".join(
        f'<rect x="{16 + i * 17.4:.1f}" y="29" width="14" height="6" fill="{M if i < filled else "#24272B"}"/>'
        for i in range(20))
    bottom_l = f"{xp}/{nxt['min']} xp" if nxt else f"{xp} xp"
    bottom_r = f"next: {html.escape(nxt['name'])}" if nxt else "max level"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="380" height="64" viewBox="0 0 380 64" role="img">'
        f'<title>{html.escape(cfg["club"]["name"])}: {html.escape(lvl["name"])}, {xp} XP</title>'
        '<defs><filter id="glow" x="-10%" y="-30%" width="120%" height="160%">'
        '<feGaussianBlur stdDeviation="1.3" result="b"/>'
        '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
        '<pattern id="scan" width="4" height="3" patternUnits="userSpaceOnUse">'
        '<rect width="4" height="1" fill="#ffffff" fill-opacity="0.03"/></pattern></defs>'
        f'<path d="M8.75 0.75H379.25V55.25L371.25 63.25H0.75V8.75Z" fill="{BG}" stroke="{M}" stroke-width="1.5" filter="url(#glow)"/>'
        '<path d="M8.75 0.75H379.25V55.25L371.25 63.25H0.75V8.75Z" fill="url(#scan)"/>'
        f'<text x="16" y="21" font-family="{MONO}" font-size="12" fill="#5DCAA5">{left}</text>'
        f'<rect x="{cursor_x:.1f}" y="11" width="7" height="12" fill="#5DCAA5">'
        '<animate attributeName="opacity" values="1;1;0;0" dur="1.1s" repeatCount="indefinite"/></rect>'
        f'<text x="364" y="21" text-anchor="end" font-family="{MONO}" font-size="12" fill="{L}">{right}</text>'
        f'<g filter="url(#glow)">{segs}</g>'
        f'<text x="16" y="52" font-family="{MONO}" font-size="11" fill="#888780">{bottom_l}</text>'
        f'<text x="364" y="52" text-anchor="end" font-family="{MONO}" font-size="11" fill="#888780">{bottom_r}</text>'
        '</svg>'
    )


# ----------------------------------------------------------------------------- site

CSS = """
:root{--bg:#07090C;--panel:#0B0F14;--line:#1C2128;--text:#D6D9DE;--muted:#8B9098;--accent:#5DCAA5;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:var(--mono);font-size:14px;line-height:1.6}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:960px;margin:0 auto;padding:32px 16px 64px}
header{border:1px solid var(--line);background:var(--panel);padding:18px 20px;margin-bottom:24px;
clip-path:polygon(10px 0,100% 0,100% calc(100% - 10px),calc(100% - 10px) 100%,0 100%,0 10px)}
header h1{margin:0;font-size:20px;font-weight:500}header h1 span{color:var(--accent)}
header p{margin:4px 0 0;color:var(--muted);font-size:13px}
.tabs{display:flex;gap:8px;margin-bottom:12px}.tabs button{font:inherit;background:var(--panel);color:var(--muted);
border:1px solid var(--line);padding:6px 14px;cursor:pointer}.tabs button.on{color:var(--accent);border-color:var(--accent)}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line)}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--muted);font-weight:400;font-size:12px}td.n{color:var(--muted);width:40px}
.u{display:flex;align-items:center;gap:10px}.u img{width:28px;height:28px;border-radius:4px;background:#1C2128}
td.xp{text-align:right;color:var(--accent);white-space:nowrap}.mini img{width:31px;height:32px;margin-right:3px}
.lvl{font-size:12px;padding:1px 8px;border:1px solid}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:12px;margin:12px 0 24px}
.grid figure{margin:0;text-align:center}.grid figcaption{color:var(--muted);font-size:11px}
pre{background:var(--panel);border:1px solid var(--line);padding:12px;overflow-x:auto;font-size:12px;white-space:pre-wrap;word-break:break-all}
.feed{list-style:none;padding:0;margin:0}.feed li{border-bottom:1px solid var(--line);padding:6px 0;color:var(--muted)}
.feed b{color:var(--text);font-weight:400}.empty{color:var(--muted);padding:24px;text-align:center}
h2{font-size:15px;font-weight:500;margin:28px 0 8px}h2::before{content:"// ";color:var(--muted)}
button.copy{font:inherit;font-size:12px;background:none;border:1px solid var(--line);color:var(--muted);padding:3px 10px;cursor:pointer}
@media(max-width:640px){.mini{display:none}th.mini{display:none}}
"""


def page(title: str, body: str, depth: int = 0) -> str:
    up = "../" * depth
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body><div class="wrap">'
            f'<header><h1><a href="{up}" style="color:inherit">&gt; <span>wcc_cyber_chiefs</span> // xp</a></h1>'
            f'<p>earn xp for showing up, solving, writing and building. levels and badges update automatically.</p></header>'
            f'{body}</div></body></html>')


def board_rows(cfg, users, key) -> str:
    ranked = sorted(users.items(), key=lambda kv: (-kv[1][key], kv[0].lower()))
    ranked = [(u, d) for u, d in ranked if d[key] > 0]
    if not ranked:
        return '<div class="empty">no xp yet. be the first.</div>'
    rows = []
    for i, (u, d) in enumerate(ranked, 1):
        lvl = cfg["levels"][d["level"]]
        M, L = COLORS.get(lvl["color"], COLORS["gray"])
        earned = [b for b, v in d["badges"].items() if v["tier"]]
        mini = "".join(f'<img src="badges/{u}/{b}-mini.svg" alt="{b}" title="{b}">' for b in earned)
        ue = html.escape(u)
        rows.append(
            f'<tr><td class="n">{i:02d}</td><td><div class="u"><img src="https://github.com/{ue}.png?size=56" alt="">'
            f'<a href="u/{ue}/">{ue}</a></div></td>'
            f'<td><span class="lvl" style="color:{L};border-color:{M}">{html.escape(lvl["name"])}</span></td>'
            f'<td class="mini">{mini}</td><td class="xp">{d[key]} xp</td></tr>')
    return ('<table><thead><tr><th>#</th><th>member</th><th>level</th><th class="mini">badges</th>'
            '<th style="text-align:right">xp</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>")


def event_line(cfg, e) -> str:
    label = cfg["sources"].get(e["source"], {}).get("label", e["source"])
    ref = f' · {html.escape(e["ref"])}' if e.get("ref") else ""
    return f'<li>{e["ts"][:10]} <b>{e["xp"]:+d} xp</b> {html.escape(label)}{ref}</li>'


def build_site(cfg: dict, result: dict, ledger: list[dict], out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "badges" / "catalog").mkdir(parents=True)
    users = result["users"]
    site = cfg["club"]["site_url"].rstrip("/")

    for bname, b in cfg["badges"].items():
        for t in range(0, len(b["tiers"]) + 1):
            (out / "badges" / "catalog" / f"{bname}-t{t}.svg").write_text(shield_svg(bname, b, t))
    for i, l in enumerate(cfg["levels"]):
        (out / "badges" / "catalog" / f"level-{i + 1}.svg").write_text(level_svg(cfg, l["min"], i))

    for u, d in users.items():
        bdir = out / "badges" / u
        bdir.mkdir(parents=True)
        (bdir / "level.svg").write_text(level_svg(cfg, d["xp"], d["level"]))
        for bname, b in cfg["badges"].items():
            t = d["badges"][bname]["tier"]
            (bdir / f"{bname}.svg").write_text(shield_svg(bname, b, t, f"{u}: {bname}"))
            (bdir / f"{bname}-mini.svg").write_text(shield_svg(bname, b, t, f"{u}: {bname}", compact=True))
        figs = []
        for bname, b in cfg["badges"].items():
            v = d["badges"][bname]
            nxt = next((t for t in b["tiers"] if v["value"] < t), None)
            prog = f"{v['value']}/{nxt} to next" if nxt else "maxed"
            figs.append(f'<figure><img src="../../badges/{u}/{bname}.svg" alt="{bname}" width="120" height="160">'
                        f'<figcaption>{html.escape(b["desc"])}<br>{prog}</figcaption></figure>')
        earned = [b for b, v in d["badges"].items() if v["tier"]]
        embed = (f"[![{cfg['club']['name']} level]({site}/badges/{u}/level.svg)]({site}/u/{u}/)\n"
                 + " ".join(f"[![{b}]({site}/badges/{u}/{b}.svg)]({site}/u/{u}/)" for b in earned))
        feed = "".join(event_line(cfg, e) for e in d["events"][:15]) or '<li>no activity yet</li>'
        ue = html.escape(u)
        body = (f'<p><img src="../../badges/{ue}/level.svg" alt="level" width="380" height="64"></p>'
                f'<p style="color:var(--muted)">{ue} · {d["xp"]} xp all-time · {d["sem_xp"]} xp this semester</p>'
                f'<h2>badges</h2><div class="grid">{"".join(figs)}</div>'
                f'<h2>show it off</h2><p style="color:var(--muted)">paste this into your GitHub profile README. '
                f'it updates on its own.</p><pre id="e">{html.escape(embed)}</pre>'
                f'<button class="copy" onclick="navigator.clipboard.writeText(document.getElementById(\'e\').innerText);'
                f'this.textContent=\'copied\'">copy</button>'
                f'<h2>recent activity</h2><ul class="feed">{feed}</ul>')
        udir = out / "u" / u
        udir.mkdir(parents=True)
        (udir / "index.html").write_text(page(f"{u} · Cyber Chiefs XP", body, depth=2))

    sem = result["semester"]
    sem_name = html.escape(sem["name"]) if sem else "this semester"
    recent = sorted((e for e in ledger if e.get("user") in users), key=lambda e: e["ts"], reverse=True)[:12]
    feed = "".join(f'<li><a href="u/{html.escape(e["user"])}/">{html.escape(e["user"])}</a> ' + event_line(cfg, e)[4:]
                   for e in recent) or "<li>nothing yet</li>"
    catalog = "".join(
        f'<figure><img src="badges/catalog/{b}-t{len(v["tiers"])}.svg" alt="{b}" width="120" height="160">'
        f'<figcaption>{html.escape(v["desc"])}<br>{" / ".join(map(str, v["tiers"]))}</figcaption></figure>'
        for b, v in cfg["badges"].items())
    ladder = "".join(f'<li><b>lvl.{i + 1:02d} {html.escape(l["name"])}</b> · {l["min"]}+ xp</li>'
                     for i, l in enumerate(cfg["levels"]))
    earn = "".join(f'<li><b>{html.escape(v["label"])}</b> · {v["xp"] if v["xp"] is not None else "varies"} xp</li>'
                   for k, v in cfg["sources"].items() if k != "correction")
    body = (
        f'<div class="tabs"><button class="on" data-t="sem">{sem_name}</button><button data-t="all">all-time</button></div>'
        f'<div id="t-sem">{board_rows(cfg, users, "sem_xp")}</div>'
        f'<div id="t-all" hidden>{board_rows(cfg, users, "xp")}</div>'
        f'<h2>latest</h2><ul class="feed">{feed}</ul>'
        f'<h2>badges</h2><div class="grid">{catalog}</div>'
        f'<h2>levels</h2><ul class="feed">{ladder}</ul>'
        f'<h2>how to earn</h2><ul class="feed">{earn}</ul>'
        f'<h2>join</h2><p style="color:var(--muted)">open a <a href="https://github.com/{html.escape(cfg["club"].get("org", "wcc-cyber-chiefs"))}/xp/issues/new?template=join-xp.yml">Join XP issue</a> '
        f'to get on the board. joining puts your GitHub username and your XP activity on this public page.</p>'
        '<script>document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>{'
        'document.querySelectorAll(".tabs button").forEach(x=>x.classList.toggle("on",x===b));'
        'document.getElementById("t-sem").hidden=b.dataset.t!=="sem";'
        'document.getElementById("t-all").hidden=b.dataset.t!=="all"})</script>')
    (out / "index.html").write_text(page("Cyber Chiefs XP", body))
    (out / ".nojekyll").write_text("")


def state_of(result: dict) -> dict:
    return {"users": {u: {"xp": d["xp"], "level": d["level"],
                          "badges": {b: v["tier"] for b, v in d["badges"].items()}}
                      for u, d in result["users"].items()}}


def diff_announcements(cfg: dict, prev: dict, new: dict) -> list[str]:
    if "users" not in prev:
        return []  # first deploy: don't flood the channel
    site = cfg["club"]["site_url"].rstrip("/")
    msgs = []
    for u, n in new["users"].items():
        p = prev["users"].get(u)
        link = f"[{u}](<{site}/u/{u}/>)"
        if p is None:
            msgs.append(f"`[+]` {link} joined the XP board. welcome to the grind.")
            p = {"level": 0, "badges": {}}
        if n["level"] > p["level"]:
            l = cfg["levels"][n["level"]]
            msgs.append(f"`[^]` {link} leveled up to **lvl.{n['level'] + 1:02d} {l['name']}** ({n['xp']} xp)")
        for b, t in n["badges"].items():
            if t > p["badges"].get(b, 0):
                tiers = cfg["badges"][b]["tiers"]
                tier_name = f" ({METALS[t - 1][0]})" if len(tiers) > 1 else ""
                msgs.append(f"`[*]` {link} unlocked **{b}**{tier_name}")
    return msgs


# ----------------------------------------------------------------------------- commands

def cmd_build(a):
    root = ROOT
    data = Path(a.data_dir) if a.data_dir else root / "data"
    cfg = load_config(root)
    members, ledger = load_members(data), load_ledger(data)
    result = compute(cfg, members, ledger, a.today)
    out = Path(a.out)
    build_site(cfg, result, ledger, out)
    state = state_of(result)
    (out / "state.json").write_text(json.dumps(state, indent=1))
    prev = {}
    if a.prev_state and Path(a.prev_state).exists():
        prev = json.loads(Path(a.prev_state).read_text() or "{}")
    elif a.prev_url:
        try:
            with urllib.request.urlopen(a.prev_url, timeout=15) as r:
                prev = json.loads(r.read())
        except Exception as ex:  # first deploy or site down
            print(f"note: no previous state ({ex}); skipping announcements", file=sys.stderr)
    msgs = diff_announcements(cfg, prev, state)
    if a.announce_file:
        Path(a.announce_file).write_text(json.dumps(msgs, indent=1))
    print(f"built {out} · {len(members)} members · {len(ledger)} events · {len(msgs)} announcements")


def cmd_announce(a):
    msgs = json.loads(Path(a.file).read_text()) if Path(a.file).exists() else []
    hook = os.environ.get("DISCORD_WEBHOOK_XP", "")
    if not msgs:
        print("nothing to announce")
        return
    if not hook:
        print("DISCORD_WEBHOOK_XP not set; would have posted:\n" + "\n".join(msgs))
        return
    chunks, cur = [], ""
    for m in msgs[:40]:
        if len(cur) + len(m) + 1 > 1900:
            chunks.append(cur)
            cur = ""
        cur += m + "\n"
    if cur:
        chunks.append(cur)
    for c in chunks:
        req = urllib.request.Request(hook, data=json.dumps({"content": c, "allowed_mentions": {"parse": []}}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "cyber-chiefs-xp"})
        urllib.request.urlopen(req, timeout=15)
    print(f"posted {len(msgs)} announcements")


def cmd_add(a):
    data = Path(a.data_dir) if a.data_dir else ROOT / "data"
    try:
        lines = add_events(ROOT, data, a.user.split(","), a.source, a.xp, a.ref or "", a.by)
    except XPError as ex:
        sys.exit(f"error: {ex}")
    print("\n".join(lines))


AWARD_HELP = ("usage: `/award @user [@user2 ...] <source> [xp] [ref or note]`\n"
              "examples:\n- `/award @alex @sam attendance 2026-11-18`\n"
              "- `/award @alex writeup https://github.com/.../pull/12`\n"
              "- `/award @alex award 50 ran the IR tabletop`\n"
              "- `/award @alex correction -25 duplicate solve`")


def cmd_award(a):
    cfg = load_config(ROOT)
    data = Path(a.data_dir) if a.data_dir else ROOT / "data"
    body = Path(a.body_file).read_text().strip().splitlines()[0] if Path(a.body_file).read_text().strip() else ""
    reply = []
    officers = {o.lower() for o in cfg.get("officers", [])}
    if a.actor.lower() not in officers:
        reply.append(f"@{a.actor} only officers listed in `xp-config.yml` can award XP.")
    else:
        toks = body.split()[1:]
        users = [t[1:] for t in toks if t.startswith("@")]
        rest = [t for t in toks if not t.startswith("@")]
        if not users or not rest:
            reply.append(AWARD_HELP)
        else:
            source = rest[0]
            xp = None
            ref_toks = rest[1:]
            if ref_toks and re.fullmatch(r"[+-]?\d+", ref_toks[0]):
                xp = int(ref_toks[0])
                ref_toks = ref_toks[1:]
            ref = " ".join(ref_toks)[:200]
            try:
                reply += add_events(ROOT, data, users, source, xp, ref, by=a.actor)
            except XPError as ex:
                reply.append(f"error: {ex}\n\n{AWARD_HELP}")
    Path(a.reply_file).write_text("\n".join(reply) + "\n")
    print("\n".join(reply))


def _form_field(body: str, heading: str) -> str:
    m = re.search(rf"###\s*{re.escape(heading)}\s*\n+(.*?)(?=\n###|\Z)", body, re.S)
    v = m.group(1).strip() if m else ""
    return "" if v in ("_No response_", "None") else v


def cmd_join(a):
    data = Path(a.data_dir) if a.data_dir else ROOT / "data"
    body = Path(a.body_file).read_text()
    user = a.user
    members = load_members(data)
    if not re.search(r"-\s*\[[xX]\]\s*I understand", body):
        msg = "You need to tick the consent box so we can show your username and XP on the public board. Edit the issue or open a new one."
    elif user in members:
        msg = f"@{user} you're already on the board: {load_config(ROOT)['club']['site_url']}/u/{user}/"
    else:
        discord = re.sub(r"[^\w.#-]", "", _form_field(body, "Discord username"))[:40]
        members[user] = {"joined": now_iso(), "discord": discord}
        save_members(data, members)
        msg = (f"Welcome to the board, @{user}. Your profile appears here within a few minutes: "
               f"{load_config(ROOT)['club']['site_url']}/u/{user}/\n\n"
               "Earn XP by attending sessions, solving club CTF challenges, writing writeups and building challenges. "
               "Your badges page has a copy-paste snippet for your GitHub profile README.")
    Path(a.reply_file).write_text(msg + "\n")
    print(msg)


def cmd_dispatch(a):
    data = Path(a.data_dir) if a.data_dir else ROOT / "data"
    p = json.loads(Path(a.payload_file).read_text() or "{}")
    users = p.get("users") or ([p["user"]] if p.get("user") else [])
    try:
        lines = add_events(ROOT, data, users, p.get("source", ""), p.get("xp"), str(p.get("ref", ""))[:200],
                           by=f"dispatch:{a.sender}")
    except XPError as ex:
        sys.exit(f"error: {ex}")
    print("\n".join(lines))


def cmd_validate(a):
    cfg = load_config(ROOT)
    data = Path(a.data_dir) if a.data_dir else ROOT / "data"
    members, ledger = load_members(data), load_ledger(data)
    problems = 0
    for e in ledger:
        for k in ("ts", "user", "xp", "source"):
            if k not in e:
                print(f"line {e['_line']}: missing `{k}`"); problems += 1
        if e.get("source") not in cfg["sources"]:
            print(f"line {e['_line']}: unknown source {e.get('source')}"); problems += 1
        if e.get("user") not in members:
            print(f"line {e['_line']}: {e.get('user')} is not a member (ignored in totals)")
    for i in range(1, len(cfg["levels"])):
        if cfg["levels"][i]["min"] <= cfg["levels"][i - 1]["min"]:
            print("levels must have increasing `min`"); problems += 1
    for b, v in cfg["badges"].items():
        if v.get("color") not in COLORS:
            print(f"badge {b}: unknown color {v.get('color')}"); problems += 1
    print(f"{problems} problem(s)")
    sys.exit(1 if problems else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", help="defaults to ./data")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--out", default="_site"); b.add_argument("--prev-state")
    b.add_argument("--prev-url"); b.add_argument("--announce-file"); b.add_argument("--today")
    b.set_defaults(fn=cmd_build)
    n = sub.add_parser("announce"); n.add_argument("--file", default="announce.json"); n.set_defaults(fn=cmd_announce)
    d = sub.add_parser("add"); d.add_argument("--user", required=True, help="comma-separated")
    d.add_argument("--source", required=True); d.add_argument("--xp", type=int); d.add_argument("--ref")
    d.add_argument("--by", default="manual"); d.set_defaults(fn=cmd_add)
    w = sub.add_parser("award"); w.add_argument("--actor", required=True); w.add_argument("--body-file", required=True)
    w.add_argument("--reply-file", default="reply.md"); w.set_defaults(fn=cmd_award)
    j = sub.add_parser("join"); j.add_argument("--user", required=True); j.add_argument("--body-file", required=True)
    j.add_argument("--reply-file", default="reply.md"); j.set_defaults(fn=cmd_join)
    s = sub.add_parser("dispatch"); s.add_argument("--payload-file", required=True); s.add_argument("--sender", default="unknown")
    s.set_defaults(fn=cmd_dispatch)
    v = sub.add_parser("validate"); v.set_defaults(fn=cmd_validate)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
