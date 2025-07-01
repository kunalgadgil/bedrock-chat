import * as cdk from "aws-cdk-lib";
import { CfnOutput, Stack, StackProps } from "aws-cdk-lib";
import { Construct } from "constructs";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import { DockerImageCode, DockerImageFunction } from "aws-cdk-lib/aws-lambda";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as path from "path";
import { Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as logs from "aws-cdk-lib/aws-logs";

import { excludeDockerImage } from "./constants/docker";

interface ApiPublishmentStackProps extends StackProps {
  readonly bedrockRegion: string;
  readonly enableBedrockCrossRegionInference: boolean;
  readonly conversationTableName: string;
  readonly botTableName: string;
  readonly tableAccessRoleArn: string;
  readonly webAclArn: string;
  readonly usagePlan: apigateway.UsagePlanProps;
  readonly deploymentStage?: string;
  readonly largeMessageBucketName: string;
  readonly corsOptions?: apigateway.CorsOptions;
  readonly slackBotToken?: string;
  readonly slackSigningSecret?: string;
}

export class ApiPublishmentStack extends Stack {
  public readonly chatQueue: sqs.Queue;
  constructor(scope: Construct, id: string, props: ApiPublishmentStackProps) {
    super(scope, id, props);

    console.log(`usagePlan: ${JSON.stringify(props.usagePlan)}`); // DEBUG

    const deploymentStage = props.deploymentStage ?? "dev";

    const chatQueueDLQ = new sqs.Queue(this, "ChatQueueDlq", {
      retentionPeriod: cdk.Duration.days(14),
    });
    const chatQueue = new sqs.Queue(this, "ChatQueue", {
      visibilityTimeout: cdk.Duration.minutes(30),
      deadLetterQueue: {
        maxReceiveCount: 2, // one retry
        queue: chatQueueDLQ,
      },
    });

    const handlerRole = new iam.Role(this, "HandlerRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    handlerRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName(
        "service-role/AWSLambdaBasicExecutionRole"
      )
    );
    handlerRole.addToPolicy(
      // Assume the table access role for row-level access control.
      new iam.PolicyStatement({
        actions: ["sts:AssumeRole"],
        resources: [props.tableAccessRoleArn],
      })
    );
    handlerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:*"],
        resources: ["*"],
      })
    );
    const largeMessageBucket = s3.Bucket.fromBucketName(
      this,
      "LargeMessageBucket",
      props.largeMessageBucketName
    );
    largeMessageBucket.grantReadWrite(handlerRole);

    // Handler for FastAPI
    const apiHandler = new DockerImageFunction(this, "ApiHandler", {
      code: DockerImageCode.fromImageAsset(
        path.join(__dirname, "../../backend"),
        {
          platform: Platform.LINUX_AMD64,
          file: "Dockerfile",
          exclude: [...excludeDockerImage],
        }
      ),
      memorySize: 1024,
      timeout: cdk.Duration.minutes(15),
      environment: {
        PUBLISHED_API_ID: id.replace("ApiPublishmentStack", ""),
        QUEUE_URL: chatQueue.queueUrl,
        CONVERSATION_TABLE_NAME: props.conversationTableName,
        BOT_TABLE_NAME: props.botTableName,
        CORS_ALLOW_ORIGINS: (props.corsOptions?.allowOrigins ?? ["*"]).join(
          ","
        ),
        ACCOUNT: Stack.of(this).account,
        REGION: Stack.of(this).region,
        BEDROCK_REGION: props.bedrockRegion,
        LARGE_MESSAGE_BUCKET: props.largeMessageBucketName,
        TABLE_ACCESS_ROLE_ARN: props.tableAccessRoleArn,
        // Slack integration environment variables
        SLACK_BOT_TOKEN: props.slackBotToken || "",
        SLACK_SIGNING_SECRET: props.slackSigningSecret || "",
      },
      role: handlerRole,
      logRetention: logs.RetentionDays.THREE_MONTHS,
    });

    // Handler for SQS consumer
    const sqsConsumeHandler = new DockerImageFunction(
      this,
      "SqsConsumeHandler",
      {
        code: DockerImageCode.fromImageAsset(
          path.join(__dirname, "../../backend"),
          {
            platform: Platform.LINUX_AMD64,
            file: "lambda.Dockerfile",
            cmd: ["app.sqs_consumer.handler"],
            exclude: [...excludeDockerImage],
          }
        ),
        memorySize: 1024,
        timeout: cdk.Duration.minutes(15),
        environment: {
          PUBLISHED_API_ID: id.replace("ApiPublishmentStack", ""),
          QUEUE_URL: chatQueue.queueUrl,
          CONVERSATION_TABLE_NAME: props.conversationTableName,
          BOT_TABLE_NAME: props.botTableName,
          CORS_ALLOW_ORIGINS: (props.corsOptions?.allowOrigins ?? ["*"]).join(
            ","
          ),
          ACCOUNT: Stack.of(this).account,
          REGION: Stack.of(this).region,
          ENABLE_BEDROCK_CROSS_REGION_INFERENCE: props.enableBedrockCrossRegionInference.toString(),
          BEDROCK_REGION: props.bedrockRegion,
          TABLE_ACCESS_ROLE_ARN: props.tableAccessRoleArn,
        },
        role: handlerRole,
        logRetention: logs.RetentionDays.THREE_MONTHS,
      }
    );
    sqsConsumeHandler.addEventSource(
      new lambdaEventSources.SqsEventSource(chatQueue)
    );
    chatQueue.grantSendMessages(apiHandler);
    chatQueue.grantConsumeMessages(sqsConsumeHandler);

    // Main API Gateway for regular endpoints (with WAF protection and API key required)
    const api = new apigateway.LambdaRestApi(this, "Api", {
      restApiName: id,
      handler: apiHandler,
      proxy: true,
      deployOptions: {
        stageName: deploymentStage,
      },
      defaultMethodOptions: { apiKeyRequired: true },
      defaultCorsPreflightOptions: props.corsOptions,
    });

    // Separate API Gateway for Slack webhooks (without WAF protection and no API key required)
    const slackApi = new apigateway.LambdaRestApi(this, "SlackApi", {
      restApiName: `${id}-Slack`,
      handler: apiHandler,
      proxy: false,
      deployOptions: {
        stageName: deploymentStage,
      },
      defaultMethodOptions: { apiKeyRequired: false },
      defaultCorsPreflightOptions: props.corsOptions,
    });

    // Add Slack webhook resources to the separate API
    const slackResource = slackApi.root.addResource("slack");

    // Slack events endpoint
    const slackEventsResource = slackResource.addResource("events");
    slackEventsResource.addMethod("POST", new apigateway.LambdaIntegration(apiHandler), {
      apiKeyRequired: false,
    });

    // Slack health endpoint
    const slackHealthResource = slackResource.addResource("health");
    slackHealthResource.addMethod("GET", new apigateway.LambdaIntegration(apiHandler), {
      apiKeyRequired: false,
    });

    // Slack status endpoint
    const slackStatusResource = slackResource.addResource("status");
    slackStatusResource.addMethod("GET", new apigateway.LambdaIntegration(apiHandler), {
      apiKeyRequired: false,
    });

    // Slack poll-response endpoint
    const slackPollResource = slackResource.addResource("poll-response");
    slackPollResource.addMethod("POST", new apigateway.LambdaIntegration(apiHandler), {
      apiKeyRequired: false,
    });

    const apiKey = api.addApiKey("ApiKey", {
      description: "Default api key (Auto generated by CDK)",
    });
    const usagePlan = api.addUsagePlan("UsagePlan", {
      ...props.usagePlan,
    });
    usagePlan.addApiKey(apiKey);
    usagePlan.addApiStage({ stage: api.deploymentStage });

    // Apply WAF only to the main API Gateway (not the Slack API)
    const association = new wafv2.CfnWebACLAssociation(
      this,
      "WebAclAssociation",
      {
        resourceArn: `arn:aws:apigateway:${Stack.of(this).region}::/restapis/${api.restApiId}/stages/${api.deploymentStage.stageName}`,
        webAclArn: props.webAclArn,
      }
    );
    association.addDependency(api.node.defaultChild as cdk.CfnResource);

    // Note: Slack API does not have WAF protection, allowing Slack webhooks to work without IP restrictions

    this.chatQueue = chatQueue;

    new CfnOutput(this, "ApiId", {
      value: api.restApiId,
    });
    new CfnOutput(this, "ApiName", {
      value: api.restApiName,
    });
    new CfnOutput(this, "SlackApiId", {
      value: slackApi.restApiId,
    });
    new CfnOutput(this, "SlackApiName", {
      value: slackApi.restApiName,
    });
    new CfnOutput(this, "ApiUsagePlanId", {
      value: usagePlan.usagePlanId,
    });
    new CfnOutput(this, "AllowedOrigins", {
      value: props.corsOptions?.allowOrigins?.join(",") ?? "*",
    });
    new CfnOutput(this, "DeploymentStage", {
      value: deploymentStage,
    });
    new CfnOutput(this, "SlackWebhookUrl", {
      value: `https://${slackApi.restApiId}.execute-api.${Stack.of(this).region}.amazonaws.com/${deploymentStage}/slack/events`,
      description: "URL to use for Slack Event Subscriptions",
    });
  }
}
