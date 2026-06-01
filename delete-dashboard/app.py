import os
import json
import re
import requests
import boto3
import hmac
import hashlib
import time
from typing import Dict, Any, List
from langchain_aws import ChatBedrock
from urllib.parse import parse_qs

# Initialize clients
bedrock = boto3.client(service_name="bedrock-agent")
bedrock_runtime = boto3.client(
    'bedrock-runtime',
    config=boto3.session.Config(
        read_timeout=300,
        retries={'max_attempts': 3, 'mode': 'adaptive'}
    )
)
s3_client = boto3.client('s3')
secretsmanager_client = boto3.client('secretsmanager')
grafana_client = boto3.client('grafana')
credentials = boto3.Session().get_credentials()

# Configuration
INFERENCE_PROFILE_ID = os.environ['INFERENCE_PROFILE_ID']
GRAFANA_URL = os.environ['GRAFANA_URL']
GRAFANA_TOKEN = os.environ['GRAFANA_TOKEN']
PROMPT_ID = os.environ['PROMPT_ID']
SLACK_SIGNING_SECRET_NAME = os.environ['SLACK_SIGNING_SECRET']

# Timeouts para requests HTTP (connect, read) em segundos
GRAFANA_TIMEOUT = (5, 300)
SLACK_TIMEOUT = (5, 30)

def get_managed_prompt(prompt_id, user_message):
    try:
        response = bedrock.get_prompt(
            promptIdentifier=prompt_id
        )
        variants = response.get('variants', [])
        if variants:
            template_configuration = variants[0].get('templateConfiguration', {})
            
            if 'text' in template_configuration:
                text_config = template_configuration.get('text', {})
                prompt_content = text_config.get('text', '')
            elif 'chat' in template_configuration:
                chat_config = template_configuration.get('chat', {})
                messages = chat_config.get('messages', [])
                if messages:
                    content = messages[0].get('content', [])
                    if content:
                        prompt_content = content[0].get('text', '')
                    else:
                        prompt_content = ''
                else:
                    prompt_content = ''
            else:
                prompt_content = ''
            
            if prompt_content:
                formatted_prompt = prompt_content.replace('{{user_message}}', str(user_message))
                return formatted_prompt
        
        print("Prompt content not found in the response")
        return ""
    except Exception as e:
        print(f"Error getting managed prompt: {str(e)}")
        raise e

def get_secret(secretName):

    try:
        get_secret_value_response = secretsmanager_client.get_secret_value(
            SecretId=secretName
        )
    
    except Exception as e:
        print(f"Error to get secret: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error to get secret': str(e)})
        }

    secret = get_secret_value_response['SecretString']

    return json.loads(secret)['GRAFANA_TOKEN']

def send_slack_message(url, body):
   try:
      response = requests.post(
         url,
         json=body,
         headers={'Content-Type': 'application/json'},
         timeout=SLACK_TIMEOUT
      )
      if not response.ok:
         print(f"Failed to send Slack message: {response.status_code} - {response.text}")
   except Exception as e:
      print(f"Error sending Slack message: {str(e)}")

def get_slack_signing_secret():
   """Retrieve Slack Signing Secret from Secrets Manager."""
   try:
      response = secretsmanager_client.get_secret_value(
         SecretId=SLACK_SIGNING_SECRET_NAME
      )
      secret = json.loads(response['SecretString'])
      return secret['SLACK_SIGNING_SECRET']
   except Exception as e:
      print(f"Error retrieving Slack signing secret: {str(e)}")
      raise e

def verify_slack_request(event):
   """
   Verify that the incoming request is from Slack using the Signing Secret.
   Raises Exception if invalid.
   """
   headers = event.get('headers', {})
   timestamp = headers.get('X-Slack-Request-Timestamp') or headers.get('x-slack-request-timestamp', '')
   signature = headers.get('X-Slack-Signature') or headers.get('x-slack-signature', '')

   if not timestamp or not signature:
      raise Exception("Missing Slack signature headers")

   if abs(time.time() - int(timestamp)) > 300:
      raise Exception("Slack request timestamp too old (possible replay attack)")

   body = event.get('body', '')
   if isinstance(body, bytes):
      body = body.decode('utf-8')

   signing_secret = get_slack_signing_secret()
   sig_basestring = f"v0:{timestamp}:{body}"
   my_signature = "v0=" + hmac.new(
      signing_secret.encode(),
      sig_basestring.encode(),
      hashlib.sha256
   ).hexdigest()

   if not hmac.compare_digest(my_signature, signature):
      raise Exception("Invalid Slack signature - request not from authorized Slack workspace")


def delete_dashboard(dashboard_names: List[str], grafana_url: str, response_url) -> Dict[str, Any]:
    """Function to delete dashboard"""

    GRAFANA_API_TOKEN = get_secret(GRAFANA_TOKEN)
    headers = {
      "Authorization": f"Bearer {GRAFANA_API_TOKEN}",
      "Content-Type": "application/json",
      "Accept": "application/json",
    }

    results = []
    overall_status = 200

    for dashboard_name in dashboard_names:
        try:
            search_url = f"{grafana_url}/api/search?query={dashboard_name}"
            search_response = requests.get(search_url, headers=headers, timeout=GRAFANA_TIMEOUT)
            search_response.raise_for_status()
        
            dashboards = search_response.json()
            dashboard = next((d for d in dashboards if d['title'].lower() == dashboard_name.lower()), None)

            if not dashboard:
                results.append(f":warning: Dashboard '{dashboard_name}' not found")
                overall_status = 404 if overall_status == 200 else overall_status
                continue
        
            delete_url = f"{grafana_url}/api/dashboards/uid/{dashboard['uid']}"
            delete_response = requests.delete(delete_url, headers=headers, timeout=GRAFANA_TIMEOUT)
            delete_response.raise_for_status()

            results.append(f":white_check_mark: Dashboard '{dashboard_name}' deleted successfully")

        except requests.exceptions.RequestException as e:
            results.append(f":x: Error deleting dashboard '{dashboard_name}': {str(e)}")
            overall_status = 500

    slack_response = {
        'response_type': 'in_channel',
        'text': "*Result of dashboard deletion operation:*\n" + "\n".join(results)
    }

    send_slack_message(response_url, slack_response)
    
    return {
        'response': {
            'statusCode': overall_status,
            'body': json.dumps({'message': "\n".join(results)})
        }
    }

def delete_panel(items: List[Dict[str, str]], grafana_url: str, response_url) -> Dict[str, Any]:
    """Function to delete specific panel"""

    GRAFANA_API_TOKEN = get_secret(GRAFANA_TOKEN)
    headers = {
      "Authorization": f"Bearer {GRAFANA_API_TOKEN}",
      "Content-Type": "application/json",
      "Accept": "application/json",
    }

    results = []
    overall_status = 200

    for item in items:
        dashboard_name = item['dashboard_name']
        panel_name = item['name']

        try:
            search_url = f"{grafana_url}/api/search?query={dashboard_name}"
            search_response = requests.get(search_url, headers=headers, timeout=GRAFANA_TIMEOUT)
            search_response.raise_for_status()
            
            dashboards = search_response.json()
            dashboard = next((d for d in dashboards if d['title'].lower() == dashboard_name.lower()), None)
            
            if not dashboard:
                results.append(f":warning: Dashboard '{dashboard_name}' not found")
                overall_status = 404 if overall_status == 200 else overall_status
                continue
            
            dashboard_url = f"{grafana_url}/api/dashboards/uid/{dashboard['uid']}"
            dashboard_response = requests.get(dashboard_url, headers=headers, timeout=GRAFANA_TIMEOUT)
            dashboard_response.raise_for_status()
            dashboard_data = dashboard_response.json()
            
            panels = dashboard_data['dashboard']['panels']
            original_length = len(panels)
            dashboard_data['dashboard']['panels'] = [p for p in panels if p['title'].lower() != panel_name.lower()]
            
            if len(dashboard_data['dashboard']['panels']) == original_length:
                results.append(f":warning: Panel '{panel_name}' not found in dashboard '{dashboard_name}'")
                overall_status = 404 if overall_status == 200 else overall_status
                continue
            
            update_payload = {
                "dashboard": dashboard_data['dashboard'],
                "overwrite": True
            }
            
            update_url = f"{grafana_url}/api/dashboards/db"
            update_response = requests.post(update_url, headers=headers, json=update_payload, timeout=GRAFANA_TIMEOUT)
            update_response.raise_for_status()
            
            results.append(f":white_check_mark: Panel '{panel_name}' deleted successfully from dashboard '{dashboard_name}'")
            
        except requests.exceptions.RequestException as e:
            results.append(f":x: Error deleting panel '{panel_name}' from dashboard '{dashboard_name}': {str(e)}")
            overall_status = 500

    slack_response = {
        'response_type': 'in_channel',
        'text': "*Result of panel deletion operation:*\n" + "\n".join(results)
    }

    send_slack_message(response_url, slack_response)

    return {
        'response': {
            'statusCode': overall_status,
            'body': json.dumps({'message': "\n".join(results)})
        }
    }

def process_llm_response(answer, response_url):
    try:
        json_content = re.search(r'```json\n(.*?)\n```', answer.content, re.DOTALL)
        if not json_content:
            raise ValueError("JSON not found in response")
            
        parsed_intent = json.loads(json_content.group(1))

        if 'action' not in parsed_intent or 'items' not in parsed_intent:
            raise ValueError('Invalid response format from Bedrock')

        if parsed_intent['action'] == 'delete_dashboards':
            dashboard_names = [item['name'] for item in parsed_intent['items']]
            return delete_dashboard(dashboard_names, GRAFANA_URL, response_url)
            
        elif parsed_intent['action'] == 'delete_panels':
            return delete_panel(parsed_intent['items'], GRAFANA_URL, response_url)
            
        else:
            raise ValueError(f'Invalid action: {parsed_intent["action"]}')

    except json.JSONDecodeError as e:
        raise ValueError(f"Error decoding JSON: {str(e)}")
    except Exception as e:
        raise ValueError(f"Error processing response: {str(e)}")

def lambda_handler(event, context):

    try:
        # Verify the request is from Slack
        verify_slack_request(event)

        body = event.get('body', '')
        params = parse_qs(body)
        response_url = params.get('response_url', [''])[0]
        user_message = params.get('text', [''])[0]

        model_kwargs =  { 
            "max_tokens": 25000,
            "temperature": 0.0,
            "top_k": 0,
        }

        managed_prompt = get_managed_prompt(PROMPT_ID, user_message)

        llm = ChatBedrock(model_id=INFERENCE_PROFILE_ID, client=bedrock_runtime, model_kwargs=model_kwargs)
        answer = llm.invoke(managed_prompt)

        result = process_llm_response(answer, response_url)
        
    except Exception as e:
        error_message = str(e)
        print(f"Error: {error_message}")
        if 'response_url' in dir() and response_url:
            send_slack_message(response_url, {
                'response_type': 'ephemeral',
                'text': f":x: *Error*: {error_message}"
            })
        return {
            'statusCode': 403 if 'signature' in error_message.lower() or 'Slack' in error_message else 500,
            'body': json.dumps({'error': error_message})
        }
