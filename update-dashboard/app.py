import json
import boto3
import re
import requests
import csv
import io
import os
import hmac
import hashlib
import time
from urllib.parse import parse_qs
from langchain_aws import ChatBedrock
from langchain_aws import AmazonKnowledgeBasesRetriever
from langchain_core.messages import AIMessage

# Initialize clients
bedrock = boto3.client(service_name="bedrock-agent")
bedrock_runtime = boto3.client(
    'bedrock-runtime',
    config=boto3.session.Config(
        read_timeout=300,
        retries={'max_attempts': 3, 'mode': 'adaptive'}
    )
)
bedrock_kb = boto3.client('bedrock-agent-runtime') 
s3_client = boto3.client('s3')
secretsmanager_client = boto3.client('secretsmanager')
grafana_client = boto3.client('grafana')
credentials = boto3.Session().get_credentials()

# Configuration
INFERENCE_PROFILE_ID = os.environ['INFERENCE_PROFILE_ID']
GRAFANA_URL = os.environ['GRAFANA_URL']
GRAFANA_TOKEN = os.environ['GRAFANA_TOKEN']
BUCKET_NAME = os.environ['BUCKET_NAME']
BUCKET_FILE = os.environ['BUCKET_FILE']
PROMPT_ID = os.environ['PROMPT_ID']
KNOWLEDGE_BASE_ID = os.environ['KNOWLEDGE_BASE_ID']
SLACK_SIGNING_SECRET_NAME = os.environ['SLACK_SIGNING_SECRET']

# Timeouts para requests HTTP (connect, read) em segundos
GRAFANA_TIMEOUT = (5, 300)
SLACK_TIMEOUT = (5, 30)


retriever = AmazonKnowledgeBasesRetriever(
    knowledge_base_id=KNOWLEDGE_BASE_ID,
    client=bedrock_kb,
    retrieval_config={
        "vectorSearchConfiguration": {
            "numberOfResults": 100
        }
    }
)

def get_managed_prompt(prompt_id, context_data, user_message):
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
                context_str = json.dumps(context_data) if isinstance(context_data, list) else str(context_data)

                all_relevant_docs = []
                max_pages = 2
                next_token = None
                
                for page in range(max_pages):
                    try:
                        retrieval_config = {
                            "vectorSearchConfiguration": {
                                "numberOfResults": 100
                            }
                        }
                        
                        retrieve_params = {
                            "knowledgeBaseId": KNOWLEDGE_BASE_ID,
                            "retrievalQuery": {
                                "text": user_message
                            },
                            "retrievalConfiguration": retrieval_config
                        }
                        
                        if next_token:
                            retrieve_params["nextToken"] = next_token
                        
                        response = bedrock_kb.retrieve(**retrieve_params)
                        
                        retrieval_results = response.get('retrievalResults', [])
                        for result in retrieval_results:
                            content = result.get('content', {}).get('text', '')
                            if content:
                                all_relevant_docs.append(content)
                        
                        next_token = response.get('nextToken')
                        if not next_token:
                            break
                        
                    except Exception as e:
                        print(f"Error to recovery page {page + 1}: {str(e)}")
                        break
                
                unique_docs = list(set(all_relevant_docs))
                
                context_ins_content = unique_docs
                context_ins_str = json.dumps(context_ins_content)
                
                formatted_prompt = prompt_content.replace('{{context}}', context_str)
                formatted_prompt = formatted_prompt.replace('{{contextEKS}}', context_ins_str)
                formatted_prompt = formatted_prompt.replace('{{user_message}}', str(user_message))

                return formatted_prompt
        
        print("Prompt content not found in the response")
        return ""
    except Exception as e:
        print(f"Error getting managed prompt: {str(e)}")
        raise e

def format_context(docs):
    return "\n\n".join([doc.page_content for doc in docs])

def read_file_from_s3(bucket, key):
   response = s3_client.get_object(Bucket=bucket, Key=key)
   csv_content = response['Body'].read().decode('utf-8')
   csv_file = io.StringIO(csv_content)
   csv_reader = csv.reader(csv_file)
   csv_data = list(csv_reader)
   
   return csv_data

def extract_json_from_content(content):
    json_match = re.search(r'```json\n(.*?)\n```', content, re.DOTALL)
    if json_match:
        json_content = json_match.group(1)
        try:
            return json.loads(json_content)
        except json.JSONDecodeError:
            return None
    return None

def extract_panel_config(ai_message):
     try:
         if isinstance(ai_message, AIMessage):
            content = ai_message.content
         elif isinstance(ai_message, str):
            content = ai_message
         else:
            content = str(ai_message)

         json_match = re.search(r'```json\n(.*?)\n```', content, re.DOTALL)
         if json_match:
            content = json_match.group(1)
         
         parsed_content = json.loads(content)
   
         if 'panels' in parsed_content:
            return parsed_content['panels']
         elif 'body' in parsed_content and 'panels' in parsed_content['body']:
            return parsed_content['body']['panels']
         elif 'dashboard' in parsed_content and 'panels' in parsed_content['dashboard']:
            return parsed_content['dashboard']['panels']
         elif 'title' in parsed_content and 'type' in parsed_content and 'targets' in parsed_content:
            return [parsed_content]
         elif 'data' in parsed_content:
            if 'dashboard' in parsed_content['data']:
                return parsed_content['data']['dashboard'].get('panels', [])
            elif 'panels' in parsed_content['data']:
                return parsed_content['data']['panels']
            elif 'title' in parsed_content['data']:
                return [parsed_content['data']]
         return []
     
     except Exception as e:
        print(f"Failed to extract panels: {str(e)}")
        return []

def extract_dashboard(ai_message):
    try:
        content = ai_message.content
        json_data = json.loads(content)
        return json_data.get('dashboard')
    except Exception as e:
        print(f"Error to extract dashboard: {str(e)}")
        return None

def extract_code(response):
   code_blocks = re.findall(r'```(?:json)?\n(.*?)```', response, re.DOTALL)
   if code_blocks:
      return '\n\n'.join(code_blocks)

   return response

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

# Função para buscar dashboard por nome
def get_dashboard_by_name(name):
    GRAFANA_API_TOKEN = get_secret(GRAFANA_TOKEN)

    headers = {
      "Authorization": f"Bearer {GRAFANA_API_TOKEN}",
      "Content-Type": "application/json",
      "Accept": "application/json",
    }
    
    search_url = f"{GRAFANA_URL}/api/search?query={name}"
    response = requests.get(search_url, headers=headers, timeout=GRAFANA_TIMEOUT)
    if response.status_code == 200:
        dashboards = response.json()
        for dashboard in dashboards:
            if dashboard['title'].lower() == name.lower():
                return dashboard
    return None

def get_dashboard_json(uid):
    GRAFANA_API_TOKEN = get_secret(GRAFANA_TOKEN)

    headers = {
      "Authorization": f"Bearer {GRAFANA_API_TOKEN}",
      "Content-Type": "application/json",
      "Accept": "application/json",
    }
    dashboard_url = f"{GRAFANA_URL}/api/dashboards/uid/{uid}"
    response = requests.get(dashboard_url, headers=headers, timeout=GRAFANA_TIMEOUT)
    if response.status_code == 200:
        return response.json()
    return None

def extract_dashboard_content(dashboard_data):
    try:
        if isinstance(dashboard_data, str):
            dashboard_data = json.loads(dashboard_data)
        
        return {'dashboard': dashboard_data['dashboard']}

    except Exception as e:
        print(f"Error to extract dashboard content: {str(e)}")
        return {}


def create_visualization_with_bedrock(user_message):
    model_kwargs =  { 
      "max_tokens": 25000,
      "temperature": 0.0,
      "top_k": 0,
    }

    llm = ChatBedrock(model_id=INFERENCE_PROFILE_ID, client=bedrock_runtime, model_kwargs=model_kwargs)
    context = read_file_from_s3(BUCKET_NAME, BUCKET_FILE)

    managed_prompt = get_managed_prompt(PROMPT_ID, context, user_message)
    response = llm.invoke(managed_prompt)

    panel_config = extract_panel_config(response)

    try:
        return panel_config
    except json.JSONDecodeError:
        print("Error: Invalid JSON response from Bedrock")
        return None

def update_dashboard(response_url, dashboard_json):
    url = f"{GRAFANA_URL}/api/dashboards/db"
    GRAFANA_API_TOKEN = get_secret(GRAFANA_TOKEN)

    headers = {
      "Authorization": f"Bearer {GRAFANA_API_TOKEN}",
      "Content-Type": "application/json",
      "Accept": "application/json",
    }
    update_url = f"{GRAFANA_URL}/api/dashboards/db"
    try:
      response = requests.post(update_url, headers=headers, json=dashboard_json, timeout=GRAFANA_TIMEOUT)
      if response.ok:
         dashboard_url = json.loads(response.text).get('url')
         return {
               'statusCode': 200,
               'body': json.dumps({
                  'dashboard_url': dashboard_url,
                  'message': 'Dashboard updated successfully'
               })
         }
      else:
         error_response = json.loads(response.text)
         error_message = f"Error updating dashboard: {error_response['message']}"
         raise Exception(error_message)

    except requests.exceptions.RequestException as e:
      if hasattr(e, 'response') and e.response is not None:
         error_response = json.loads(e.response.text)
         error_message = f"Error updating dashboard: {error_response['message']}"
         send_slack_message(response_url, {
            'response_type': 'ephemeral',
            'text': error_message
         })
         raise Exception(error_message)
      raise Exception(f"Error updating dashboard: {str(e)}")

def concatenate_panels(dashboard_data, new_panels):
    try:
        if not isinstance(dashboard_data, dict) or 'dashboard' not in dashboard_data:
            raise ValueError("dashboard_data inválido")

        max_y = 0
        existing_panels = dashboard_data['dashboard'].get('panels', [])
        for panel in existing_panels:
            grid_pos = panel.get('gridPos', {})
            panel_y = grid_pos.get('y', 0)
            panel_h = grid_pos.get('h', 0)
            max_y = max(max_y, panel_y + panel_h)

        for panel in new_panels:
            if 'gridPos' in panel:
                panel['gridPos']['y'] += max_y
            if 'id' in panel:
                del panel['id']

        dashboard_data['dashboard']['panels'].extend(new_panels)
        
        if 'version' in dashboard_data['dashboard']:
            dashboard_data['dashboard']['version'] += 1

        return dashboard_data

    except Exception as e:
        print(f"Error to concatenate panels: {str(e)}")
        return dashboard_data

def lambda_handler(event, context):
   try:
      # Verify the request is from Slack
      verify_slack_request(event)

      body = event.get('body', '')
      params = parse_qs(body)

      response_url = params.get('response_url', [''])[0]
      text = params.get('text', [''])[0]
      parts = text.split(',', 1) 

      if len(parts) < 2:
         return {
               "statusCode": 400,
               "body": json.dumps({
                  "response_type": "ephemeral",
                  "text": "Invalid format. Use: /update-dashboard Dashboard Name, Your question"
               })
         }
      dashboard_name = parts[0].strip()
      user_message = parts[1].strip()
      dashboard = get_dashboard_by_name(dashboard_name)
      if not dashboard:
        print(f"Dashboard '{dashboard_name}' not found.")
        send_slack_message(response_url, {
            'response_type': 'ephemeral',
            'text': f"Dashboard '{dashboard_name}' not found."
        })
        exit()

      dashboard_data = get_dashboard_json(dashboard['uid'])
      if not dashboard_data:
         return {
               'statusCode': 500,
               'body': json.dumps(f"Failed to get data for dashboard '{dashboard_name}'.")
         }
      dashboard_content = extract_dashboard_content(dashboard_data)    
      new_panel = create_visualization_with_bedrock(user_message)

      if not new_panel:
        return {
            'statusCode': 500,
            'body': json.dumps("Failed to create new visualization.")
        }
      

      updated_dashboard = concatenate_panels(dashboard_content, new_panel)

      update_payload = {
         "dashboard": updated_dashboard['dashboard'],
         "overwrite": True
      }

      response = update_dashboard(response_url, update_payload)
      response_body = json.loads(response['body'])
      url_path = response_body['dashboard_url']
      dashboard_url = GRAFANA_URL + url_path

      slack_response = {
         'response_type': 'in_channel',
         'text': f'Dashboard successfully updated! :white_check_mark:',
         'blocks': [
         {
            'type': 'section',
            'text': {
               'type': 'mrkdwn',
               'text': f'*Dashboard successfully updated!* :white_check_mark:\n<{dashboard_url}|Click here to access the dashboard>'
            }
         }
         ]    
      }

      send_slack_message(response_url, slack_response)

      return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'application/json'
         },
        'body': response['body']
      }
   
   except Exception as e:
      print(f"Error: {str(e)}")
      if 'response_url' in dir() and response_url:
         send_slack_message(response_url, {
               'response_type': 'ephemeral',
               'text': str(e)
         })
      return {
         'statusCode': 403 if 'signature' in str(e).lower() or 'Slack' in str(e) else 200,
         'headers': {'Content-Type': 'application/json'},
         'body': json.dumps({
               'response_type': 'ephemeral',
               'text': f'Error: {str(e)} :x'
         })
      }