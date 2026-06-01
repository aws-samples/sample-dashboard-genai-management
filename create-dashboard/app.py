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

# S3 client with special VPC configuration
s3_client = boto3.client(
    's3',
    use_ssl=True,
    config=boto3.session.Config(
        signature_version='s3v4',
        s3={'addressing_style': 'path'},  # Use path-style instead of virtual-hosted
        retries={'max_attempts': 5, 'mode': 'adaptive'},
        connect_timeout=10,
        read_timeout=60
    )
)

secretsmanager_client = boto3.client('secretsmanager')
grafana_client = boto3.client('grafana')
credentials = boto3.Session().get_credentials()

# Configuration
INFERENCE_PROFILE_ID = os.environ['INFERENCE_PROFILE_ID']
GRAFANA_URL = os.environ['GRAFANA_URL']
BUCKET_NAME = os.environ['BUCKET_NAME']
BUCKET_FILE = os.environ['BUCKET_FILE']
PROMPT_ID = os.environ['PROMPT_ID']
GRAFANA_TOKEN = os.environ['GRAFANA_TOKEN']
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

base_json = {
   "dashboard":{
      "annotations":{
         "list":[
            {
                "builtIn": 1,
                "datasource": {
                "type": "grafana",
                "uid": "-- Grafana --"
                },
                "enable": "",
                "hide": "",
                "iconColor": "rgba(0, 211, 255, 1)",
                "name": "Annotations & Alerts",
                "type": "dashboard"
            }
         ]
      },
      "graphTooltip":0,
      "links":[
         
      ],
      "panels":[
         {
            "datasource":{
               "type":"",
               "uid":""
            },
            "fieldConfig":{
               "defaults":{
                  "color":{
                     "mode":"palette-classic"
                  },
                  "custom":{
                     "axisLabel":"",
                     "axisPlacement":"auto",
                     "barAlignment":0,
                     "drawStyle":"bars",
                     "fillOpacity":80,
                     "gradientMode":"none",
                     "lineInterpolation":"linear",
                     "lineWidth":1,
                     "pointSize":5,
                     "scaleDistribution":{
                        "type":"linear"
                     },
                     "showPoints":"auto",
                     "stacking":{
                        "group":"A",
                        "mode":"none"
                     },
                     "thresholdsStyle":{
                        "mode":"off"
                     }
                  },
                  "mappings":[
                     
                  ],
               },
               "overrides":[
                  
               ]
            },
            "gridPos":{
               "h":8,
               "w":12,
               "x":0,
               "y":0
            },
            "id":2,
            "options":{
               "barWidth":0.97,
               "groupWidth":0.7,
               "legend":{
                  "calcs":[
                     
                  ],
                  "displayMode":"list",
                  "placement":"bottom"
               },
               "orientation":"auto",
               "showValue":"auto",
               "text":{
                  
               },
               "tooltip":{
                  "mode":"single"
               }
            },
            "targets":[
               {
                  "alias":"",
                  "dimensions":{
                  },
                  "expression":"",
                  "id":"",
                  "metricEditorMode":0,
                  "metricName":"",
                  "metricQueryType":0,
                  "namespace":"",
                  "matchExact": "",
                  "period":"",
                  "queryMode":"Metrics",
                  "region":"",
                  "statistic": ""
               }
            ],
            "title":"",
            "type":""
         }
      ],
      "tags":[
         
      ],
      "templating":{
         "list":[
            
         ]
      },
      "time": {
        "from": "",
        "to": ""
      },
      "timepicker":{
         
      },
      "timezone":"browser",
      "title":""
   }
}

def extract_code(response):
   """Extract JSON from Bedrock response"""
   try:
      if isinstance(response, str):
         content = response
      else:
         content = str(response)
      
      json_block_pattern = r'```json\n(.*?)\n```'
      json_match = re.search(json_block_pattern, content, re.DOTALL)
      
      if json_match:
         json_content = json_match.group(1)
         
         json_lines = json_content.split('\n')
         for i, line in enumerate(json_lines):
            if line.strip().startswith('{'):
               json_content = '\n'.join(json_lines[i:])
               break
         
         try:
            json_obj = json.loads(json_content)
            
            if 'dashboard' in json_obj:
               dashboard_content = json_obj['dashboard']
               return json.dumps({"dashboard": dashboard_content}, indent=2)
            
         except json.JSONDecodeError as e:
            print(f"Error to decode JSON from code block: {e}")
      
      try:
         start_idx = content.find('{')
         if start_idx != -1:
            brace_count = 0
            end_idx = start_idx
            for i in range(start_idx, len(content)):
               if content[i] == '{':
                  brace_count += 1
               elif content[i] == '}':
                  brace_count -= 1
                  if brace_count == 0:
                     end_idx = i + 1
                     break
            
            if end_idx > start_idx:
               json_content = content[start_idx:end_idx]
               json_obj = json.loads(json_content)
               
               if 'dashboard' in json_obj:
                  return json.dumps({"dashboard": json_obj['dashboard']}, indent=2)
               else:
                  return json.dumps({"dashboard": json_obj}, indent=2)
      except Exception as e:
         print(f"Error extracting JSON directly: {e}")
      
      print(f"Failed to extract JSON. Response content (first 500 chars):")
      print(content[:500])
      return None
      
   except Exception as e:
      print(f"Error in extract_code: {e}")
      print(f"Response type: {type(response)}")
      print(f"Response content (first 500 chars): {str(response)[:500]}")
      return None

def create_dashboard(response_url, dashboard_data):
   url = f"{GRAFANA_URL}/api/dashboards/db"
   GRAFANA_API_TOKEN = get_secret(GRAFANA_TOKEN)
   headers = {
      "Authorization": f"Bearer {GRAFANA_API_TOKEN}",
      "Content-Type": "application/json",
      "Accept": "application/json",
   }

   try:
      dashboard_data = json.loads(dashboard_data)
      response = requests.post(url, headers=headers, json=dashboard_data, timeout=GRAFANA_TIMEOUT)

      if response.ok:
         dashboard_url = json.loads(response.text).get('url')
         return {
            'statusCode': 200,
            'body': json.dumps({
               'dashboard_url': dashboard_url,
               'message': 'Dashboard created successfully'
            })
         }
      else:
         error_response = json.loads(response.text)
         error_message = f"Error to create dashboard:: {error_response['message']}"

         return {
            'statusCode': response.status_code,
            'error': True,
            'error_message': error_message,
            'error_details': error_response,
            'dashboard_config': dashboard_data
         }

   except requests.exceptions.RequestException as e:
      if hasattr(e, 'response') and e.response is not None:
         error_response = json.loads(e.response.text)
         error_message = f"Error to create dashboard: {error_response['message']}"
         return {
            'statusCode': 500,
            'error': True,
            'error_message': error_message,
            'error_details': error_response,
            'dashboard_config': dashboard_data
         }
      return {
         'statusCode': 500,
         'error': True,
         'error_message': f"Error to create dashboard: {str(e)}",
         'dashboard_config': dashboard_data
      }

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

def get_managed_prompt(prompt_id, context_data, user_message, error_history=None):
   try:
      response = bedrock.get_prompt(
         promptIdentifier=prompt_id
      )
      variants = response.get('variants', [])
      if variants:
         template_configuration = variants[0].get('templateConfiguration', {})
         
         text_config = template_configuration.get('text', {})
         prompt_content = text_config.get('text', '')
         
         if prompt_content:
            context_str = json.dumps(context_data) if isinstance(context_data, list) else str(context_data)

            search_on_knowledgeBase = "cpu, memory, file system, replicas, storage, requests, pods, nodes " + user_message
            relevant_docs = retriever.invoke(search_on_knowledgeBase)
            context_ins_content = [doc.page_content for doc in relevant_docs] if isinstance(relevant_docs, list) else relevant_docs.page_content
            context_ins_str = json.dumps(context_ins_content)

            base_json_str = json.dumps(base_json)
            
            formatted_prompt = prompt_content.replace('{{context}}', context_str)
            formatted_prompt = formatted_prompt.replace('{{contextEKS}}', context_ins_str)
            formatted_prompt = formatted_prompt.replace('{{user_message}}', str(user_message))
            formatted_prompt = formatted_prompt.replace('{{base_json}}', base_json_str)
            
            # If there is error history, add to prompt
            if error_history:
               error_context = "\n\n=== PREVIOUS ATTEMPTS FAILED ===\n"
               for i, err in enumerate(error_history, 1):
                  error_context += f"\nAttempt {i}:\n"
                  error_context += f"Error: {err['error_message']}\n"
                  if 'error_details' in err:
                     error_context += f"Details: {json.dumps(err['error_details'], indent=2)}\n"
                  error_context += f"Generated config: {json.dumps(err.get('dashboard_config'), indent=2)}\n"
               
               error_context += "\n=== CRITICAL INSTRUCTIONS FOR RETRY ===\n"
               error_context += "1. Analyze the error messages above carefully\n"
               error_context += "2. Identify what went wrong (invalid metric, wrong format, missing field, etc.)\n"
               error_context += "3. Generate a CORRECTED dashboard configuration that fixes these errors\n"
               error_context += "4. Common Grafana API errors and fixes:\n"
               error_context += "   - 'metric not found' → verify metric exists in {{context}} or {{contextEKS}}\n"
               error_context += "   - 'invalid datasource' → use correct datasource UID or 'default'\n"
               error_context += "   - 'missing required field' → add all required fields\n"
               error_context += "   - 'invalid JSON structure' → fix JSON format\n"
               error_context += "5. DO NOT repeat the same mistake from previous attempts\n\n"
               
               formatted_prompt = error_context + formatted_prompt
            
            return formatted_prompt
      
      print("Prompt content not found in the response")
      return ""
   except Exception as e:
      print(f"Error getting managed prompt: {str(e)}")
      raise e



def read_file_from_s3(bucket, key):
   try:
      response = s3_client.get_object(Bucket=bucket, Key=key)
      csv_content = response['Body'].read().decode('utf-8')
      csv_file = io.StringIO(csv_content)
      csv_reader = csv.reader(csv_file)
      csv_data = list(csv_reader)
      return csv_data
   except Exception as e:
      print(f"ERROR to read S3 file: {str(e)}")
      print(f"Error type: {type(e).__name__}")
      import traceback
      print(f"Traceback: {traceback.format_exc()}")
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
   Returns True if valid, raises Exception if invalid.
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

def create_dashboard_with_retry(response_url, user_message, context, llm, max_retries=3):
   """
   Try create dashboard with automatic retry in error cases.
   The bedrock model analyzes the error e tries to correct.
   """
   error_history = []
   
   for attempt in range(max_retries):
      try:
         print(f"\n=== Attempt {attempt + 1}/{max_retries} ===")
         
         managed_prompt = get_managed_prompt(
            PROMPT_ID, 
            context, 
            user_message, 
            error_history if attempt > 0 else None
         )
         
         if not managed_prompt or managed_prompt.strip() == "":
            raise Exception("Prompt is empty or not found")
         
         answer = llm.invoke(managed_prompt)
         content = answer.content 
         
         print(f"Bedrock response received (length: {len(content)} chars)")
         
         code = extract_code(content)
         
         if not code:
            error_msg = "Failed to extract dashboard configuration from AI response"
            print(f"❌ {error_msg}")
            print(f"Full response: {content}")
            
            error_history.append({
               'attempt': attempt + 1,
               'error_message': error_msg,
               'error_details': {'raw_response': content[:1000]},  # Limit size
               'dashboard_config': None
            })
            
            if attempt >= max_retries - 1:
               return {
                  'success': False,
                  'attempts': attempt + 1,
                  'error_history': error_history,
                  'final_error': error_msg
               }
            
            continue
         
         print(f"Dashboard configuration extracted successfully")
         
         print(f"Attempting to create dashboard in Grafana...")
         response = create_dashboard(response_url, code)
         
         if response.get('error'):
            print(f"❌ Attempt {attempt + 1} failed: {response['error_message']}")
            
            # Non-retryable errors - auth/permission issues won't fix themselves on retry
            non_retryable_keywords = ['expired', 'unauthorized', 'forbidden', 'invalid api key', 'authentication']
            error_msg_lower = response['error_message'].lower()
            is_non_retryable = any(keyword in error_msg_lower for keyword in non_retryable_keywords)
            
            error_history.append({
               'attempt': attempt + 1,
               'error_message': response['error_message'],
               'error_details': response.get('error_details'),
               'dashboard_config': response.get('dashboard_config')
            })
            
            if is_non_retryable or attempt >= max_retries - 1:
               return {
                  'success': False,
                  'attempts': attempt + 1,
                  'error_history': error_history,
                  'final_error': response['error_message']
               }
            
            print(f"Retrying with error context...")
            continue
         
         print(f"✅ Dashboard created successfully on attempt {attempt + 1}")
         return {
            'success': True,
            'attempts': attempt + 1,
            'response': response,
            'error_history': error_history if error_history else None
         }
         
      except Exception as e:
         print(f"❌ Attempt {attempt + 1} failed with exception: {str(e)}")
         import traceback
         print(f"Traceback: {traceback.format_exc()}")
         
         error_history.append({
            'attempt': attempt + 1,
            'error_message': str(e),
            'error_details': {'exception': type(e).__name__, 'traceback': traceback.format_exc()}
         })
         
         if attempt >= max_retries - 1:
            return {
               'success': False,
               'attempts': attempt + 1,
               'error_history': error_history,
               'final_error': str(e)
            }
         
         continue
   
   return {
      'success': False,
      'attempts': max_retries,
      'error_history': error_history,
      'final_error': 'Max retries exceeded'
   }

def lambda_handler(event, context):

   try:
      # Verify the request is from Slack
      verify_slack_request(event)

      body = event.get('body', '')

      if isinstance(body, bytes):
         for encoding in ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252']:
            try:
               body = body.decode(encoding)
               break
            except UnicodeDecodeError:
               continue
      if isinstance(body, bytes):
         body = body.decode('latin-1')

      params = parse_qs(body)

      response_url = params.get('response_url', [''])[0]
      user_message = params.get('text', [''])[0]

      if not user_message or user_message.strip() == "":
         raise Exception("User message is empty")

      model_kwargs =  { 
         "max_tokens": 25000,
         "temperature": 0.0,
         "top_k": 0,
      }
      llm = ChatBedrock(model_id=INFERENCE_PROFILE_ID, client=bedrock_runtime, model_kwargs=model_kwargs)
      context_data = read_file_from_s3(BUCKET_NAME, BUCKET_FILE)

      print("Starting dashboard creation with self-healing retry...")
      result = create_dashboard_with_retry(response_url, user_message, context_data, llm, max_retries=3)
      
      if result['success']:
         response_body = json.loads(result['response']['body'])
         url_path = response_body['dashboard_url']
         dashboard_url = GRAFANA_URL + url_path
         
         success_text = f'Dashboard created successfully! :white_check_mark:'
         
         slack_response = {
            'response_type': 'in_channel',
            'text': success_text,
            'blocks': [
            {
               'type': 'section',
               'text': {
                  'type': 'mrkdwn',
                  'text': f'*{success_text}*\n<{dashboard_url}|Click here to access dashboard>'
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
            'body': result['response']['body']
         }
      else:
         error_message = f"Failed to create dashboard after {result['attempts']} attempts.\n"
         error_message += f"Final error: {result['final_error']}"
         
         slack_response = {
            'response_type': 'ephemeral',
            'text': f':x: {error_message}'
         }
         send_slack_message(response_url, slack_response)
         
         return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
               'response_type': 'ephemeral',
               'text': error_message,
               'attempts': result['attempts'],
               'error_history': result['error_history']
            })
         }
   
   except Exception as e:
      print(f"Error: {str(e)}")
      # response_url may not be defined if Slack verification failed
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