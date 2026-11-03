"""Layer 4 analyst dashboard: FastAPI, server-rendered HTML/CSS, inline SVG charts, no JavaScript.

    python -m dashboard.app --db local/layer4-demo.sqlite3        # http://127.0.0.1:8000
    uvicorn dashboard.app:app                                     # path from CIS_AUDIT_DB

It reads the hash-chained Layer 4 audit log. Its only write is an analyst release request, appended as
an ``analyst_release`` entry that the Layer 4 engine applies at its next tick
(``ResponseEngine.sync_releases``). Set ``CIS_RELEASE_TOKEN`` to require a token for releases. There is
no user authentication: it binds to 127.0.0.1 by default; put it behind an authenticating proxy before
exposing it on a network.

JSON for other consumers: ``/api/sensors``, ``/api/sensors/{id}/decisions``, ``/api/released``,
``/api/chain``.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sqlite3
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from core.response.auditlog import GENESIS, AuditLog, entry_hash

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATES = ("trusted", "suspicious", "quarantined")
STATE_RANK = {"quarantined": 0, "suspicious": 1, "trusted": 2}
NAV = [("/", "Sensors"), ("/provenance", "Provenance failures"), ("/suspicious", "Authentic but suspicious"),
       ("/release", "Analyst release"), ("/audit", "Audit log")]
DECISION_COLUMNS = "seq, ts, sensor_id, payload"


def e(value):
    """HTML-escape anything (sensor ids and explanations may originate from untrusted messages)."""
    return escape("" if value is None else str(value), quote=True)


# ---------------------------------------------------------------------------------------------- data


class AuditReader:
    """Read-only queries on the audit log. Opens a fresh read-only connection per call."""

    def __init__(self, path):
        self.path = Path(path)

    def exists(self):
        return self.path.exists()

    def _connect(self):
        return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)

    def _query(self, sql, args=()):
        with self._connect() as db:
            return db.execute(sql, args).fetchall()

    def latest(self):
        rows = self._query(f"""SELECT {DECISION_COLUMNS} FROM audit_log WHERE seq IN (
                                 SELECT MAX(seq) FROM audit_log WHERE kind = 'trust_decision' GROUP BY sensor_id)""")
        decisions = [json.loads(r[3]) for r in rows]
        return sorted(decisions, key=lambda d: (STATE_RANK[d["state"]], d["trust_score"], d["sensor_id"]))

    def decisions(self, sensor_id=None, where="", limit=None):
        sql = f"SELECT {DECISION_COLUMNS} FROM audit_log WHERE kind = 'trust_decision'"
        args = []
        if sensor_id is not None:
            sql += " AND sensor_id = ?"
            args.append(sensor_id)
        if where:
            sql += " AND " + where
        sql += " ORDER BY seq DESC" + (f" LIMIT {int(limit)}" if limit else "")
        return [json.loads(r[3]) for r in self._query(sql, args)]

    def events(self, kinds, limit=500):
        marks = ",".join("?" * len(kinds))
        rows = self._query(f"SELECT seq, ts, kind, sensor_id, payload FROM audit_log WHERE kind IN ({marks}) "
                           f"ORDER BY seq DESC LIMIT {int(limit)}", kinds)
        return [{"seq": s, "ts": t, "kind": k, "sensor_id": sid, **json.loads(p)} for s, t, k, sid, p in rows]

    def entries(self, limit=500):
        return self._query("SELECT seq, ts, kind, sensor_id, prev_hash, hash FROM audit_log "
                           f"ORDER BY seq DESC LIMIT {int(limit)}")

    def has_kind(self, kind):
        return bool(self._query("SELECT 1 FROM audit_log WHERE kind = ? LIMIT 1", (kind,)))

    def released(self, after=0, limit=5000):
        out = []
        for seq, ts, sensor, payload in self._query(
                f"SELECT {DECISION_COLUMNS} FROM audit_log WHERE kind = 'trust_decision' AND seq > ? "
                f"ORDER BY seq LIMIT {int(limit)}", (after,)):
            d = json.loads(payload)
            out += [{"seq": seq, "sensor_id": sensor, "ts": d["ts"], "quantity": v["quantity"],
                     "value": v["released_value"], "unit": v["unit"], "source": v["release"], "state": d["state"]}
                    for v in d["evidence"] if v["release"] != "withheld"]
        return out

    def verify(self):
        """Recompute the hash chain over a read-only connection: ``(ok, count, first_bad_seq)``."""
        prev, expected, count = GENESIS, 1, 0
        with self._connect() as db:
            for seq, ts, kind, sensor, dedup, payload, prev_hash, digest in db.execute(
                    "SELECT seq, ts, kind, sensor_id, dedup_key, payload, prev_hash, hash FROM audit_log ORDER BY seq"):
                if seq != expected or prev_hash != prev or entry_hash(prev, seq, ts, kind, sensor, dedup, payload) != digest:
                    return False, count, seq
                prev, expected, count = digest, seq + 1, count + 1
        return True, count, None


# ---------------------------------------------------------------------------------------------- html


def page(title, active, body, refresh=30):
    nav = "".join(f'<a href="{href}" class="{"active" if href == active else ""}">{e(label)}</a>' for href, label in NAV)
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{meta}<title>{e(title)} · Layer 4</title><link rel="stylesheet" href="/static/style.css"></head>
<body><header><h1>Sensor trust and response</h1><nav>{nav}</nav></header>
<main>{body}</main><footer>Layer 4 · reads the hash-chained audit log · auto-refresh {refresh or "off"}{"s" if refresh else ""}</footer>
</body></html>""")


def badge(state):
    return f'<span class="badge {e(state)}">{e(state)}</span>'


def trust_bar(value):
    return f'<span class="bar"><span style="width:{max(0.0, min(1.0, value)) * 100:.0f}%"></span></span>{value:.2f}'


def table(headers, rows, numeric=()):
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    if not rows:
        return f'<div class="table-wrap"><table><tr>{head}</tr><tr><td colspan="{len(headers)}" class="muted">None.</td></tr></table></div>'
    body = "".join("<tr>" + "".join(
        f'<td class="{"num" if i in numeric else ""}">{cell}</td>' for i, cell in enumerate(row)) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table><tr>{head}</tr>{body}</table></div>'


def short_ts(ts):
    return e(str(ts)[:16].replace("T", " "))


def sensor_link(sensor_id):
    return f'<a href="/sensors/{quote(sensor_id)}">{e(sensor_id)}</a>'


# ---------------------------------------------------------------------------------------------- svg


def _times(values):
    return [datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() for v in values]


def line_chart(ts, series, markers, unit, width=1000, height=260):
    """``series``: [(label, css colour, values, dashed)]; ``markers``: [(index, value, css colour, shape, title)]."""
    left, right, top, bottom = 60, 12, 10, 26
    xs = _times(ts)
    values = [v for _, _, vals, _ in series for v in vals if v is not None] + [m[1] for m in markers if m[1] is not None]
    if not xs or not values:
        return '<p class="muted">No values.</p>'
    x0, x1 = min(xs), max(xs) if max(xs) > min(xs) else min(xs) + 1
    y0, y1 = min(values), max(values)
    pad = (y1 - y0) * 0.08 or 1.0
    y0, y1 = y0 - pad, y1 + pad
    sx = lambda x: left + (x - x0) / (x1 - x0) * (width - left - right)
    sy = lambda y: top + (y1 - y) / (y1 - y0) * (height - top - bottom)
    parts = [f'<line class="axis" x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}"/>',
             f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}"/>']
    for frac in (0, 0.5, 1):
        y = y0 + frac * (y1 - y0)
        parts.append(f'<text x="{left-6}" y="{sy(y)+4:.1f}" text-anchor="end">{y:.1f}</text>')
    parts.append(f'<text x="{left}" y="{height-6}">{short_ts(ts[0])}</text>')
    parts.append(f'<text x="{width-right}" y="{height-6}" text-anchor="end">{short_ts(ts[-1])}</text>')
    parts.append(f'<text x="4" y="{top+8}">{e(unit)}</text>')
    for _, colour, vals, dashed in series:
        segment = []
        for x, v in list(zip(xs, vals)) + [(None, None)]:
            if v is None:
                if len(segment) > 1:
                    pts = " ".join(f"{sx(a):.1f},{sy(b):.1f}" for a, b in segment)
                    dash = ' stroke-dasharray="6 4"' if dashed else ""
                    parts.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="2"{dash}/>')
                segment = []
            else:
                segment.append((x, v))
    for i, v, colour, shape, title in markers:
        if v is None:
            continue
        cx, cy = sx(xs[i]), sy(v)
        mark = (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="{colour}">' if shape == "circle" else
                f'<path d="M{cx-5:.1f},{cy-5:.1f} L{cx+5:.1f},{cy+5:.1f} M{cx-5:.1f},{cy+5:.1f} L{cx+5:.1f},{cy-5:.1f}" '
                f'stroke="{colour}" stroke-width="2.5">')
        parts.append(f'{mark}<title>{e(title)}</title>{"</circle>" if shape == "circle" else "</path>"}')
    return f'<svg viewBox="0 0 {width} {height}" role="img">{"".join(parts)}</svg>'


def state_chart(decisions, width=1000, height=150):
    left, right, top, bottom = 60, 12, 10, 26
    xs = _times([d["ts"] for d in decisions])
    if not xs:
        return ""
    x0, x1 = min(xs), max(xs) if max(xs) > min(xs) else min(xs) + 1
    sx = lambda x: left + (x - x0) / (x1 - x0) * (width - left - right)
    sy = lambda y: top + (1 - y) * (height - top - bottom)
    parts = []
    for i, d in enumerate(decisions):
        a = sx(xs[i])
        b = sx(xs[i + 1]) if i + 1 < len(xs) else width - right
        parts.append(f'<rect x="{a:.1f}" y="{top}" width="{max(b - a, 1):.1f}" height="{height-top-bottom}" '
                     f'fill="var(--{d["state"]})" opacity="0.22"><title>{short_ts(d["ts"])}: {e(d["state"])}</title></rect>')
    pts = " ".join(f"{sx(x):.1f},{sy(d['trust_score']):.1f}" for x, d in zip(xs, decisions))
    parts.append(f'<polyline points="{pts}" fill="none" stroke="var(--text)" stroke-width="1.8"/>')
    parts += [f'<line class="axis" x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}"/>',
              f'<text x="{left-6}" y="{sy(1)+4:.1f}" text-anchor="end">1.0</text>',
              f'<text x="{left-6}" y="{sy(0)+4:.1f}" text-anchor="end">0.0</text>',
              f'<text x="4" y="{top+8}">trust</text>',
              f'<text x="{left}" y="{height-6}">{short_ts(decisions[0]["ts"])}</text>',
              f'<text x="{width-right}" y="{height-6}" text-anchor="end">{short_ts(decisions[-1]["ts"])}</text>']
    return f'<svg viewBox="0 0 {width} {height}" role="img">{"".join(parts)}</svg>'


# ---------------------------------------------------------------------------------------------- app


def create_app(db_path, release_token=None):
    reader = AuditReader(db_path)
    app = FastAPI(title="Layer 4 dashboard", docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def missing():
        return page("No audit log", "/", f'<div class="banner error">Audit log not found: <span class="mono">{e(reader.path)}</span></div>'
                    '<p>Build the demo log with <span class="mono">python -m scripts.demo_layer4</span>.</p>', refresh=0)

    def chain_banner():
        ok, count, bad = reader.verify()
        if not ok:
            return (f'<div class="banner error"><strong>Audit chain broken at entry #{bad}.</strong> '
                    "The log was modified after it was written; do not trust the entries from that point on.</div>")
        return ""

    def demo_banner():
        if reader.has_kind("demo_notice"):
            return ('<div class="banner info">Demo data: Layer 1 is the real signer and verifier; '
                    "the Layer 3 rows are a synthetic stand-in.</div>")
        return ""

    @app.get("/", response_class=HTMLResponse)
    def overview():
        if not reader.exists():
            return missing()
        latest = reader.latest()
        if not latest:
            return page("Sensors", "/", '<p class="muted">No trust decisions yet.</p>')
        ok, count, bad = reader.verify()
        counts = {s: sum(d["state"] == s for d in latest) for s in STATES}
        cards = (f'<div class="card"><div class="label">Sensors</div><div class="value">{len(latest)}</div></div>'
                 + "".join(f'<div class="card {s}"><div class="label">{s}</div><div class="value">{counts[s]}</div></div>' for s in STATES)
                 + f'<div class="card {"trusted" if ok else "quarantined"}"><div class="label">Audit chain</div>'
                   f'<div class="value">{"verified" if ok else f"broken #{bad}"}</div><div class="muted">{count} entries</div></div>')
        rows = [[sensor_link(d["sensor_id"]), badge(d["state"]), e(d["cause"] or ""), trust_bar(d["trust_score"]),
                 e(d.get("alert_count_short", 0)), e(d.get("alert_count_long", 0)), e(d.get("provenance_failures_recent", 0)),
                 e(d["category"]), e(", ".join(d["actions"])), short_ts(d["ts"])] for d in latest]
        changes = reader.decisions(where="json_extract(payload, '$.state') != json_extract(payload, '$.previous_state')", limit=200)
        change_rows = [[short_ts(d["ts"]), sensor_link(d["sensor_id"]), badge(d["previous_state"]) + " → " + badge(d["state"]),
                        e(d["cause"] or ""), f'<span class="explain">{e(d["explanation"])}</span>'] for d in changes]
        body = (chain_banner() + demo_banner() + f'<div class="cards">{cards}</div>'
                f'<p class="muted">Last tick: {short_ts(max(d["ts"] for d in latest))} UTC</p>'
                "<h2>Sensors</h2>" + table(["sensor", "state", "cause", "trust", "alerts / 6 h (suspicious at 3)",
                                            "alerts / 12 h (quarantine at 6)", "L1 failures / 2 h (quarantine at 3)",
                                            "category", "actions", "last decision"], rows, numeric=(4, 5, 6))
                + "<h2>State changes</h2>" + table(["time", "sensor", "change", "cause", "explanation"], change_rows))
        return page("Sensors", "/", body)

    @app.get("/sensors/{sensor_id}", response_class=HTMLResponse)
    def sensor(sensor_id: str, quantity: str | None = None):
        if not reader.exists():
            return missing()
        history = list(reversed(reader.decisions(sensor_id)))
        if not history:
            raise HTTPException(404, "Unknown sensor")
        quantities = [v["quantity"] for v in history[-1]["evidence"]]
        quantity = quantity if quantity in quantities else quantities[0]
        ev = [next(v for v in d["evidence"] if v["quantity"] == quantity) for d in history]
        ts = [d["ts"] for d in history]
        unit = next((v["unit"] for v in ev if v["unit"]), "")
        markers = [(i, v["raw_value"], "var(--suspicious)", "circle", f'{short_ts(ts[i])}: Layer 3 alert, p={v["p_value"]:.1e}')
                   for i, v in enumerate(ev) if v["alert"]]
        markers += [(i, v["raw_value"], "var(--quarantined)", "cross", f'{short_ts(ts[i])}: Layer 1 {v["provenance_status"]}')
                    for i, v in enumerate(ev) if v["provenance_status"] not in ("valid", "missing")]
        chart = line_chart(ts, [("raw", "var(--raw)", [v["raw_value"] for v in ev], False),
                                ("released", "var(--released)", [v["released_value"] for v in ev], True)], markers, unit)
        options = "".join(f'<option value="{e(q)}" {"selected" if q == quantity else ""}>{e(q)}</option>' for q in quantities)
        last = history[-1]
        rows = [[short_ts(d["ts"]), badge(d["state"]), e(d["category"]), e(", ".join(d["actions"])),
                 f'<span class="explain">{e(d["explanation"])}</span>'] for d in reversed(history[-200:])]
        body = (chain_banner() + f'<p><a href="/">← all sensors</a></p><h2 class="mono">{e(sensor_id)}</h2>'
                f'<p>{badge(last["state"])} trust {last["trust_score"]:.2f} · {e(last["explanation"])}</p>'
                f'<form class="inline" method="get"><label>Quantity<select name="quantity">{options}</select></label>'
                '<button type="submit">Show</button></form>'
                '<div class="legend"><span><i style="background:var(--raw)"></i>raw (as signed, always kept)</span>'
                '<span><i style="background:var(--released)"></i>released downstream (dashed)</span>'
                '<span><i class="dot" style="background:var(--suspicious)"></i>Layer 3 alert</span>'
                '<span style="color:var(--quarantined)">✕ Layer 1 failure</span></div>'
                f'<div class="chart">{chart}</div>'
                '<div class="legend"><span><i style="background:var(--trusted)"></i>trusted</span>'
                '<span><i style="background:var(--suspicious)"></i>suspicious</span>'
                '<span><i style="background:var(--quarantined)"></i>quarantined</span><span>line: trust score</span></div>'
                f'<div class="chart">{state_chart(history)}</div>'
                "<h2>Decisions (latest 200)</h2>" + table(["time", "state", "category", "actions", "explanation"], rows))
        return page(sensor_id, "", body)

    @app.get("/provenance", response_class=HTMLResponse)
    def provenance():
        if not reader.exists():
            return missing()
        rows = [[e(x["ts"][:19]), sensor_link(x["sensor_id"]) if x["sensor_id"] else '<span class="muted">none</span>',
                 f'<strong>{e(x["status"])}</strong>', e(x.get("quantity")), e(x.get("timestamp")), e(x.get("sequence")),
                 e(x.get("value")), e(x.get("event_id"))] for x in reader.events(["provenance_event"])]
        body = (chain_banner() + '<p class="note">Layer 1 rejected these messages (bad signature, replay, revoked key, out of '
                "window, chain anomaly, unknown sensor, malformed). A rejected value is never released. One failure flags the "
                "sensor; 3 within 2 hours, or one together with a Layer 3 alert, quarantine it.</p>"
                + table(["checked at", "sensor", "status", "quantity", "message timestamp", "sequence", "value", "L1 event"], rows))
        return page("Provenance failures", "/provenance", body)

    @app.get("/suspicious", response_class=HTMLResponse)
    def suspicious():
        if not reader.exists():
            return missing()
        decisions = reader.decisions(where="json_extract(payload, '$.category') = 'authentic_but_suspicious'", limit=500)
        rows = [[short_ts(d["ts"]), sensor_link(d["sensor_id"]), e(", ".join(v["variable"] for v in d["evidence"] if v["alert"])),
                 badge(d["state"]), e(d.get("alert_count_short", 0)), e(d.get("alert_count_long", 0)), e(", ".join(d["actions"]))]
                for d in decisions]
        body = (chain_banner() + '<p class="note">Valid signature but Layer 3 alerted: possible physical spoofing or a '
                "compromised node with valid keys.</p>"
                + table(["time", "sensor", "alerted variables", "state", "alerts / 6 h", "alerts / 12 h", "actions"], rows, numeric=(4, 5)))
        return page("Authentic but suspicious", "/suspicious", body)

    def release_page(message=""):
        held = [d for d in reader.latest() if d["state"] == "quarantined" and d["cause"] == "provenance"]
        token_field = ('<label>Release token<input type="password" name="token" required></label>' if release_token else "")
        if held:
            options = "".join(f'<option value="{e(d["sensor_id"])}">{e(d["sensor_id"])}</option>' for d in held)
            form = (f'<form method="post" action="/release" class="inline"><label>Sensor<select name="sensor_id">{options}</select></label>'
                    '<label>Analyst name<input name="analyst" required maxlength="100"></label>'
                    '<label>Reason (what was checked)<textarea name="reason" required maxlength="1000"></textarea></label>'
                    f'{token_field}<button type="submit">Request release</button></form>')
        else:
            form = '<div class="banner ok">No sensor is waiting for an analyst release.</div>'
        history = [[e(x["ts"][:19]), e(x["kind"]), sensor_link(x["sensor_id"]) if x["sensor_id"] else "",
                    e(x.get("analyst", "")), e(x.get("reason", "")), e(x.get("previous_cause", ""))]
                   for x in reader.events(["analyst_release", "analyst_release_applied", "analyst_release_ignored"])]
        body = (chain_banner() + message + '<p class="note">Sensors quarantined after repeated Layer 1 failures stay quarantined '
                "until an analyst releases them. The request is written to the audit log and applied by Layer 4 at its next tick.</p>"
                + form + "<h2>Release history</h2>" + table(["time", "entry", "sensor", "analyst", "reason", "previous cause"], history))
        return page("Analyst release", "/release", body, refresh=0)

    @app.get("/release", response_class=HTMLResponse)
    def release_form(submitted: int = 0):
        if not reader.exists():
            return missing()
        done = ('<div class="banner ok">Release request recorded in the audit log. Layer 4 applies it at its next tick.</div>'
                if submitted else "")
        return release_page(done)

    @app.post("/release")
    async def release_submit(request: Request):
        if not reader.exists():
            return missing()
        form = {k: v[0].strip() for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
        sensor_id, analyst, reason = form.get("sensor_id", ""), form.get("analyst", ""), form.get("reason", "")
        if release_token and not hmac.compare_digest(form.get("token", ""), release_token):
            return release_page('<div class="banner error">Wrong release token.</div>')
        held = {d["sensor_id"] for d in reader.latest() if d["state"] == "quarantined" and d["cause"] == "provenance"}
        if sensor_id not in held:
            return release_page('<div class="banner error">That sensor is not waiting for a release.</div>')
        if not analyst or not reason or len(analyst) > 100 or len(reason) > 1000:
            return release_page('<div class="banner error">Analyst name and reason are both required.</div>')
        with AuditLog(reader.path) as log:
            log.append(datetime.now().astimezone().isoformat(), "analyst_release",
                       {"analyst": analyst, "reason": reason}, sensor_id=sensor_id)
        return RedirectResponse("/release?submitted=1", status_code=303)

    @app.get("/audit", response_class=HTMLResponse)
    def audit():
        if not reader.exists():
            return missing()
        rows = [[e(s), e(t[:19]), e(k), e(sid or ""), f'<span class="mono">{e(p[:16])}…</span>',
                 f'<span class="mono">{e(h[:16])}…</span>'] for s, t, k, sid, p, h in reader.entries()]
        body = (chain_banner() + '<p class="note">Append-only: SQLite triggers reject updates and deletes, and each hash covers '
                "the previous hash and the entry. Showing the latest 500 entries.</p>"
                + table(["seq", "time", "kind", "sensor", "previous hash", "hash"], rows, numeric=(0,)))
        return page("Audit log", "/audit", body)

    # ------------------------------------------------------------------------------------- json

    @app.get("/api/sensors")
    def api_sensors():
        return reader.latest() if reader.exists() else []

    @app.get("/api/sensors/{sensor_id}/decisions")
    def api_decisions(sensor_id: str, limit: int = 200):
        return reader.decisions(sensor_id, limit=max(1, min(limit, 5000))) if reader.exists() else []

    @app.get("/api/released")
    def api_released(after: int = 0, limit: int = 5000):
        """Downstream feed: only raw values of trusted sensors or imputed estimates; poll with ``after``."""
        return reader.released(after, max(1, min(limit, 50000))) if reader.exists() else []

    @app.get("/api/chain")
    def api_chain():
        if not reader.exists():
            return JSONResponse({"error": "audit log not found"}, status_code=404)
        ok, count, bad = reader.verify()
        return {"ok": ok, "entries": count, "first_bad_seq": bad}

    return app


app = create_app(os.environ.get("CIS_AUDIT_DB", ROOT / "local/layer4-demo.sqlite3"),
                 os.environ.get("CIS_RELEASE_TOKEN") or None)


def main(argv=None):
    import uvicorn
    parser = argparse.ArgumentParser(description="Layer 4 analyst dashboard")
    parser.add_argument("--db", default=os.environ.get("CIS_AUDIT_DB", str(ROOT / "local/layer4-demo.sqlite3")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(args.db, os.environ.get("CIS_RELEASE_TOKEN") or None), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
