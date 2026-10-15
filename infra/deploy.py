"""Update the CloudFormation-owned task definition; called only by protected deploy CI."""
import json
import os
import subprocess
import urllib.request

parameters = json.loads(os.environ["CFN_PARAMETERS"])
parameters["ImageUri"] = os.environ["IMAGE_URI"]
if parameters.get("LiveEnabled", "false") not in {"false", "true"}:
    raise ValueError("LiveEnabled must be true or false")
stack = os.environ["STACK_NAME"]
subprocess.run([
    "aws", "cloudformation", "deploy", "--stack-name", stack,
    "--template-file", "infra/service.yaml", "--capabilities", "CAPABILITY_IAM",
    "--role-arn", os.environ["CFN_ROLE_ARN"], "--no-fail-on-empty-changeset",
    "--parameter-overrides", *[f"{key}={value}" for key, value in parameters.items()],
], check=True)
outputs = json.loads(subprocess.check_output([
    "aws", "cloudformation", "describe-stacks", "--stack-name", stack,
    "--query", "Stacks[0].Outputs", "--output", "json",
], text=True))
url = next(item["OutputValue"] for item in outputs if item["OutputKey"] == "Url")
with urllib.request.urlopen(url + "/_stcore/health", timeout=15) as response:
    if response.status != 200:
        raise RuntimeError("HTTPS health check failed")
print("Service deployed and HTTPS health check passed")
