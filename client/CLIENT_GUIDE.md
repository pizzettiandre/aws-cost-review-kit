# How to give read-only access for your AWS review (about 5 minutes)

You will create a temporary **read-only** role. With it I can see how your account is configured, your usage metrics and your bill. I **cannot** create, change or delete anything, and I **cannot** read your files, databases or secrets.

**Never send me passwords, access keys or your root login.** You don't need to.

## Before you start
From me you will receive:
- my **AWS account ID** (12 digits)
- a **secret phrase** (External ID), used only for this review

## Steps
1. Sign in to the AWS Console with a user that can create IAM roles (an administrator).
2. Open **CloudFormation** → **Create stack** → **With new resources (standard)**.
3. Choose **Upload a template file** and select `readonly-review-role.yaml` (attached to our order). Click **Next**.
4. Stack name: `readonly-review`.
   - **ReviewerAccountId**: my account ID
   - **ExternalId**: the secret phrase I sent you
5. Click **Next** twice. On the last page tick **"I acknowledge that AWS CloudFormation might create IAM resources with custom names"** and click **Submit**.
6. Wait until the status is **CREATE_COMPLETE**. Open the **Outputs** tab and send me the **RoleArn** value in our Fiverr chat.

That's it. You can open the template in any text editor to check exactly what it allows.

## When the review is finished
Go to **CloudFormation**, select the `readonly-review` stack and click **Delete**. The access is removed immediately.

## Questions people ask
- **Does this cost anything?** The role is free. Reading your bill through the Cost Explorer API costs $0.01 per request, about $0.05 in total for the review.
- **Can you see my customers' data?** No. Reading files (S3 objects), database items, secrets and decryption keys is explicitly blocked.
- **How long does the access last?** Until you delete the stack. Each of my sessions expires after 1 hour.
