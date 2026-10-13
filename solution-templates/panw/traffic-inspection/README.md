# Centralized VPC Traffic Inspection with Palo Alto Networks VM-Series and AWS Gateway Load Balancer

## Overview
This solution template deploys a four-VPC lab environment on AWS in which a Palo Alto Networks VM-Series next-generation firewall, placed behind AWS Gateway Load Balancer (GWLB), inspects outbound, inbound and east-west traffic. A sample online store (web frontend and backend data server) and a simulated attacker give you live traffic to inspect, including a simulated Log4j exploit against the public-facing web application.

> **Note:** This environment is intentionally vulnerable. It is designed for controlled security training and demonstrations, not for production use. Deploy it in a dedicated, non-production AWS account.

### What This Solution Deploys

This template automates the deployment of:

1. **Security VPC** - Centralized inspection VPC with a Gateway Load Balancer, a GWLB endpoint service and an AWS Transit Gateway
2. **VM-Series Firewall** - PAN-OS firewall from the AWS Marketplace (pay-as-you-go), bootstrapped automatically with a ready-made security policy
3. **Online Store Frontend VPC** - Public-facing sample web application, reachable through its own GWLB endpoint for inbound inspection
4. **Online Store Data VPC** - Backend data server that the frontend talks to over the Transit Gateway
5. **Attacker VPC** - Simulated threat actor that runs a Log4j exploit chain against the frontend
6. **Bootstrap Automation** - Lambda-backed custom resources that resolve the latest Marketplace AMI, build the firewall's bootstrap bucket and wire up the GWLB endpoints

### Architecture

The solution implements a centralized inspection pattern:
- An AWS Transit Gateway connects the Frontend and Data VPCs to the Security VPC (spoke and security route tables)
- AWS Gateway Load Balancer sends traffic to the VM-Series firewall over GENEVE (UDP 6081)
- A GWLB endpoint in the Frontend VPC, together with an IGW edge route table, enables inbound inspection
- The VM-Series firewall applies App-ID, Advanced Threat Prevention and Advanced URL Filtering policies
- The spoke VPCs start with direct routing, so you redirect traffic through the firewall yourself as part of the lab

| VPC | Default CIDR | Purpose |
|---|---|---|
| Security VPC | 10.0.0.0/24 | GWLB, GWLB endpoint, Transit Gateway attachment, VM-Series firewall |
| Online Store Frontend VPC | 10.1.0.0/16 | Public-facing sample web application (intentionally vulnerable) |
| Online Store Data VPC | 10.2.0.0/16 | Backend data server (SSH on non-standard port 2222) |
| Attacker VPC | 192.168.10.0/23 | Simulated attacker for the Log4j use case |

### Prerequisites

- AWS Account with permissions to create CloudFormation stacks, IAM roles, VPC and EC2 resources, Elastic Load Balancing, Transit Gateway, Lambda and S3
- VM-Series subscription ([VM-Series Next-Gen Virtual Firewall w/ Advanced Security Subs (PAYG) on AWS Marketplace](https://aws.amazon.com/marketplace/pp/prodview-3xtziatyes54i?trk=51f363bd-ff3d-4c52-b270-5299b08aaa2b)). Choose **View purchase options** (or **Try for free** for the 15-day trial), accept the terms, and wait for the subscription to become **Active**.
- An S3 bucket in the deployment region to hold the nested CloudFormation templates (it can stay private)
- A public HTTPS URL for `bootstrap.xml`, such as the raw file URL in this repository. The bootstrap Lambda fetches it anonymously, so it cannot be a private S3 object.
- AWS CLI configured (optional; the AWS Console works as well)
- Supported regions: US East (N. Virginia) `us-east-1` and US West (N. California) `us-west-1`

### Quick Start

1. **Upload the nested templates to S3**
   ```bash
   export AWS_REGION=us-east-1
   export BUCKET=your-template-bucket
   export PREFIX=panw-gwlb

   aws s3 mb s3://$BUCKET --region $AWS_REGION
   aws s3 cp . s3://$BUCKET/$PREFIX/ --recursive \
     --exclude "*" --include "aws-panw-gwlb-cfn-*.yaml"
   ```

2. **Deploy the solution**
   ```bash
   export ASSETS_BASE_URL=https://$BUCKET.s3.$AWS_REGION.amazonaws.com/$PREFIX   # no trailing slash
   export BOOTSTRAP_XML_URL=<REPO_URL>/raw/main/bootstrap.xml                    # must be publicly readable

   aws cloudformation create-stack \
     --stack-name VM-Series \
     --region $AWS_REGION \
     --template-url $ASSETS_BASE_URL/aws-panw-gwlb-cfn-root.yaml \
     --parameters \
       ParameterKey=AssetsBaseUrl,ParameterValue=$ASSETS_BASE_URL \
       ParameterKey=BootstrapXmlUrl,ParameterValue=$BOOTSTRAP_XML_URL \
     --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND

   aws cloudformation wait stack-create-complete --stack-name VM-Series --region $AWS_REGION
   ```
   The stack fails early with a subscription link if the account has not accepted the Marketplace terms. After the stack completes, the firewall needs several more minutes to boot and apply its bootstrap configuration.

3. **Allow your IP to reach the firewall and the web application**
   ```bash
   MY_IP=$(curl -s https://checkip.amazonaws.com)/32

   # VM-Series management UI (HTTPS)
   MGMT_SG=$(aws ec2 describe-security-groups --region $AWS_REGION \
     --filters Name=group-name,Values=Security-VM-Series-Management \
     --query 'SecurityGroups[0].GroupId' --output text)
   aws ec2 authorize-security-group-ingress --region $AWS_REGION \
     --group-id $MGMT_SG --protocol tcp --port 443 --cidr $MY_IP

   # Online store web frontend (HTTP)
   WEB_SG=$(aws ec2 describe-security-groups --region $AWS_REGION \
     --filters "Name=group-name,Values=Online Store Frontend Public" \
     --query 'SecurityGroups[0].GroupId' --output text)
   aws ec2 authorize-security-group-ingress --region $AWS_REGION \
     --group-id $WEB_SG --protocol tcp --port 80 --cidr $MY_IP
   ```

4. **Log in to the firewall**

   Find `VMSeriesManagementEIP` in the outputs of the nested VM-Series stack (CloudFormation console → the `VmSeriesStack` nested stack → **Outputs**), then browse to `https://<VMSeriesManagementEIP>` and sign in as `admin`. The firewall uses a self-signed certificate, so your browser will show a warning on first access. Change the admin password after your first login (see [Security Considerations](#security-considerations)).

#### Template Parameters

| Parameter | Default | Description |
|---|---|---|
| `AssetsBaseUrl` | Workshop assets bucket | Base URL (no trailing slash) of the S3 location hosting the nested templates. **Set this to your own bucket.** |
| `BootstrapXmlUrl` | Workshop assets bucket | Public HTTPS URL of `bootstrap.xml`. **Set this to your own copy.** |
| `PanOSVersion` | `11.2` | PAN-OS version prefix used to select the newest matching Marketplace AMI (for example `11.2` or `11.2.4`) |
| `VMSeriesInstanceType` | `m5.xlarge` | Firewall instance type. Must support at least three network interfaces. |
| `SecurityNamePrefix` | `Security` | Name/tag prefix for Security VPC resources |
| `FrontendNamePrefix` | `Online Store Frontend` | Name/tag prefix for Frontend VPC resources |
| `DataNamePrefix` | `Online Store Data` | Name/tag prefix for Data VPC resources |
| `AttackerNamePrefix` | `Attacker` | Name/tag prefix for Attacker VPC resources |
| `*VPCCIDR`, `*SubnetCIDR*` | See [Architecture](#architecture) | VPC and subnet address ranges |

> The lab steps in the solution guide refer to resources by their default names (for example **Online Store Data Private**). Keep the default name prefixes if you are following the guide. `bootstrap.xml` also assumes the default Frontend and Data CIDRs.

### What Gets Created

The root template (`aws-panw-gwlb-cfn-root.yaml`) deploys three nested stacks:

- **Security stack** (`aws-panw-gwlb-cfn-security.yaml`)
  - Security VPC, internet gateway, subnets and route tables
  - Transit Gateway with spoke and security route tables
  - Gateway Load Balancer, VPC endpoint service and GWLB endpoint
  - Security groups for the firewall's management, data and public interfaces

- **VM-Series stack** (`aws-panw-gwlb-cfn-vmseries.yaml`)
  - VM-Series EC2 instance with management, data and public network interfaces
  - Two Elastic IPs (management and public)
  - GWLB target group (GENEVE) and listener
  - Encrypted S3 bootstrap bucket with `init-cfg.txt` and `bootstrap.xml`
  - Lambda custom resources for Marketplace AMI lookup and bootstrap bucket setup

- **Combined stack** (`aws-panw-gwlb-cfn-combined.yaml`)
  - Online Store Frontend VPC with a web server, GWLB endpoint and IGW edge route table
  - Online Store Data VPC with a data server
  - Attacker VPC with an attacker instance
  - Transit Gateway attachments, SSM VPC endpoints and IAM instance profiles (instances are reachable through AWS Systems Manager Session Manager without SSH keys)

- **Firewall configuration** (`bootstrap.xml`)
  - Zones, interfaces and routes for GWLB overlay routing
  - Address objects for the Online Store Frontend and Data VPCs
  - Security rules, including an App-ID based SSH deny rule and threat, vulnerability and URL filtering profiles

### Lab Use Cases

The solution guide walks through four use cases:

| # | Use case | What you do |
|---|---|---|
| 1 | Inspect outbound traffic | Route the Data VPC's internet-bound traffic through the Transit Gateway to the firewall and review the traffic logs |
| 2 | Inspect east-west traffic | Remove the direct spoke-to-spoke propagations so Frontend ↔ Data traffic passes through the firewall |
| 3 | Inspect inbound traffic | Route internet traffic for the Frontend VPC through its GWLB endpoint, observe the simulated Log4j attack, and block it with a strict Vulnerability Protection profile |
| 4 | App-ID policies | See App-ID identify and block SSH running on the non-standard port 2222, regardless of port |

### Cost Estimate

Costs below are approximate list prices for `us-east-1` with the default parameters. Check the [AWS Pricing Calculator](https://calculator.aws/) and the Marketplace listing for current figures.

- VM-Series software (PAYG): **billed hourly by AWS Marketplace** (roughly $1.71/hour for 4-vCPU instance types; see the listing for your instance type). Free for the first 15 days under the free trial.
- EC2: VM-Series `m5.xlarge` (~$0.19/hour) plus three `t2.micro` lab instances (~$0.035/hour)
- Transit Gateway: 3 VPC attachments (~$0.15/hour) plus data processing
- Gateway Load Balancer and 2 GWLB endpoints: ~$0.03/hour plus data processing
- Interface VPC endpoints for SSM: 9 endpoints (~$0.09/hour)
- Public IPv4 addresses: ~$0.005/hour each
- EBS storage, S3, Lambda, CloudWatch Logs: minimal

**Estimated cost: roughly $0.55/hour during the free trial, or about $2.25/hour after it** (about $13 or $55 per day). Delete the stack as soon as you finish the lab.

### Security Considerations

- **Intentionally vulnerable workloads.** The web frontend is vulnerable to Log4j, and the attacker instance runs an exploit chain against it. Do not open the frontend security group to `0.0.0.0/0`. Restrict access to your own IP as shown in the Quick Start.
- **Lab credentials in `bootstrap.xml`.** The bundled configuration contains a fixed admin password hash, an SSH public key and expired self-signed certificates used for the SSL decryption demo. Change the admin password after your first login, and never reuse these credentials outside the lab.
- **Third-party container images.** The lab instances pull sample application images from Docker Hub at deploy time (see `UserData` in `aws-panw-gwlb-cfn-combined.yaml`).
- **IMDSv1 on the firewall.** IMDSv2 is not enforced on the VM-Series instance because PAN-OS PAYG licensing uses IMDSv1 for Marketplace registration. The lab instances enforce IMDSv2.

### Known Limitations

- **One deployment per account per region.** The stacks use fixed CloudFormation export names and a fixed Gateway Load Balancer name.
- **Single Availability Zone.** All resources are deployed in the first AZ of the region.
- **PAN-OS versions.** `bootstrap.xml` is a PAN-OS 10.2 configuration that migrates forward to 11.x. It has not been validated against 12.x, so pin `PanOSVersion` to an 11.x release.

### Cleanup

To remove all resources:
```bash
aws cloudformation delete-stack --stack-name VM-Series --region $AWS_REGION
aws cloudformation wait stack-delete-complete --stack-name VM-Series --region $AWS_REGION

# Remove the uploaded templates
aws s3 rm s3://$BUCKET/$PREFIX/ --recursive
```
The firewall's bootstrap bucket is emptied and deleted with the stack. If you no longer need the VM-Series subscription, cancel it under **Manage subscriptions** in the AWS Marketplace console.

### Learn More

- [VM-Series Deployment on AWS (Palo Alto Networks)](https://docs.paloaltonetworks.com/vm-series)
- [AWS Gateway Load Balancer Documentation](https://docs.aws.amazon.com/elasticloadbalancing/latest/gateway/introduction.html)
- [AWS Transit Gateway Documentation](https://docs.aws.amazon.com/vpc/latest/tgw/what-is-transit-gateway.html)
- [VM-Series on AWS Marketplace](https://aws.amazon.com/marketplace/pp/prodview-3xtziatyes54i)

### Support

For issues or questions:
- Palo Alto Networks: [Support Portal](https://support.paloaltonetworks.com/)
- AWS: [AWS Support](https://aws.amazon.com/support/)8
- GitHub Issues: [Report an issue](<REPO_URL>/issues)
