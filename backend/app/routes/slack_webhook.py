import json
import os
import hmac
import hashlib
import time
import re
import asyncio
from typing import Any, Dict
from datetime import datetime, timedelta

import boto3
from app.routes.schemas.conversation import ChatInput, MessageInput
from app.routes.schemas.published_api import ChatInputWithoutBotId
from app.user import User
from app.usecases.chat import fetch_conversation
from fastapi import APIRouter, HTTPException, Request, Response, BackgroundTasks
from pydantic import BaseModel
from ulid import ULID

router = APIRouter(tags=["slack_webhook"])

# Environment variables
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET', '')

sqs_client = boto3.client("sqs")
QUEUE_URL = os.environ.get("QUEUE_URL", "")

# In-memory storage for Slack context (in production, use DynamoDB)
slack_context_store = {}

class SlackContext(BaseModel):
    message_id: str
    conversation_id: str
    channel: str
    user: str
    timestamp: datetime
    bot_id: str
    response_sent: bool = False


class SlackEvent(BaseModel):
    type: str
    challenge: str | None = None
    event: Dict[str, Any] | None = None


@router.post("/slack/events")
async def handle_slack_events(request: Request, background_tasks: BackgroundTasks):
    """Handle Slack Events API webhooks"""

    # Get request body
    body = await request.body()
    body_str = body.decode('utf-8')

    print(f"Received Slack webhook: {body_str}")

    # Verify Slack signature
    if not verify_slack_signature(body_str, dict(request.headers)):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        slack_data = json.loads(body_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Handle URL verification challenge
    if slack_data.get('type') == 'url_verification':
        challenge = slack_data.get('challenge', '')
        return Response(content=challenge, media_type="text/plain")

    # Handle event callbacks
    if slack_data.get('type') == 'event_callback':
        await handle_slack_event(request, slack_data, background_tasks)
        return {"status": "ok"}

    return {"status": "ignored"}


async def handle_slack_event(request: Request, slack_event: Dict[str, Any], background_tasks: BackgroundTasks):
    """Process Slack events (mentions, direct messages)"""

    event = slack_event.get('event', {})
    event_type = event.get('type')

    print(f"Processing Slack event type: {event_type}")

    # Only handle app mentions and direct messages
    if event_type not in ['app_mention', 'message']:
        print(f"Ignoring event type: {event_type}")
        return

    # Ignore bot messages to prevent loops
    if event.get('bot_id') or event.get('subtype') == 'bot_message':
        print("Ignoring bot message")
        return

    # Extract message details
    channel = event.get('channel')
    user = event.get('user')
    text = event.get('text', '')

    print(f"Channel: {channel}, User: {user}, Text: {text}")

    # Clean message text (remove bot mentions)
    cleaned_text = clean_message_text(text)

    if not cleaned_text.strip():
        print("Empty message after cleaning")
        return

    # Get current user from request (this will be the published API user)
    current_user: User = request.state.current_user

    # Extract bot_id from current_user.id (same logic as published API)
    bot_id = (
        current_user.id.split("#")[1] if "#" in current_user.id else current_user.id
    )

    # Create conversation ID based on Slack channel/user for context
    conversation_id = f"slack-{channel}-{user}"

    # Generate message ID
    response_message_id = str(ULID())

    # Create chat input
    chat_input = ChatInput(
        conversation_id=conversation_id,
        message=MessageInput(
            role="user",
            content=[
                {
                    "contentType": "text",
                    "body": cleaned_text
                }
            ],
            model="amazon-nova-micro",  # You can make this configurable
            parent_message_id=None,
            message_id=response_message_id,
        ),
        bot_id=bot_id,
        continue_generate=False,
        enable_reasoning=False,
    )

    try:
        # Send to SQS for processing (same as published API)
        _ = sqs_client.send_message(
            QueueUrl=QUEUE_URL, MessageBody=chat_input.model_dump_json()
        )
        print(f"Sent message to SQS: {response_message_id}")

        # Send immediate response to Slack
        await send_slack_response(channel, "🤔 Processing your request...")

        # Store the context for later response
        store_slack_context(response_message_id, channel, conversation_id, user, bot_id)

        # Start background polling for this specific message
        background_tasks.add_task(poll_for_response, response_message_id, conversation_id)

    except Exception as e:
        print(f"Error processing Slack message: {str(e)}")
        await send_slack_response(channel, f"Sorry, I encountered an error: {str(e)}")


async def send_slack_response(channel: str, text: str):
    """Send response back to Slack"""
    try:
        import httpx
        
        url = "https://slack.com/api/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json"
        }
        data = {
            "channel": channel,
            "text": text
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=data)
            result = response.json()
            
            if not result.get('ok'):
                print(f"Failed to send Slack message: {result}")
            else:
                print("Successfully sent message to Slack")
                
    except Exception as e:
        print(f"Error sending Slack message: {str(e)}")


def clean_message_text(text: str) -> str:
    """Remove bot mentions and clean up text"""
    # Remove bot mentions like <@U1234567890>
    text = re.sub(r'<@[A-Z0-9]+>', '', text)
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def verify_slack_signature(body: str, headers: Dict[str, str]) -> bool:
    """Verify Slack request signature for security"""
    try:
        # Skip verification if signing secret is not set (for testing)
        if not SLACK_SIGNING_SECRET:
            print("Warning: SLACK_SIGNING_SECRET not set, skipping verification")
            return True
        
        # Get signature and timestamp from headers
        slack_signature = headers.get('x-slack-signature', '')
        slack_timestamp = headers.get('x-slack-request-timestamp', '')
        
        if not slack_signature or not slack_timestamp:
            print("Missing signature or timestamp")
            return False
        
        # Check timestamp (prevent replay attacks)
        try:
            timestamp_int = int(slack_timestamp)
            current_time = int(time.time())
            time_diff = abs(current_time - timestamp_int)
            
            if time_diff > 60 * 5:  # 5 minutes
                print(f"Request too old: {time_diff} seconds")
                return False
        except ValueError:
            print(f"Invalid timestamp format: {slack_timestamp}")
            return False
        
        # Create signature
        sig_basestring = f"v0:{slack_timestamp}:{body}"
        my_signature = 'v0=' + hmac.new(
            SLACK_SIGNING_SECRET.encode(),
            sig_basestring.encode(),
            hashlib.sha256
        ).hexdigest()
        
        is_valid = hmac.compare_digest(my_signature, slack_signature)
        print(f"Signature verification: {is_valid}")
        
        return is_valid
        
    except Exception as e:
        print(f"Error verifying signature: {str(e)}")
        return False


def store_slack_context(message_id: str, channel: str, conversation_id: str, user: str, bot_id: str):
    """Store Slack context for later response"""
    context = SlackContext(
        message_id=message_id,
        conversation_id=conversation_id,
        channel=channel,
        user=user,
        timestamp=datetime.now(),
        bot_id=bot_id,
        response_sent=False
    )
    slack_context_store[message_id] = context
    print(f"Stored context for message {message_id}: {context}")


async def poll_for_response(message_id: str, conversation_id: str):
    """Poll for AI response and send it back to Slack"""
    print(f"Starting polling for message {message_id}")

    max_attempts = 60  # Poll for up to 60 seconds
    poll_interval = 1  # Poll every 1 second

    for attempt in range(max_attempts):
        try:
            print(f"Polling attempt {attempt + 1}/{max_attempts} for message {message_id}")

            # Get context
            context = slack_context_store.get(message_id)
            if not context:
                print(f"Context not found for message {message_id}")
                return

            if context.response_sent:
                print(f"Response already sent for message {message_id}")
                return

            # Create a user object to fetch the conversation
            user = User(id=context.bot_id, name="slack_bot", email="slack@bot.com", groups=[])

            # Try to fetch the conversation to see if response is ready
            try:
                conversation = fetch_conversation(user.id, conversation_id)

                # Look for the response message (child of our input message)
                input_message = conversation.message_map.get(message_id)
                if input_message and input_message.children:
                    # Get the response message
                    response_message_id = input_message.children[0]
                    response_message = conversation.message_map.get(response_message_id)

                    if response_message and response_message.content:
                        # Extract text from response content
                        response_text = ""
                        for content_block in response_message.content:
                            if hasattr(content_block, 'contentType') and content_block.contentType == "text":
                                response_text += content_block.body
                            elif hasattr(content_block, 'content_type') and content_block.content_type == "text":
                                response_text += content_block.body

                        if response_text.strip():
                            # Send response to Slack
                            await send_slack_response(context.channel, response_text)

                            # Mark as sent
                            context.response_sent = True
                            slack_context_store[message_id] = context

                            print(f"Successfully sent response for message {message_id}")
                            return

            except Exception as fetch_error:
                print(f"Error fetching conversation (attempt {attempt + 1}): {fetch_error}")
                # Continue polling

            # Wait before next poll
            await asyncio.sleep(poll_interval)

        except Exception as e:
            print(f"Error in polling attempt {attempt + 1}: {str(e)}")
            await asyncio.sleep(poll_interval)

    # If we get here, polling timed out
    context = slack_context_store.get(message_id)
    if context and not context.response_sent:
        await send_slack_response(
            context.channel,
            "⏰ Sorry, the request timed out. Please try again."
        )
        context.response_sent = True
        slack_context_store[message_id] = context

    print(f"Polling timed out for message {message_id}")


@router.post("/slack/poll-response")
async def poll_and_respond(request: Request):
    """Manually poll for AI responses and send them back to Slack"""
    processed_count = 0
    pending_count = 0

    # Get all pending contexts
    for message_id, context in slack_context_store.items():
        if not context.response_sent:
            pending_count += 1

            # Check if response is ready
            try:
                user = User(id=context.bot_id, name="slack_bot", email="slack@bot.com", groups=[])
                conversation = fetch_conversation(user.id, context.conversation_id)

                input_message = conversation.message_map.get(message_id)
                if input_message and input_message.children:
                    response_message_id = input_message.children[0]
                    response_message = conversation.message_map.get(response_message_id)

                    if response_message and response_message.content:
                        # Extract text from response content
                        response_text = ""
                        for content_block in response_message.content:
                            if hasattr(content_block, 'contentType') and content_block.contentType == "text":
                                response_text += content_block.body
                            elif hasattr(content_block, 'content_type') and content_block.content_type == "text":
                                response_text += content_block.body

                        if response_text.strip():
                            # Send response to Slack
                            await send_slack_response(context.channel, response_text)

                            # Mark as sent
                            context.response_sent = True
                            processed_count += 1

            except Exception as e:
                print(f"Error processing message {message_id}: {str(e)}")

    # Clean up old contexts (older than 1 hour)
    cutoff_time = datetime.now() - timedelta(hours=1)
    old_message_ids = [
        msg_id for msg_id, ctx in slack_context_store.items()
        if ctx.timestamp < cutoff_time
    ]
    for msg_id in old_message_ids:
        del slack_context_store[msg_id]

    return {
        "status": "completed",
        "processed_responses": processed_count,
        "pending_responses": pending_count - processed_count,
        "cleaned_old_contexts": len(old_message_ids)
    }


@router.get("/slack/health")
def slack_health():
    """Health check for Slack integration"""
    return {
        "status": "ok",
        "slack_bot_token_set": bool(SLACK_BOT_TOKEN),
        "slack_signing_secret_set": bool(SLACK_SIGNING_SECRET),
        "queue_url_set": bool(QUEUE_URL)
    }


@router.get("/slack/status")
def slack_status():
    """Get status of Slack integration"""
    pending_contexts = [
        {
            "message_id": msg_id,
            "channel": ctx.channel,
            "user": ctx.user,
            "timestamp": ctx.timestamp.isoformat(),
            "response_sent": ctx.response_sent,
            "age_seconds": (datetime.now() - ctx.timestamp).total_seconds()
        }
        for msg_id, ctx in slack_context_store.items()
    ]

    return {
        "total_contexts": len(slack_context_store),
        "pending_responses": len([ctx for ctx in slack_context_store.values() if not ctx.response_sent]),
        "completed_responses": len([ctx for ctx in slack_context_store.values() if ctx.response_sent]),
        "contexts": pending_contexts
    }
