import os
import boto3
import requests
from bs4 import BeautifulSoup
from boto3.dynamodb.conditions import Key
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')

def lambda_handler(event, context):
    table_name = os.environ['DYNAMODB_TABLE']
    table = dynamodb.Table(table_name)

    service = Service(executable_path=r'/opt/chromedriver')
    chrome_options = webdriver.ChromeOptions()
    chrome_options.binary_location = "/opt/chrome/chrome"
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-tools")
    chrome_options.add_argument("--no-zygote")
    chrome_options.add_argument("--single-process")
    chrome_options.add_argument("window-size=2560x1440")
    chrome_options.add_argument("--user-data-dir=/tmp/chrome-user-data")
    chrome_options.add_argument("--remote-debugging-port=9222")
    chrome_options.add_argument('user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/61.0.3163.100 Safari/537.36')

    chrome = webdriver.Chrome(service=service, options=chrome_options)

    api_gateway_urls = [
        'https://' + os.environ['API_GATEWAY_REGION1'] + '.execute-api.' + os.environ['AWS_REGION'] + '.amazonaws.com/prod/',
        'https://fu5te2nc0l.execute-api.ap-southeast-2.amazonaws.com/prod/'
    ]
    
    timestamp = int(datetime.now().timestamp())
    items = []

    for url in api_gateway_urls:
        try:
            print(f"Fetching response from {url}")

            # Sign request with SigV4
            region = url.split('.')[2]
            request = AWSRequest(method='GET', url=url, headers={'Content-Type': 'application/json'})
            credentials = boto3.Session().get_credentials()
            SigV4Auth(credentials, 'execute-api', region).add_auth(request)
            signed_headers = dict(request.headers.items())

            # Set the signed headers using execute_cdp_cmd
            chrome.execute_cdp_cmd("Network.enable", {})
            chrome.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": signed_headers})

            chrome.get(url)
            page_source = chrome.page_source
            if "Blocked" not in page_source:
                soup = BeautifulSoup(page_source, 'html.parser')
                items.extend(extract_item_info(soup))
                print(f"Page source: {page_source}")
            else:
                print(f"Error fetching response from {url}: Request unsuccessful")
                print(f"Page source: {page_source}")
        except requests.exceptions.RequestException as e:
            print(f"Error fetching response from {url}: {e}")
    
    unique_items = {item['item_id']: item for item in items}.values()
    if(len(unique_items) == 0):
        print("No items found")
        return {
            'statusCode': 200,
            'body': 'No items found'
        }
    
    # Get previous inventory status
    response = table.query(
        KeyConditionExpression=Key('item_id').eq('TIMESTAMP'),
        ScanIndexForward=False,
        Limit=1
    )
    previous_timestamp = response['Items'][0]['timestamp'] if response['Items'] else 0
    
    new_items = []
    for item in unique_items:
        item_id = item['item_id']
        title = item['title']
        color = item['color']
        url = item['url']
        price = item['price']
        unavailable = item['unavailable']
        
        if not unavailable:
            response = table.query(
                KeyConditionExpression=Key('item_id').eq(item_id),
                ScanIndexForward=False,
                Limit=1
            )
            existing_item = response['Items'][0] if response['Items'] else None
            
            if not existing_item or existing_item['timestamp'] < previous_timestamp:
                new_items.append(item)

            table.put_item(Item={
                'item_id': item_id,
                'timestamp': timestamp,
                'title': title,
                'color': color,
                'url': url,
                'price': price,
            })
            print(f"Added item {item_id} to DynamoDB")
    if new_items:
        new_items_message = "\n\n".join([f"{item['title']} - {item['color']} - {item['price']}\nhttps://hermes.com{item['url']}" for item in new_items])
        try:
            response = sns.publish(
                TopicArn=os.environ['SNS_TOPIC_ARN'],
                Subject="New Items Added",
                Message=f"The following new items have been added:\n{new_items_message}"
            )
            print(f"SNS publish response: {response}")
        except Exception as e:
            print(f"Error publishing to SNS: {str(e)}")

    
    return {
        'statusCode': 200,
        'body': 'Lambda function executed successfully'
    }

def extract_item_info(soup):
    items = []
    for div in soup.find_all('div', class_='product-grid-list-item'):
        item_id = div['id'].replace('grid-product-', '')
        title = div.find('span', class_='product-item-name').text.strip()
        color = div.find('span', class_='product-item-colors').text.split(':')[-1].strip()
        url = div.find('a')['href']
        price = int(div.find('span', class_='price').text.replace('AU$', '').replace(',', ''))
        unavailable = 'Unavailable' in div.text
        items.append({
            'item_id': item_id,
            'title': title,
            'color': color,
            'url': url,
            'price': price,
            'unavailable': unavailable
        })
    return items