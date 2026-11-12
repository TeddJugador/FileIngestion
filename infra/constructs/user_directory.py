"""The system's Cognito user pool: staff, parents and admins of every school."""

from __future__ import annotations

from aws_cdk import Acknowledgment, Duration, RemovalPolicy, Validations
from aws_cdk import aws_cognito as cognito
from constructs import Construct

GROUPS = ("admin", "staff", "parent")
SCHOOL_ID_ATTRIBUTE = "school_id"  # read as custom:school_id
LOCAL_DEV_CALLBACK = "http://localhost:3000/callback"


class UserDirectory(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str) -> None:
        super().__init__(scope, construct_id)

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="school-users",
            self_sign_up_enabled=False,  # admins create users
            sign_in_aliases=cognito.SignInAliases(email=True),
            sign_in_case_sensitive=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)
            ),
            custom_attributes={
                # Tenant key. Immutable: moving a user between schools means a new user.
                SCHOOL_ID_ATTRIBUTE: cognito.StringAttribute(min_len=7, max_len=7, mutable=False)
            },
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        Validations.of(self.user_pool.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(
                id="AwsSolutions-COG8",
                reason="Owner decision (2026-11-11): Essentials plan, free up to 10,000 MAU; one "
                "school has a few hundred users. Plus (threat protection) costs US$0.02 per "
                "MAU from the first user. Revisit with the planned usage controls.",
            ),
            Acknowledgment(
                id="AwsSolutions-COG2",
                reason="MFA is optional by design: parents sign in occasionally and many have "
                "no authenticator app. TOTP is available to every user; admins can be required "
                "to enrol by policy.",
            ),
        )

        self.groups = {
            name: cognito.CfnUserPoolGroup(
                self,
                f"{name.capitalize()}Group",
                user_pool_id=self.user_pool.user_pool_id,
                group_name=name,
                description=f"School {name} users",
            )
            for name in GROUPS
        }

        # Future web front end: authorization code flow with PKCE, no client secret.
        self.web_client = self.user_pool.add_client(
            "WebClient",
            user_pool_client_name="school-web",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[LOCAL_DEV_CALLBACK],
                logout_urls=["http://localhost:3000/"],
            ),
            prevent_user_existence_errors=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )

        # Scripts and tests sign in with a username and password. Dev stage only.
        self.test_client: cognito.UserPoolClient | None = None
        if stage == "dev":
            self.test_client = self.user_pool.add_client(
                "TestClient",
                user_pool_client_name="school-test-dev-only",
                generate_secret=False,
                auth_flows=cognito.AuthFlow(user_password=True),
                prevent_user_existence_errors=True,
                access_token_validity=Duration.hours(1),
                id_token_validity=Duration.hours(1),
                refresh_token_validity=Duration.days(1),
            )
