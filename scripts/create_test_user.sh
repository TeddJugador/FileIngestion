#!/usr/bin/env bash
# Create a confirmed test user in the dev user pool and print an ID token for it.
#
#   scripts/create_test_user.sh <email> <admin|staff|parent> [SCH-001]
#
# Prompts for a password (hidden; at least 12 characters with upper and lower case,
# a digit and a symbol). Re-running for an existing user resets the password and
# prints a fresh token. Dev stage only: it signs in through the password test client.
set -euo pipefail

if [[ $# -lt 2 || ! "$2" =~ ^(admin|staff|parent)$ ]]; then sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2; fi
EMAIL="$1"; GROUP="$2"; SCHOOL="${3:-SCH-001}"
[[ "$SCHOOL" =~ ^SCH-[0-9]{3}$ ]] || { echo "school must look like SCH-001" >&2; exit 2; }

output() {
  aws cloudformation describe-stacks --stack-name SchoolSharedStack \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
POOL="$(output UserPoolId)"
CLIENT="$(output TestClientId)"
[[ -n "$CLIENT" && "$CLIENT" != "None" ]] || { echo "no TestClientId output (not the dev stage?)" >&2; exit 1; }

read -r -s -p "Password for ${EMAIL}: " PASSWORD; echo

if ! aws cognito-idp admin-get-user --user-pool-id "$POOL" --username "$EMAIL" >/dev/null 2>&1; then
  aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$EMAIL" \
    --message-action SUPPRESS \
    --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
      Name=custom:school_id,Value="$SCHOOL" >/dev/null
  echo "Created ${EMAIL} (${SCHOOL})" >&2
fi
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$EMAIL" \
  --password "$PASSWORD" --permanent
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username "$EMAIL" \
  --group-name "$GROUP"

aws cognito-idp initiate-auth --client-id "$CLIENT" --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters "USERNAME=${EMAIL},PASSWORD=${PASSWORD}" \
  --query AuthenticationResult.IdToken --output text
