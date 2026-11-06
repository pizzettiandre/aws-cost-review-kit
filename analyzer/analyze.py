#!/usr/bin/env python3
"""
Turns the anonymized data collected by collect.py into a first-draft findings list
and a client-ready report (HTML + PDF). Every finding must be reviewed by hand before delivery.

Usage:
  python3 analyze.py review_acme_2026-10-30_anonymized.zip --client "Acme Shop" --package premium
  (--package basic | standard | premium; premium adds the step-by-step fix guide)
"""
import argparse
import datetime as dt
import html
import json
import os
import sys
import zipfile

# Approximate on-demand prices, us-east-1, Linux (USD/hour). Used only when Compute Optimizer
# has no recommendation. Estimates — always double-check big numbers in the AWS Pricing Calculator.
EC2_HOURLY = {
    "t2.micro": .0116, "t2.small": .023, "t2.medium": .0464, "t2.large": .0928, "t2.xlarge": .1856,
    "t3.micro": .0104, "t3.small": .0208, "t3.medium": .0416, "t3.large": .0832, "t3.xlarge": .1664,
    "t3.2xlarge": .3328, "t3a.medium": .0376, "t3a.large": .0752, "t3a.xlarge": .1504,
    "m4.large": .10, "m4.xlarge": .20, "m4.2xlarge": .40,
    "m5.large": .096, "m5.xlarge": .192, "m5.2xlarge": .384, "m5.4xlarge": .768,
    "m6i.large": .096, "m6i.xlarge": .192, "m6i.2xlarge": .384, "m7i.large": .1008, "m7i.xlarge": .2016,
    "c4.large": .10, "c4.xlarge": .199, "c5.large": .085, "c5.xlarge": .17, "c5.2xlarge": .34,
    "c6i.large": .085, "c6i.xlarge": .17, "r4.large": .133, "r5.large": .126, "r5.xlarge": .252,
    "r5.2xlarge": .504, "r6i.large": .126, "r6i.xlarge": .252,
}
EBS_GB_MONTH = {"gp2": .10, "gp3": .08, "io1": .125, "io2": .125, "st1": .045, "sc1": .015, "standard": .05}
SNAPSHOT_GB_MONTH = .05
EIP_MONTH = 3.65
ALB_MONTH = 16.43
HOURS = 730
OLD_GEN = ("t2.", "m4.", "c4.", "r4.", "m3.", "c3.", "r3.")
COMPUTE_SERVICES = ("Amazon Elastic Compute Cloud - Compute", "AWS Lambda", "Amazon Elastic Container Service",
                    "AWS Fargate")


def load(path):
    if path.endswith(".zip"):
        return json.loads(zipfile.ZipFile(path).read("data.json"))
    return json.load(open(path))


def name_of(inst):
    for t in inst.get("Tags", []) or []:
        if t.get("Key") == "Name":
            return t.get("Value")
    return inst.get("InstanceId")


def smaller(itype):
    sizes = ["nano", "micro", "small", "medium", "large", "xlarge", "2xlarge", "4xlarge", "8xlarge"]
    fam, size = itype.split(".", 1)
    if size in sizes and sizes.index(size) > 0:
        return f"{fam}.{sizes[sizes.index(size) - 1]}"
    return None


def money(x):
    return f"${x:,.0f}"


def monthly_totals(cost):
    out = []
    for m in cost.get("monthly_by_service", []):
        total = sum(float(g["Metrics"]["UnblendedCost"]["Amount"]) for g in m.get("Groups", []))
        comp = sum(float(g["Metrics"]["UnblendedCost"]["Amount"]) for g in m.get("Groups", [])
                   if g["Keys"][0] in COMPUTE_SERVICES)
        out.append({"start": m["TimePeriod"]["Start"], "end": m["TimePeriod"]["End"],
                    "total": total, "compute": comp, "estimated": m.get("Estimated", False)})
    return out


def analyze(d):
    F = []

    def add(**kw):
        kw.setdefault("savings", 0.0)
        kw.setdefault("resources", [])
        F.append(kw)

    regions = d.get("regions", {})

    # ---------------- COST: EC2 rightsizing / idle
    co_recs = {}
    for r in regions.values():
        for rec in r.get("compute_optimizer", []):
            iid = rec.get("instanceArn", "").split("/")[-1]
            co_recs[iid] = rec
    rightsize, idle, oldgen = [], [], []
    for reg, r in regions.items():
        for inst in r.get("instances", []):
            if inst.get("State", {}).get("Name") != "running":
                continue
            iid, itype = inst["InstanceId"], inst.get("InstanceType", "")
            cpu = r.get("cpu", {}).get(iid, {})
            price = EC2_HOURLY.get(itype)
            monthly = price * HOURS if price else None
            label = f"{name_of(inst)} ({itype}, {reg})"
            if itype.startswith(OLD_GEN):
                oldgen.append((label, (monthly or 0) * 0.10))
            if not cpu:
                continue
            avg, mx = cpu.get("avg", 100), cpu.get("max", 100)
            if mx < 5:
                idle.append((label + f" — max CPU {mx}%", monthly or 0))
            elif avg < 10 and mx < 40:
                rec = co_recs.get(iid)
                if rec and rec.get("recommendationOptions"):
                    opt = rec["recommendationOptions"][0]
                    target = opt.get("instanceType")
                    sav = float(opt.get("savingsOpportunity", {}).get("estimatedMonthlySavings", {}).get("value", 0))
                else:
                    target = smaller(itype)
                    sav = (monthly - EC2_HOURLY.get(target, price / 2) * HOURS) if monthly and target else 0
                rightsize.append((label + f" — avg CPU {avg}%, max {mx}% → {target or 'smaller size'}", sav))
    if idle:
        add(title=f"{len(idle)} EC2 instance(s) almost idle (CPU under 5% the whole period)", category="Cost",
            priority="High", savings=sum(s for _, s in idle), resources=[l for l, _ in idle],
            why="These servers are running and billed every hour but do practically no work.",
            fix=["Confirm with the owner if the instance is still needed (check its Name tag and recent logins).",
                 "If not needed: create an AMI or snapshot as a backup, then stop it for 2 weeks.",
                 "If nobody complains, terminate it and delete the backup after 30 days."])
    if rightsize:
        add(title=f"{len(rightsize)} EC2 instance(s) are oversized for their workload", category="Cost",
            priority="High", savings=sum(s for _, s in rightsize), resources=[l for l, _ in rightsize],
            why="Average CPU stayed under 10% during the period, so a smaller (cheaper) size can run the same load.",
            fix=["Pick a low-traffic window and create an AMI of the instance (rollback point).",
                 "Console: EC2 → Instances → select → Instance state → Stop.",
                 "Actions → Instance settings → Change instance type → choose the suggested size → Start.",
                 "Watch CPU and memory in CloudWatch for 1 week. If CPU stays above 70%, go back one size."])
    if oldgen:
        add(title=f"{len(oldgen)} instance(s) on older generations (t2/m4/c4/r4)", category="Cost", priority="Low",
            savings=sum(s for _, s in oldgen), resources=[l for l, _ in oldgen],
            why="Newer generations (t3, m6i, c6i, r6i) cost about the same or less and are faster.",
            fix=["Same procedure as a resize: AMI → stop → change instance type (e.g. t2.medium → t3.medium) → start.",
                 "Check the AMI supports the Nitro system (ENA and NVMe drivers) before changing."])

    # ---------------- COST: storage & network leftovers
    vols, snaps, eips, lbs = [], [], [], []
    old_cut = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=90)
    for reg, r in regions.items():
        for v in r.get("volumes", []):
            if v.get("State") == "available":
                c = v.get("Size", 0) * EBS_GB_MONTH.get(v.get("VolumeType"), .10)
                vols.append((f"{v['VolumeId']} ({v.get('Size')} GB {v.get('VolumeType')}, {reg})", c))
        old = [s for s in r.get("snapshots", [])
               if str(s.get("StartTime", ""))[:10] and str(s.get("StartTime"))[:10] < old_cut.date().isoformat()]
        if old:
            gb = sum(s.get("VolumeSize", 0) for s in old)
            snaps.append((f"{len(old)} snapshots older than 90 days in {reg} ({gb:,} GB)", gb * SNAPSHOT_GB_MONTH))
        for a in r.get("addresses", []):
            if not a.get("AssociationId"):
                eips.append((f"{a.get('AllocationId', 'Elastic IP')} ({reg})", EIP_MONTH))
        for lb in r.get("load_balancers", []):
            if lb.get("target_count", 1) == 0:
                lbs.append((f"{lb['name']} ({lb.get('type')}, {reg})", ALB_MONTH))
    if vols:
        add(title=f"{len(vols)} EBS volume(s) not attached to any server", category="Cost", priority="Medium",
            savings=sum(s for _, s in vols), resources=[l for l, _ in vols],
            why="Detached disks keep being billed every month even though nothing uses them.",
            fix=["Console: EC2 → Volumes → filter State = available.",
                 "For each volume: Actions → Create snapshot (cheap backup), then Actions → Delete volume."])
    if snaps:
        add(title="Old EBS snapshots that are probably no longer needed", category="Cost", priority="Medium",
            savings=sum(s for _, s in snaps), resources=[l for l, _ in snaps],
            why="Snapshots are incremental, so the real saving is usually lower than the estimate — but old ones add up.",
            fix=["Decide a retention rule (e.g. keep 30 daily + 12 monthly).",
                 "Use Amazon Data Lifecycle Manager or AWS Backup to apply it automatically.",
                 "Delete snapshots outside the rule (never the ones used by an AMI still in use)."])
    if eips:
        add(title=f"{len(eips)} Elastic IP(s) reserved but not in use", category="Cost", priority="Low",
            savings=sum(s for _, s in eips), resources=[l for l, _ in eips],
            why="AWS charges for every public IPv4 address, used or not.",
            fix=["Console: EC2 → Elastic IPs → select the unassociated address → Actions → Release."])
    if lbs:
        add(title=f"{len(lbs)} load balancer(s) with no servers behind them", category="Cost", priority="Medium",
            savings=sum(s for _, s in lbs), resources=[l for l, _ in lbs],
            why="A load balancer has a fixed hourly price even with zero traffic.",
            fix=["Confirm DNS records (Route 53 or your DNS provider) don't point to it.",
                 "Console: EC2 → Load Balancers → select → Actions → Delete."])

    # ---------------- COST: Savings Plans
    months = monthly_totals(d.get("cost", {}))
    full = [m for m in months if not m["estimated"]][-3:]
    sp_active = d.get("cost", {}).get("savings_plans", [])
    if full:
        already = sum(f["savings"] for f in F if "EC2 instance" in f["title"])  # resizing/idle first
        base = min(m["compute"] for m in full) - already
        if base >= 150 and not sp_active:
            commit = base * 0.7
            add(title="No Savings Plan for your steady compute usage", category="Cost", priority="High",
                savings=commit * 0.25, resources=[f"Steady compute spend after the resizing above: about {money(base)}/month"],
                why="A 1-year Compute Savings Plan typically gives 20-30% off EC2, Fargate and Lambda in exchange "
                    "for a fixed hourly commitment. Committing ~70% of your lowest month keeps the risk low.",
                fix=["Do the resizing and clean-up items first (they lower the baseline).",
                     "Console: Billing → Savings Plans → Recommendations → Compute Savings Plan, 1 year, No upfront.",
                     f"Start with a commitment around ${commit / HOURS:.2f}/hour or lower; you can add more later."])

    # ---------------- COST: S3
    big = [b for b in d.get("s3", []) if (b.get("standard_gb") or 0) >= 200 and b.get("lifecycle_rules") == 0]
    if big:
        sav = sum(b["standard_gb"] * .023 * .3 for b in big)
        add(title=f"{len(big)} large S3 bucket(s) with no lifecycle rule", category="Cost", priority="Low",
            savings=sav, resources=[f"{b['name']} ({b['standard_gb']:,} GB in Standard)" for b in big],
            why="Data that is rarely read costs less in S3 Intelligent-Tiering or Glacier classes.",
            fix=["S3 → bucket → Management → Create lifecycle rule.",
                 "Logs/backups: move to Glacier Instant Retrieval after 30 days, expire after 365 days.",
                 "Unknown access pattern: transition to Intelligent-Tiering after 0 days."])

    # ---------------- SECURITY
    acc = d.get("account", {})
    summary = acc.get("summary", {})
    root = next((u for u in acc.get("credential_report", []) if u.get("user") == "<root_account>"), {})
    if summary.get("AccountMFAEnabled") == 0 or root.get("mfa_active") == "false":
        add(title="Root account has no MFA", category="Security", priority="High",
            why="The root user can do anything, including closing the account. A leaked password is enough to lose it.",
            fix=["Sign in as root → account menu → Security credentials → Assign MFA device.",
                 "Use an authenticator app or a hardware key; store recovery codes safely."])
    if root.get("access_key_1_active") == "true" or root.get("access_key_2_active") == "true":
        add(title="Root account has active access keys", category="Security", priority="High",
            why="Root keys give unlimited programmatic access and cannot be restricted by policies.",
            fix=["Find where the key is used (IAM credential report → last used).",
                 "Replace it with an IAM role or a least-privilege IAM user, then delete the root key."])
    no_mfa = [u["user"] for u in acc.get("credential_report", [])
              if u.get("user") != "<root_account>" and u.get("password_enabled") == "true"
              and u.get("mfa_active") == "false"]
    if no_mfa:
        add(title=f"{len(no_mfa)} console user(s) without MFA", category="Security", priority="Medium",
            resources=no_mfa,
            why="Users who log in to the console with only a password are the most common way accounts get compromised.",
            fix=["Ask each user to enable MFA (IAM → Users → Security credentials).",
                 "Optional: add a policy that denies actions when MFA is not present."])
    old_keys = []
    cut = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=90)).date().isoformat()
    for u in acc.get("credential_report", []):
        for k in ("1", "2"):
            rot = u.get(f"access_key_{k}_last_rotated", "N/A")
            if u.get(f"access_key_{k}_active") == "true" and rot not in ("N/A", "") and rot[:10] < cut:
                old_keys.append(f"{u['user']} (key {k}, last rotated {rot[:10]})")
    if old_keys:
        add(title=f"{len(old_keys)} access key(s) older than 90 days", category="Security", priority="Low",
            resources=old_keys, why="Long-lived keys are often forgotten in laptops, scripts and repositories.",
            fix=["Create a new key, update the application, deactivate the old key, wait 1 week, delete it.",
                 "Better: use IAM roles (EC2 instance profiles, ECS task roles) instead of keys."])
    no_pab = [b["name"] for b in d.get("s3", []) if b.get("public_access_block") == {}]
    if no_pab:
        add(title=f"{len(no_pab)} S3 bucket(s) without Block Public Access", category="Security", priority="Medium",
            resources=no_pab, why="Without this setting a single wrong policy or ACL can expose files to the internet.",
            fix=["If the bucket does not serve a public website: S3 → bucket → Permissions → Block public access → "
                 "Edit → enable all four options.", "Even better: enable it at account level (S3 → Block Public "
                 "Access settings for this account)."])
    pub_db = [f"{db['DBInstanceIdentifier']} ({reg})" for reg, r in regions.items() for db in r.get("rds", [])
              if db.get("PubliclyAccessible")]
    if pub_db:
        add(title=f"{len(pub_db)} database(s) reachable from the internet", category="Security", priority="High",
            resources=pub_db, why="Databases should only accept connections from your application servers.",
            fix=["RDS → database → Modify → Connectivity → Public access: No.",
                 "Check the security group allows the DB port only from the app's security group."])
    trails = d.get("cloudtrail", [])
    if not any(t.get("is_logging") for t in trails):
        add(title="No active CloudTrail (audit log)", category="Security", priority="Medium",
            why="Without CloudTrail there is no record of who changed what — essential after any incident.",
            fix=["CloudTrail → Create trail → apply to all regions → new S3 bucket. The first trail is free "
                 "(you only pay S3 storage)."])
    gd_off = [reg for reg, r in regions.items() if r.get("guardduty_enabled") is False]
    if gd_off:
        add(title="GuardDuty threat detection is off", category="Security", priority="Low", resources=gd_off,
            why="GuardDuty spots crypto-mining, leaked keys and unusual logins; it usually costs a few dollars/month.",
            fix=["GuardDuty → Get started → Enable (30-day free trial shows the real cost)."])

    # ---------------- RELIABILITY
    no_backup = [f"{db['DBInstanceIdentifier']} ({reg})" for reg, r in regions.items() for db in r.get("rds", [])
                 if db.get("BackupRetentionPeriod", 1) == 0]
    if no_backup:
        add(title=f"{len(no_backup)} database(s) without automated backups", category="Reliability", priority="High",
            resources=no_backup, why="If data is deleted or corrupted, there is no point-in-time restore.",
            fix=["RDS → database → Modify → Backup retention period: 7 days (or more) → Apply immediately."])
    single = [f"{db['DBInstanceIdentifier']} ({reg})" for reg, r in regions.items() for db in r.get("rds", [])
              if not db.get("MultiAZ") and db.get("BackupRetentionPeriod", 0) > 0
              and "dev" not in db["DBInstanceIdentifier"] and "test" not in db["DBInstanceIdentifier"]]
    if single:
        add(title=f"{len(single)} production database(s) in a single Availability Zone", category="Reliability",
            priority="Medium", resources=single,
            why="If that zone has a problem, the database is down until AWS recovers it.",
            fix=["Decide if the business can accept a few hours of downtime.",
                 "If not: RDS → Modify → Multi-AZ deployment: Yes (roughly doubles the DB instance cost)."])
    unenc = [f"{db['DBInstanceIdentifier']} ({reg})" for reg, r in regions.items() for db in r.get("rds", [])
             if db.get("StorageEncrypted") is False]
    if unenc:
        add(title=f"{len(unenc)} database(s) with unencrypted storage", category="Security", priority="Low",
            resources=unenc, why="Encryption at rest is required by most compliance frameworks and is free.",
            fix=["Encryption can't be turned on in place: take a snapshot → copy it with encryption → restore.",
                 "Plan a short maintenance window."])

    order = {"High": 0, "Medium": 1, "Low": 2}
    F.sort(key=lambda f: (order[f["priority"]], -f["savings"]))
    for n, f in enumerate(F, 1):
        f["id"] = n
    return F, months


# ---------------------------------------------------------------- report
CSS = """
*{box-sizing:border-box}body{font-family:Poppins,Helvetica,Arial,sans-serif;color:#14233a;margin:0;font-size:12.5px}
.page{padding:42px 48px;page-break-after:always}.page:last-child{page-break-after:auto}
.top{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:3px solid #ff9f43;padding-bottom:14px}
h1{font-size:25px;margin:0}h2{font-size:17px;margin:26px 0 8px}.sub{color:#5b6b82;font-size:12px;margin-top:3px}
.brand{text-align:right;font-size:11px;color:#5b6b82}.brand b{color:#14233a;font-size:13px}
.kpis{display:flex;gap:12px;margin-top:20px}.k{flex:1;background:#f3f6fa;border-radius:10px;padding:11px 14px}
.k b{display:block;font-size:22px}.k small{color:#5b6b82;font-size:11px}.g{color:#0f9d7a}.r{color:#d9534f}
table{width:100%;border-collapse:collapse;margin-top:8px}th{text-align:left;font-size:10px;letter-spacing:.8px;
text-transform:uppercase;color:#5b6b82;padding:6px 8px;border-bottom:2px solid #e3e9f1}td{padding:8px;border-bottom:1px solid #eef2f7;vertical-align:top}
.p{display:inline-block;font-size:10px;font-weight:700;padding:2px 9px;border-radius:999px}
.High{background:#fde3df;color:#b23b2e}.Medium{background:#fff1d6;color:#9a6200}.Low{background:#e2f5ee;color:#0f7a5f}
.f{border:1px solid #e3e9f1;border-radius:10px;padding:14px 16px;margin-top:12px;page-break-inside:avoid}
.f h3{margin:0 0 4px;font-size:14px}.meta{color:#5b6b82;font-size:11px;margin-bottom:6px}
ul,ol{margin:6px 0 0 18px;padding:0}li{margin:2px 0}.note{color:#5b6b82;font-size:10.5px;margin-top:22px;line-height:1.5}
.bar{display:flex;gap:6px;align-items:flex-end;height:90px;margin-top:6px}.bar div{background:#ff9f43;border-radius:4px 4px 0 0;
width:60px;position:relative}.bar span{position:absolute;top:-16px;width:100%;text-align:center;font-size:10px}
.lbl{display:flex;gap:6px}.lbl div{width:60px;text-align:center;font-size:10px;color:#5b6b82}
"""


def esc(s):
    return html.escape(str(s))


def render(F, months, client, package, author):
    savings = sum(f["savings"] for f in F)
    risks = sum(1 for f in F if f["category"] != "Cost")
    full = [m for m in months if not m["estimated"]]
    last = full[-1]["total"] if full else 0
    today = dt.date.today().strftime("%B %d, %Y")
    pkg = {"basic": "Cost Check", "standard": "Cost + Architecture", "premium": "Review + Fix Guide"}[package]
    shown = F if package != "basic" else [f for f in F if f["category"] == "Cost"]
    rows = "".join(
        f"<tr><td>{f['id']}</td><td>{esc(f['title'])}</td><td>{f['category']}</td>"
        f"<td>{money(f['savings']) if f['savings'] else '—'}</td><td><span class='p {f['priority']}'>{f['priority']}</span></td></tr>"
        for f in shown)
    mx = max([m["total"] for m in full] or [1])
    bars = "".join(f"<div style='height:{max(6, m['total'] / mx * 80):.0f}px'><span>{money(m['total'])}</span></div>"
                   for m in full[-4:])
    lbls = "".join(f"<div>{dt.date.fromisoformat(m['start']).strftime('%b %Y')}</div>" for m in full[-4:])
    head = (f"<div class='top'><div><h1>AWS {esc(pkg)} — {esc(client)}</h1>"
            f"<div class='sub'>{today} · {len(F)} findings · read-only review</div></div>"
            f"<div class='brand'><b>{esc(author)}</b><br>AWS Certified Solutions Architect</div></div>")
    p1 = (f"<div class='page'>{head}<div class='kpis'>"
          f"<div class='k'><small>Last full month bill</small><b>{money(last)}</b></div>"
          f"<div class='k'><small>Estimated monthly savings</small><b class='g'>{money(savings)}</b></div>"
          f"<div class='k'><small>Estimated yearly savings</small><b class='g'>{money(savings * 12)}</b></div>"
          f"<div class='k'><small>Security / reliability risks</small><b class='r'>{risks}</b></div></div>"
          f"<h2>Monthly bill</h2><div class='bar'>{bars}</div><div class='lbl'>{lbls}</div>"
          f"<h2>Findings, in order of priority</h2><table><tr><th>#</th><th>Finding</th><th>Area</th>"
          f"<th>Est. savings / mo</th><th>Priority</th></tr>{rows}</table>"
          f"<p class='note'>Savings are estimates based on list prices and the usage observed during the review "
          f"period. Actual results depend on your workload and discounts. This review was prepared with the help of "
          f"automated and AI-assisted analysis on anonymized data; every finding was checked by hand. No changes were "
          f"made to your account.</p></div>")
    det = ""
    for f in shown:
        res = "".join(f"<li>{esc(r)}</li>" for r in f["resources"][:12])
        more = f"<li>… and {len(f['resources']) - 12} more</li>" if len(f["resources"]) > 12 else ""
        steps = ""
        if package == "premium":
            steps = "<b>How to fix it, step by step</b><ol>" + "".join(f"<li>{esc(s)}</li>" for s in f["fix"]) + "</ol>"
        elif package == "standard":
            steps = f"<b>Recommended action</b><ul><li>{esc(f['fix'][0])}</li></ul>"
        det += (f"<div class='f'><h3>{f['id']}. {esc(f['title'])}</h3><div class='meta'><span class='p {f['priority']}'>"
                f"{f['priority']}</span> &nbsp;{f['category']}"
                f"{' · est. ' + money(f['savings']) + '/month' if f['savings'] else ''}</div>"
                f"<div>{esc(f['why'])}</div>{'<b>Affected resources</b><ul>' + res + more + '</ul>' if res else ''}"
                f"{steps}</div>")
    p2 = f"<div class='page'>{head}<h2>Details</h2>{det}</div>"
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{p1}{p2}</body></html>"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--client", required=True)
    ap.add_argument("--package", choices=["basic", "standard", "premium"], default="standard")
    ap.add_argument("--author", default="André Luís Pizzetti")
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    F, months = analyze(load(a.data))
    os.makedirs(a.out, exist_ok=True)
    slug = a.client.lower().replace(" ", "_")
    json.dump(F, open(os.path.join(a.out, f"findings_{slug}.json"), "w"), indent=1)
    page = render(F, months, a.client, a.package, a.author)
    hp = os.path.join(a.out, f"aws_review_{slug}.html")
    open(hp, "w").write(page)
    print(f"{len(F)} findings · est. savings {money(sum(f['savings'] for f in F))}/month")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.goto("file://" + os.path.abspath(hp))
            pg.pdf(path=hp.replace(".html", ".pdf"), format="A4", print_background=True,
                   margin={"top": "10mm", "bottom": "12mm", "left": "0", "right": "0"})
            b.close()
        print("PDF:", hp.replace(".html", ".pdf"))
    except Exception as e:
        print("HTML only (install playwright for PDF):", hp, "|", e)


if __name__ == "__main__":
    main()
