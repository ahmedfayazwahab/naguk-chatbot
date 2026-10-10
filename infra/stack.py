"""CDK stack: S3 index bucket, rate-limit table, agent Lambda behind a
throttled HTTP API, and a budget alarm. Deploy with:

    cdk deploy

Secrets (create once, out of band, before first deploy):
    aws secretsmanager create-secret --name naguk/openai-api-key --secret-string "sk-proj-..."
    aws secretsmanager create-secret --name naguk/voyage-api-key --secret-string "pa-..."
    aws secretsmanager create-secret --name naguk/pinecone-api-key --secret-string "pcsk_..."
"""
from aws_cdk import (
    Stack, Duration, CfnOutput,
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_budgets as budgets,
    aws_iam as iam,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from aws_cdk import aws_lambda as _lambda
from constructs import Construct

ALLOWED_ORIGINS = ["https://ahmedfayazwahab.ca", "http://localhost:8080"]
BUDGET_LIMIT_USD = 15
ALERT_EMAIL = "fayazahmedpg2024@gmail.com"


class NagukChatbotStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        index_bucket = s3.Bucket(self, "IndexBucket", removal_policy=None)

        rate_table = dynamodb.Table(
            self, "RateLimitTable",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
        )

        agent_fn = PythonFunction(
            self, "AgentFunction",
            # A minimal dir of symlinks to the real files (not the project root) — bundling
            # copies the whole entry dir, and pointing it at the project root previously swept
            # up infra/cdk.out's own prior build output into itself across repeated builds.
            entry="lambda_src",
            index="lambda_handler.py",
            handler="handler",
            runtime=_lambda.Runtime.PYTHON_3_12,
            timeout=Duration.seconds(30),
            memory_size=512,
            environment={
                "INDEX_BUCKET": index_bucket.bucket_name,
                "RATE_LIMIT_TABLE": rate_table.table_name,
                "ALLOWED_ORIGINS": ",".join(ALLOWED_ORIGINS),
                "PINECONE_INDEX_NAME": "naguk-restaurants",
            },
        )
        index_bucket.grant_read(agent_fn)
        rate_table.grant_read_write_data(agent_fn)
        agent_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[
                f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:naguk/openai-api-key*",
                f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:naguk/voyage-api-key*",
                f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:naguk/pinecone-api-key*",
            ],
        ))

        http_api = apigwv2.HttpApi(
            self, "AgentApi",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=ALLOWED_ORIGINS,
                allow_methods=[apigwv2.CorsHttpMethod.POST],
                allow_headers=["content-type"],
            ),
            default_integration=integrations.HttpLambdaIntegration("AgentIntegration", agent_fn),
            create_default_stage=False,
        )
        apigwv2.HttpStage(
            self, "DefaultStage",
            http_api=http_api, stage_name="$default", auto_deploy=True,
            throttle=apigwv2.ThrottleSettings(rate_limit=5, burst_limit=10),
        )

        budgets.CfnBudget(
            self, "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST", time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=BUDGET_LIMIT_USD, unit="USD"),
            ),
            notifications_with_subscribers=[budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    notification_type="ACTUAL", comparison_operator="GREATER_THAN",
                    threshold=80, threshold_type="PERCENTAGE",
                ),
                subscribers=[budgets.CfnBudget.SubscriberProperty(
                    subscription_type="EMAIL", address=ALERT_EMAIL,
                )],
            )],
        )

        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "IndexBucketName", value=index_bucket.bucket_name)
