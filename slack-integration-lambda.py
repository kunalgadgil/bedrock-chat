import json
import os
import requests
import boto3
from typing import Dict, Any

# Environment variables
BEDROCK_CHAT_API_URL = os.environ.get('BEDROCK_CHAT_API_URL')
BEDROCK_CHAT_API_KEY = os.environ.get('BEDROCK_CHAT_API_KEY')
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET')

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda handler for Slack events integration with Bedrock Chat
    """
    try:
        # Parse the request body
        body = json.loads(event.get('body', '{}'))
        
        # Handle Slack URL verification challenge
        if body.get('type') == 'url_verification':
            return {
                'statusCode': 200,
                'body': body.get('challenge')
            }
        
        # Handle Slack events
        if body.get('type') == 'event_callback':
            slack_event = body.get('event', {})
            
            # Ignore bot messages to prevent loops
            if slack_event.get('bot_id'):
                return {'statusCode': 200, 'body': 'OK'}
            
            # Process different event types
            if slack_event.get('type') in ['app_mention', 'message']:
                handle_message_event(slack_event)
        
        return {'statusCode': 200, 'body': 'OK'}
        
    except Exception as e:
        print(f"Error processing Slack event: {str(e)}")
        return {'statusCode': 500, 'body': 'Internal Server Error'}

def handle_message_event(event: Dict[str, Any]) -> None:
    """
    Handle incoming Slack message events
    """
    channel = event.get('channel')
    user = event.get('user')
    text = event.get('text', '')
    
    # Remove bot mention from text if present
    text = clean_message_text(text)
    
    if not text.strip():
        return
    
    # Send message to Bedrock Chat API
    try:
        response = send_to_bedrock_chat(text, user)
        
        if response:
            # Send response back to Slack
            send_slack_message(channel, response)
            
    except Exception as e:
        print(f"Error handling message: {str(e)}")
        send_slack_message(channel, "Sorry, I encountered an error processing your request.")

def clean_message_text(text: str) -> str:
    """
    Clean Slack message text by removing bot mentions and formatting
    """
    import re
    
    # Remove bot mentions like <@U1234567890>
    text = re.sub(r'<@[UW][A-Z0-9]+>', '', text)
    
    # Remove extra whitespace
    text = ' '.join(text.split())
    
    return text.strip()

def send_to_bedrock_chat(message: str, user_id: str) -> str:
    """
    Send message to Bedrock Chat published API
    """
    headers = {
        'Content-Type': 'application/json',
        'x-api-key': BEDROCK_CHAT_API_KEY
    }
    
    payload = {
        'conversationId': f"slack-{user_id}",  # Use Slack user ID as conversation ID
        'message': {
            'content': [
                {
                    'contentType': 'text',
                    'body': message
                }
            ],
            'model': 'claude-v4-opus'  # Adjust model as needed
        }
    }
    
    try:
        response = requests.post(
            f"{BEDROCK_CHAT_API_URL}/conversation",
            headers=headers,
            json=payload,
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            conversation_id = result.get('conversationId')
            message_id = result.get('messageId')
            
            # Poll for the response (since API is asynchronous)
            return poll_for_response(conversation_id, message_id)
        else:
            print(f"Bedrock Chat API error: {response.status_code} - {response.text}")
            return None
            
    except requests.exceptions.RequestException as e:
        print(f"Request error: {str(e)}")
        return None

def poll_for_response(conversation_id: str, message_id: str, max_attempts: int = 30) -> str:
    """
    Poll the Bedrock Chat API for the response message
    """
    import time
    
    headers = {
        'x-api-key': BEDROCK_CHAT_API_KEY
    }
    
    for attempt in range(max_attempts):
        try:
            response = requests.get(
                f"{BEDROCK_CHAT_API_URL}/conversation/{conversation_id}/{message_id}",
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                message = data.get('message', {})
                content = message.get('content', [])
                
                # Extract text content
                for item in content:
                    if item.get('contentType') == 'text':
                        return item.get('body', '')
                        
            time.sleep(2)  # Wait 2 seconds before next attempt
            
        except requests.exceptions.RequestException as e:
            print(f"Polling error: {str(e)}")
            time.sleep(2)
    
    return "I'm still processing your request. Please try again in a moment."

def send_slack_message(channel: str, text: str) -> None:
    """
    Send message to Slack channel
    """
    headers = {
        'Authorization': f'Bearer {SLACK_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }
    
    payload = {
        'channel': channel,
        'text': text
    }
    
    try:
        response = requests.post(
            'https://slack.com/api/chat.postMessage',
            headers=headers,
            json=payload,
            timeout=10
        )
        
        if not response.json().get('ok'):
            print(f"Slack API error: {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"Error sending Slack message: {str(e)}")
