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
from app.routes.schemas.published_api import ChatInputWithoutBotId, MessageInputWithoutMessageId
from app.user import User
from app.usecases.chat import fetch_conversation
from fastapi import APIRouter, HTTPException, Request, Response, BackgroundTasks
from pydantic import BaseModel
from ulid import ULID

router = APIRouter(tags=["slack_webhook"])

# Environment variables
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET', '')
SLACK_MODEL = os.environ.get('SLACK_MODEL', 'amazon-nova-micro')

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
    print(f"Request headers: {dict(request.headers)}")

    # ALWAYS verify Slack signature first (for all requests including URL verification)
    if not verify_slack_signature(body_str, dict(request.headers)):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        slack_data = json.loads(body_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Handle URL verification challenge AFTER signature verification
    if slack_data.get('type') == 'url_verification':
        challenge = slack_data.get('challenge', '')
        print(f"URL verification challenge: {challenge}")
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
    print(f"Using AI model: {SLACK_MODEL}")

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

    print(f"Current user ID: {current_user.id}")
    print(f"Extracted bot_id: {bot_id}")
    print(f"User groups: {current_user.groups}")

    # Don't create conversation ID - let the system generate one
    # Generate message ID for tracking
    response_message_id = str(ULID())

    # Use the published API schema which doesn't require conversation_id
    chat_input_without_bot = ChatInputWithoutBotId(
        conversation_id=None,  # Let the system generate a new conversation
        message=MessageInputWithoutMessageId(
            content=[
                {
                    "contentType": "text",
                    "body": cleaned_text
                }
            ],
            model=SLACK_MODEL,
        ),
        continue_generate=False,
        enable_reasoning=False,
    )

    try:
        # Follow the published API pattern exactly
        conversation_id = str(ULID())  # Generate new conversation ID

        # Create ChatInput for SQS (same format as published API)
        chat_input = ChatInput(
            conversation_id=conversation_id,
            message=MessageInput(
                role="user",
                content=chat_input_without_bot.message.content,
                model=chat_input_without_bot.message.model,
                parent_message_id=None,  # Use the latest message as the parent
                message_id=response_message_id,
            ),
            bot_id=bot_id,
            continue_generate=chat_input_without_bot.continue_generate,
            enable_reasoning=chat_input_without_bot.enable_reasoning,
        )

        # Send to SQS for processing (same as published API)
        sqs_response = sqs_client.send_message(
            QueueUrl=QUEUE_URL, MessageBody=chat_input.model_dump_json()
        )
        print(f"Sent message to SQS - Message ID: {response_message_id}, Conversation ID: {conversation_id}")
        print(f"SQS Message ID: {sqs_response.get('MessageId')}")

        # Send immediate response to Slack
        await send_slack_response(channel, "Processing your request...")

        # Store the context for polling (same as published API pattern)
        store_slack_context(response_message_id, channel, conversation_id, user, bot_id)

        # Start polling in background task (like published API client would do)
        background_tasks.add_task(poll_slack_response, response_message_id, conversation_id, channel, bot_id)

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
        
        # Get signature and timestamp from headers (case-insensitive)
        slack_signature = headers.get('x-slack-signature') or headers.get('X-Slack-Signature', '')
        slack_timestamp = headers.get('x-slack-request-timestamp') or headers.get('X-Slack-Request-Timestamp', '')
        
        if not slack_signature or not slack_timestamp:
            print(f"Missing signature or timestamp. Signature: {bool(slack_signature)}, Timestamp: {bool(slack_timestamp)}")
            print(f"Available headers: {list(headers.keys())}")
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

        # Debug logging
        print(f"Signature verification: {is_valid}")
        if not is_valid:
            print(f"Expected signature: {my_signature}")
            print(f"Received signature: {slack_signature}")
            print(f"Base string: {sig_basestring}")

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


async def poll_slack_response(message_id: str, conversation_id: str, channel: str, bot_id: str):
    """Poll for AI response using the same logic as published API clients"""
    print(f"Starting polling for Slack response - Message: {message_id}, Conversation: {conversation_id}")

    max_attempts = 30  # Poll for up to 30 seconds (like a typical API client)
    poll_interval = 1  # Poll every 1 second

    for attempt in range(max_attempts):
        try:
            print(f"Polling attempt {attempt + 1}/{max_attempts}")

            # Check if already sent
            context = slack_context_store.get(message_id)
            if context and context.response_sent:
                print(f"Response already sent for message {message_id}")
                return

            # Create user object (same as published API)
            user = User.from_published_api_id(bot_id)

            # Try to get the message using published API logic
            try:
                conversation = fetch_conversation(user.id, conversation_id)
                input_message = conversation.message_map.get(message_id)

                if input_message is None:
                    print(f"Input message {message_id} not found yet")
                    await asyncio.sleep(poll_interval)
                    continue

                # Check if response message exists (same logic as published API)
                if not input_message.children:
                    print(f"No response message yet for {message_id}")
                    await asyncio.sleep(poll_interval)
                    continue

                output_message_id = input_message.children[0]
                output_message = conversation.message_map.get(output_message_id)

                if output_message is None:
                    print(f"Output message {output_message_id} not found yet")
                    await asyncio.sleep(poll_interval)
                    continue

                # Extract response text
                response_text = ""
                for content_block in output_message.content:
                    if isinstance(content_block, dict):
                        if content_block.get('contentType') == "text":
                            response_text += content_block.get('body', '')
                    else:
                        if hasattr(content_block, 'contentType') and content_block.contentType == "text":
                            response_text += content_block.body

                if response_text.strip():
                    # Send response to Slack
                    await send_slack_response(channel, response_text)

                    # Mark as sent
                    if context:
                        context.response_sent = True
                        slack_context_store[message_id] = context

                    print(f"Successfully sent Slack response for message {message_id}")
                    return
                else:
                    print(f"Response message found but no text content")

            except Exception as fetch_error:
                if "No conversation found" in str(fetch_error):
                    print(f"Conversation not ready yet (attempt {attempt + 1})")
                else:
                    print(f"Error fetching conversation: {fetch_error}")

            await asyncio.sleep(poll_interval)

        except Exception as e:
            print(f"Error in polling attempt {attempt + 1}: {str(e)}")
            await asyncio.sleep(poll_interval)

    # Timeout - send timeout message
    print(f"Polling timed out for message {message_id}")
    await send_slack_response(channel, "⏰ Sorry, the request timed out. Please try again.")

    # Mark as sent to prevent further attempts
    context = slack_context_store.get(message_id)
    if context:
        context.response_sent = True
        slack_context_store[message_id] = context














@router.get("/slack/health")
def slack_health():
    """Health check for Slack integration"""
    return {
        "status": "ok",
        "slack_bot_token_set": bool(SLACK_BOT_TOKEN),
        "slack_signing_secret_set": bool(SLACK_SIGNING_SECRET),
        "queue_url_set": bool(QUEUE_URL)
    }


@router.post("/slack/check-all-pending")
async def check_all_pending_responses():
    """Check all pending responses and send them to Slack (manual trigger)"""
    processed = 0
    errors = 0

    for message_id, context in slack_context_store.items():
        if not context.response_sent:
            try:
                # Use the same logic as published API to check for response
                user = User.from_published_api_id(context.bot_id)
                conversation = fetch_conversation(user.id, context.conversation_id)
                input_message = conversation.message_map.get(message_id)

                if input_message and input_message.children:
                    output_message_id = input_message.children[0]
                    output_message = conversation.message_map.get(output_message_id)

                    if output_message and output_message.content:
                        # Extract response text
                        response_text = ""
                        for content_block in output_message.content:
                            if isinstance(content_block, dict):
                                if content_block.get('contentType') == "text":
                                    response_text += content_block.get('body', '')
                            else:
                                if hasattr(content_block, 'contentType') and content_block.contentType == "text":
                                    response_text += content_block.body

                        if response_text.strip():
                            await send_slack_response(context.channel, response_text)
                            context.response_sent = True
                            slack_context_store[message_id] = context
                            processed += 1

            except Exception as e:
                print(f"Error checking message {message_id}: {str(e)}")
                errors += 1

    return {
        "status": "completed",
        "processed": processed,
        "errors": errors,
        "total_pending": len([ctx for ctx in slack_context_store.values() if not ctx.response_sent])
    }


@router.get("/slack/status")
def slack_status():
    """Get status of Slack integration"""
    pending_contexts = [
        {
            "message_id": msg_id,
            "conversation_id": ctx.conversation_id,
            "channel": ctx.channel,
            "user": ctx.user,
            "bot_id": ctx.bot_id,
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
        "sqs_queue_url": QUEUE_URL,
        "slack_model": SLACK_MODEL,
        "slack_bot_token_set": bool(SLACK_BOT_TOKEN),
        "slack_signing_secret_set": bool(SLACK_SIGNING_SECRET),
        "contexts": pending_contexts
    }
