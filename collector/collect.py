#!/usr/bin/env python3
"""
Read-only AWS data collector for cost & architecture reviews.

- Assumes the client's read-only role (created with readonly-review-role.yaml).
- Only calls read operations (describe/list/get). Any other call is blocked in code.
- Anonymizes the output (account IDs, IPs, e-mails, IAM user names) before zipping.

Usage:
  python3 collect.py --role-arn arn:aws:iam::123456789012:role/ReadOnlyCostArchitectureReview \
                     --external-id "the-secret-phrase" --client acme

Requires: python3 -m pip install boto3
Your own AWS credentials (the reviewer account) must be configured, e.g. `aws configure --profile reviewer`.
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import zipfile

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

TOOL_VERSION = "1.0"
ALLOWED_PREFIXES = ("describe_", "list_", "get_", "generate_credential_report", "can_paginate")
BOTO_CFG = Config(retries={"max_attempts": 8, "mode": "adaptive"}, read_timeout=60)
ERRORS = []


class ReadOnlyClient:
    """Wraps a boto3 client and refuses any call that is not a read operation."""

    def __init__(self, client):
        self._c = client

    def __getattr__(self, name):
        attr = getattr(self._c, name)
        if callable(attr) and not name.startswith(ALLOWED_PREFIXES) and name not in ("meta",):
            raise PermissionError(f"Blocked non read-only call: {name}")
        return attr


class Session:
    def __init__(self, boto_session):
        self.s = boto_session

    def client(self, service, region=None):
        return ReadOnlyClient(self.s.client(service, region_name=region, config=BOTO_CFG))


def safe(label, fn, default=None):
    try:
        return fn()
    except Exception as e:  # one failed check must never stop the whole collection
        ERRORS.append({"where": label, "error": str(e)[:300]})
        return default


def paginate(client, op, key, **kw):
    out = []
    if client.can_paginate(op):
        for page in client.get_paginator(op).paginate(**kw):
            out.extend(page.get(key, []))
    else:
        out.extend(getattr(client, op)(**kw).get(key, []))
    return out


def month_start(d):
    return d.replace(day=1)


# ---------------------------------------------------------------- collectors
def collect_account(sess):
    iam = sess.client("iam")
    sts = sess.client("sts")
    ident = safe("sts", lambda: sts.get_caller_identity(), {})
    summary = safe("iam.summary", lambda: iam.get_account_summary()["SummaryMap"], {})
    report = []

    def cred_report():
        for _ in range(10):
            r = iam.generate_credential_report()
            if r.get("State") == "COMPLETE":
                break
            time.sleep(2)
        raw = iam.get_credential_report()["Content"].decode()
        return list(csv.DictReader(io.StringIO(raw)))

    report = safe("iam.credential_report", cred_report, [])
    return {"account_id": ident.get("Account"), "summary": summary, "credential_report": report}


def collect_cost(sess):
    ce = sess.client("ce", "us-east-1")
    today = dt.date.today()
    start = month_start(month_start(today) - dt.timedelta(days=80))  # 3 full months back
    end = today
    out = {}
    out["monthly_by_service"] = safe("ce.by_service", lambda: ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY", Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}])["ResultsByTime"], [])
    last_start = month_start(month_start(today) - dt.timedelta(days=1))
    out["last_month_by_usage_type"] = safe("ce.by_usage", lambda: ce.get_cost_and_usage(
        TimePeriod={"Start": last_start.isoformat(), "End": month_start(today).isoformat()},
        Granularity="MONTHLY", Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}])["ResultsByTime"], [])
    p30 = {"Start": (today - dt.timedelta(days=30)).isoformat(), "End": today.isoformat()}
    out["savings_plans_coverage"] = safe("ce.sp_coverage", lambda: ce.get_savings_plans_coverage(
        TimePeriod=p30)["SavingsPlansCoverages"], [])
    out["reservation_coverage"] = safe("ce.ri_coverage", lambda: ce.get_reservation_coverage(
        TimePeriod=p30)["Total"], {})
    sp = sess.client("savingsplans", "us-east-1")
    out["savings_plans"] = safe("savingsplans", lambda: sp.describe_savings_plans(
        states=["active"])["savingsPlans"], [])
    return out


def cpu_stats(cw, instance_ids, days):
    stats = {}
    end = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    start = end - dt.timedelta(days=days)
    for i in range(0, len(instance_ids), 200):
        chunk = instance_ids[i:i + 200]
        queries = []
        for n, iid in enumerate(chunk):
            for stat in ("Average", "Maximum"):
                queries.append({
                    "Id": f"{stat[:3].lower()}{n}",
                    "MetricStat": {"Metric": {"Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
                                              "Dimensions": [{"Name": "InstanceId", "Value": iid}]},
                                   "Period": 3600, "Stat": stat}})
        for q in range(0, len(queries), 500):
            res = cw.get_metric_data(MetricDataQueries=queries[q:q + 500], StartTime=start, EndTime=end)
            for r in res["MetricDataResults"]:
                n = int(r["Id"][3:])
                iid = chunk[n]
                vals = r.get("Values", [])
                if not vals:
                    continue
                s = stats.setdefault(iid, {})
                if r["Id"].startswith("ave"):
                    s["avg"] = round(sum(vals) / len(vals), 2)
                else:
                    s["max"] = round(max(vals), 2)
    return stats


def collect_region(sess, region, days):
    ec2 = sess.client("ec2", region)
    d = {"region": region}
    reservations = safe(f"{region}.instances", lambda: paginate(ec2, "describe_instances", "Reservations"), [])
    d["instances"] = [i for r in reservations for i in r.get("Instances", [])]
    d["volumes"] = safe(f"{region}.volumes", lambda: paginate(ec2, "describe_volumes", "Volumes"), [])
    d["snapshots"] = safe(f"{region}.snapshots", lambda: paginate(
        ec2, "describe_snapshots", "Snapshots", OwnerIds=["self"]), [])
    d["addresses"] = safe(f"{region}.eips", lambda: ec2.describe_addresses()["Addresses"], [])
    d["nat_gateways"] = safe(f"{region}.nat", lambda: paginate(ec2, "describe_nat_gateways", "NatGateways"), [])
    running = [i["InstanceId"] for i in d["instances"] if i.get("State", {}).get("Name") == "running"]
    cw = sess.client("cloudwatch", region)
    d["cpu"] = safe(f"{region}.cpu", lambda: cpu_stats(cw, running, days), {}) if running else {}
    rds = sess.client("rds", region)
    d["rds"] = safe(f"{region}.rds", lambda: paginate(rds, "describe_db_instances", "DBInstances"), [])
    elb = sess.client("elbv2", region)

    def lbs():
        out = []
        for lb in paginate(elb, "describe_load_balancers", "LoadBalancers"):
            tgs = elb.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]
            targets = 0
            for tg in tgs:
                targets += len(elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
                               ["TargetHealthDescriptions"])
            out.append({"name": lb["LoadBalancerName"], "type": lb.get("Type"),
                        "created": lb.get("CreatedTime"), "target_count": targets})
        return out

    d["load_balancers"] = safe(f"{region}.elb", lbs, [])
    gd = sess.client("guardduty", region)
    d["guardduty_enabled"] = safe(f"{region}.guardduty", lambda: bool(gd.list_detectors()["DetectorIds"]), None)
    co = sess.client("compute-optimizer", region)
    d["compute_optimizer"] = safe(f"{region}.compute_optimizer", lambda: co.get_ec2_instance_recommendations()
                                  ["instanceRecommendations"], [])
    return d


def collect_s3(sess):
    s3 = sess.client("s3")
    buckets = safe("s3.list", lambda: s3.list_buckets()["Buckets"], [])
    out = []
    for b in buckets:
        name = b["Name"]
        loc = safe(f"s3.{name}.location", lambda: s3.get_bucket_location(Bucket=name)["LocationConstraint"], None)
        region = loc or "us-east-1"
        item = {"name": name, "region": region, "created": b.get("CreationDate")}
        def not_configured(fn, missing_code, missing_value):
            try:
                return fn()
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == missing_code:
                    return missing_value
                raise

        item["public_access_block"] = safe(f"s3.{name}.pab", lambda: not_configured(
            lambda: s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"],
            "NoSuchPublicAccessBlockConfiguration", {}), None)  # {} = not configured, None = unknown
        rules = safe(f"s3.{name}.lifecycle", lambda: not_configured(
            lambda: s3.get_bucket_lifecycle_configuration(Bucket=name)["Rules"],
            "NoSuchLifecycleConfiguration", []), None)
        item["lifecycle_rules"] = None if rules is None else len(rules)
        cw = sess.client("cloudwatch", region)
        end = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

        def size():
            r = cw.get_metric_statistics(Namespace="AWS/S3", MetricName="BucketSizeBytes",
                                         Dimensions=[{"Name": "BucketName", "Value": name},
                                                     {"Name": "StorageType", "Value": "StandardStorage"}],
                                         StartTime=end - dt.timedelta(days=3), EndTime=end,
                                         Period=86400, Statistics=["Average"])
            pts = sorted(r["Datapoints"], key=lambda p: p["Timestamp"])
            return round(pts[-1]["Average"] / 1024 ** 3, 1) if pts else 0

        item["standard_gb"] = safe(f"s3.{name}.size", size, None)
        out.append(item)
    return out


def collect_cloudtrail(sess, region):
    ct = sess.client("cloudtrail", region)
    trails = safe("cloudtrail", lambda: ct.describe_trails()["trailList"], [])
    for t in trails:
        t["is_logging"] = safe("cloudtrail.status", lambda: ct.get_trail_status(Name=t["TrailARN"])["IsLogging"], None)
    return trails


# ---------------------------------------------------------------- anonymization
class Anonymizer:
    def __init__(self):
        self.map = {}
        self.counters = {}

    def alias(self, kind, value):
        key = (kind, value)
        if key not in self.map:
            self.counters[kind] = self.counters.get(kind, 0) + 1
            self.map[key] = f"{kind}-{self.counters[kind]:02d}"
        return self.map[key]

    def text(self, s):
        s = re.sub(r"(?<!\d)\d{12}(?!\d)", lambda m: self.alias("ACCOUNT", m.group()), s)
        s = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "x.x.x.x", s)
        s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "redacted@email", s)
        return s

    def walk(self, obj):
        if isinstance(obj, dict):
            return {k: self.walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.walk(v) for v in obj]
        if isinstance(obj, str):
            return self.text(obj)
        return obj


def anonymize(data, anon):
    for row in data["account"].get("credential_report", []):
        if row.get("user") != "<root_account>":
            row["user"] = anon.alias("USER", row["user"])
            row["arn"] = ""
    return anon.walk(data)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role-arn", required=True)
    ap.add_argument("--external-id", required=True)
    ap.add_argument("--client", required=True, help="short client name for the output folder, e.g. acme")
    ap.add_argument("--profile", default=None, help="your own AWS CLI profile (reviewer account)")
    ap.add_argument("--regions", default="", help="comma-separated regions (default: all enabled)")
    ap.add_argument("--days", type=int, default=14, help="days of CPU metrics (default 14)")
    args = ap.parse_args()

    base = boto3.Session(profile_name=args.profile)
    creds = base.client("sts").assume_role(RoleArn=args.role_arn, RoleSessionName="cost-review",
                                           ExternalId=args.external_id, DurationSeconds=3600)["Credentials"]
    sess = Session(boto3.Session(aws_access_key_id=creds["AccessKeyId"],
                                 aws_secret_access_key=creds["SecretAccessKey"],
                                 aws_session_token=creds["SessionToken"]))
    print("✓ Read-only role assumed")

    if args.regions:
        regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    else:
        regions = safe("regions", lambda: [r["RegionName"] for r in
                                           sess.client("ec2", "us-east-1").describe_regions()["Regions"]], ["us-east-1"])

    data = {"meta": {"tool_version": TOOL_VERSION, "collected_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
                     "cpu_days": args.days, "regions": regions}}
    data["account"] = collect_account(sess)
    print("✓ Account and IAM")
    data["cost"] = collect_cost(sess)
    print("✓ Cost Explorer")
    data["s3"] = collect_s3(sess)
    print(f"✓ S3 ({len(data['s3'])} buckets)")
    data["cloudtrail"] = collect_cloudtrail(sess, regions[0] if regions else "us-east-1")
    data["regions"] = {}
    for r in regions:
        rd = collect_region(sess, r, args.days)
        n = len(rd["instances"]) + len(rd["volumes"]) + len(rd["rds"]) + len(rd["load_balancers"])
        if n or rd["snapshots"] or rd["addresses"] or rd["nat_gateways"]:
            data["regions"][r] = rd
        print(f"✓ {r}: {len(rd['instances'])} EC2, {len(rd['volumes'])} EBS, {len(rd['rds'])} RDS")
    data["errors"] = ERRORS

    anon = Anonymizer()
    clean = anonymize(json.loads(json.dumps(data, default=str)), anon)
    stamp = dt.date.today().isoformat()
    out_dir = f"review_{args.client}_{stamp}"
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "mapping_KEEP_PRIVATE.json"), "w") as f:
        json.dump({f"{k[0]}:{k[1]}": v for k, v in anon.map.items()}, f, indent=1)
    zpath = os.path.join(out_dir, f"review_{args.client}_{stamp}_anonymized.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.json", json.dumps(clean, indent=1, default=str))
    print(f"\nDone. Send this file for analysis: {zpath}")
    print("Keep mapping_KEEP_PRIVATE.json on your computer (it links aliases to real names).")
    if ERRORS:
        print(f"{len(ERRORS)} checks could not run (listed inside data.json → errors). This is normal for unused services.")


if __name__ == "__main__":
    main()
