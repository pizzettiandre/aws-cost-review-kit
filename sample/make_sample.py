#!/usr/bin/env python3
"""Builds a FICTIONAL dataset (same format as collect.py output) to demo the report."""
import datetime as dt
import json
import zipfile

today = dt.date.today()


def m_start(d):
    return d.replace(day=1)


months = []
d = m_start(today)
for _ in range(4):
    d = m_start(d - dt.timedelta(days=1))
    months.insert(0, d)
months.append(m_start(today))

spend = [  # EC2, RDS, S3, ELB, EBS(other EC2), CloudWatch, Data transfer
    [1180, 520, 140, 60, 190, 45, 70], [1220, 520, 150, 62, 200, 48, 74], [1260, 525, 158, 64, 205, 50, 80],
    [1240, 525, 165, 64, 210, 52, 78], [610, 260, 85, 32, 105, 26, 40]]
svc = ["Amazon Elastic Compute Cloud - Compute", "Amazon Relational Database Service", "Amazon Simple Storage Service",
       "Amazon Elastic Load Balancing", "EC2 - Other", "AmazonCloudWatch", "AWS Data Transfer"]
monthly = []
for i, start in enumerate(months):
    end = m_start(start + dt.timedelta(days=32)) if i < 4 else today
    monthly.append({"TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()}, "Estimated": i == 4,
                    "Groups": [{"Keys": [s], "Metrics": {"UnblendedCost": {"Amount": str(v), "Unit": "USD"}}}
                               for s, v in zip(svc, spend[i])]})


def inst(iid, name, itype, state="running"):
    return {"InstanceId": iid, "InstanceType": itype, "State": {"Name": state},
            "Tags": [{"Key": "Name", "Value": name}]}


old = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=200)).isoformat()
recent = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=5)).isoformat()
region = {
    "region": "us-east-1",
    "instances": [inst("i-0a1", "web-1", "m5.xlarge"), inst("i-0a2", "web-2", "m5.xlarge"),
                  inst("i-0a3", "api-1", "c5.xlarge"), inst("i-0a4", "old-staging", "t2.large"),
                  inst("i-0a5", "worker-1", "m5.large"), inst("i-0a6", "jenkins", "t3.large", "stopped")],
    "cpu": {"i-0a1": {"avg": 6.1, "max": 31.0}, "i-0a2": {"avg": 5.4, "max": 28.5},
            "i-0a3": {"avg": 41.0, "max": 88.0}, "i-0a4": {"avg": 0.7, "max": 2.9},
            "i-0a5": {"avg": 22.0, "max": 64.0}},
    "volumes": [{"VolumeId": f"vol-0b{n}", "Size": s, "VolumeType": t, "State": "available"}
                for n, (s, t) in enumerate([(500, "gp2"), (200, "gp2"), (100, "gp3"), (300, "gp2")])]
               + [{"VolumeId": "vol-0c1", "Size": 100, "VolumeType": "gp3", "State": "in-use"}],
    "snapshots": [{"SnapshotId": f"snap-{n}", "VolumeSize": 100, "StartTime": old} for n in range(38)]
                 + [{"SnapshotId": "snap-new", "VolumeSize": 100, "StartTime": recent}],
    "addresses": [{"AllocationId": "eipalloc-01"}, {"AllocationId": "eipalloc-02"},
                  {"AllocationId": "eipalloc-03", "AssociationId": "eipassoc-1"}],
    "nat_gateways": [],
    "rds": [{"DBInstanceIdentifier": "shop-db", "MultiAZ": False, "BackupRetentionPeriod": 0,
             "PubliclyAccessible": False, "StorageEncrypted": True},
            {"DBInstanceIdentifier": "reports-db", "MultiAZ": False, "BackupRetentionPeriod": 7,
             "PubliclyAccessible": True, "StorageEncrypted": False}],
    "load_balancers": [{"name": "shop-alb", "type": "application", "target_count": 2},
                       {"name": "old-promo-alb", "type": "application", "target_count": 0}],
    "guardduty_enabled": False,
    "compute_optimizer": [],
}
data = {
    "meta": {"tool_version": "1.0", "collected_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z", "cpu_days": 14,
             "regions": ["us-east-1"], "note": "FICTIONAL SAMPLE DATA"},
    "account": {"account_id": "ACCOUNT-01", "summary": {"AccountMFAEnabled": 0},
                "credential_report": [
                    {"user": "<root_account>", "mfa_active": "false", "password_enabled": "not_supported",
                     "access_key_1_active": "false", "access_key_2_active": "false"},
                    {"user": "USER-01", "password_enabled": "true", "mfa_active": "false",
                     "access_key_1_active": "true", "access_key_1_last_rotated": "2024-03-02T10:00:00+00:00",
                     "access_key_2_active": "false"},
                    {"user": "USER-02", "password_enabled": "true", "mfa_active": "true",
                     "access_key_1_active": "false", "access_key_2_active": "false"},
                    {"user": "USER-03", "password_enabled": "true", "mfa_active": "false",
                     "access_key_1_active": "false", "access_key_2_active": "false"}]},
    "cost": {"monthly_by_service": monthly, "savings_plans": []},
    "s3": [{"name": "acme-shop-assets", "region": "us-east-1", "public_access_block": {"BlockPublicAcls": True},
            "lifecycle_rules": 1, "standard_gb": 120},
           {"name": "acme-shop-logs", "region": "us-east-1", "public_access_block": {}, "lifecycle_rules": 0,
            "standard_gb": 2400},
           {"name": "acme-backups", "region": "us-east-1", "public_access_block": {}, "lifecycle_rules": 0,
            "standard_gb": 1800}],
    "cloudtrail": [],
    "regions": {"us-east-1": region},
    "errors": [],
}
with zipfile.ZipFile("review_acme_sample_anonymized.zip", "w") as z:
    z.writestr("data.json", json.dumps(data, indent=1))
print("ok")
